"""语义层测试：通用 embeddings 客户端、失败保护、显式重建、降级。

用一个本地假 OpenAI 兼容 /embeddings 服务端做确定性验证，不依赖 Ollama / 云 key。
"""
from pathlib import Path
import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'

NOTE_A = """# 维护流程

## 日常维护
每周末运行 maintain.py 完成索引重建与健康检查。
"""

NOTE_B = """# 周报模板

## 填写要点
每周五填写本周工作总结，包含交付事项与风险。
"""


def _vec(text: str, dim: int) -> list:
    """确定性伪向量：同一文本同一维度下稳定。"""
    h = 0
    for ch in text:
        h = (h * 131 + ord(ch)) % 1000003
    return [((h + i * 17) % 1000) / 1000.0 for i in range(dim)]


class FakeEmbedServer:
    def __init__(self, dim=8):
        self.dim = dim
        self.fail = False
        self.fail_after = None
        self.fail_texts = ()          # 输入文本包含任一子串即 500（模拟单块失败）
        self.calls = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                if not self.path.endswith('/embeddings'):
                    self.send_error(404)
                    return
                length = int(self.headers.get('Content-Length') or 0)
                payload = json.loads(self.rfile.read(length) or b'{}')
                outer.calls += 1
                texts = payload.get('input') or []
                if (outer.fail
                        or (outer.fail_after is not None and outer.calls > outer.fail_after)
                        or any(m in t for t in texts for m in outer.fail_texts)):
                    self.send_error(500, 'boom')
                    return
                body = json.dumps({
                    'object': 'list',
                    'data': [{'object': 'embedding', 'index': i,
                              'embedding': _vec(t, outer.dim)}
                             for i, t in enumerate(texts)],
                    'model': payload.get('model', 'fake'),
                }).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f'http://127.0.0.1:{self.port}/v1'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class SemanticLayerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / '知识库'
        self.scripts = self.vault / '.meta' / 'scripts'
        self.scripts.mkdir(parents=True)
        (self.vault / 'AGENTS.md').write_text('', encoding='utf-8')
        for source in SCRIPTS.glob('*.py'):
            shutil.copyfile(source, self.scripts / source.name)
        self.notes = self.vault / 'notes'
        self.notes.mkdir()
        (self.notes / 'a-维护.md').write_text(NOTE_A, encoding='utf-8')
        (self.notes / 'b-周报.md').write_text(NOTE_B, encoding='utf-8')
        self.db_path = self.vault / '.meta' / 'embeddings.sqlite'
        self.server = FakeEmbedServer(dim=8)
        self.write_env()

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    # ── helpers ──
    def write_env(self, *, enabled=True, chunks=3000, overlap=300):
        lines = [
            'HARNESS_MODE=lite',
            'PRIMARY_HOST=',
            'MAINTAIN_CONCURRENCY=1',
            'API_RATE_LIMIT_MS=0',
            f'EMBED_CHUNK_CHARS={chunks}',
            f'EMBED_CHUNK_OVERLAP={overlap}',
        ]
        if enabled:
            lines += ['EMBED_PROVIDER=http', f'EMBED_BASE_URL={self.server.base_url}',
                      'EMBED_MODEL=fake-embed']
        else:
            lines += ['EMBED_PROVIDER=off']
        (self.vault / '.env').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def run_script(self, name, *args):
        return subprocess.run(
            [sys.executable, str(self.scripts / name), *args],
            cwd=self.vault, capture_output=True, text=True, encoding='utf-8',
        )

    def subprocess_py(self, code):
        return subprocess.run(
            [sys.executable, '-c', code],
            cwd=self.vault, capture_output=True, text=True, encoding='utf-8',
        )

    def rows(self):
        con = sqlite3.connect(str(self.db_path))
        try:
            return con.execute("SELECT count(*), length(embedding) FROM embeddings").fetchall()
        finally:
            con.close()

    def meta(self):
        con = sqlite3.connect(str(self.db_path))
        try:
            row = con.execute(
                "SELECT model, dim, chunk_chars, chunk_overlap FROM embed_meta").fetchone()
        finally:
            con.close()
        return row

    # ── provider 解析（纯配置，不加载模型）──
    def test_provider_off_disables_even_if_fastembed_installed(self):
        self.write_env(enabled=False)
        result = self.run_script('embed.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('语义层未启用', result.stdout)
        self.assertFalse(self.db_path.exists())

    def test_provider_http_requires_full_config(self):
        (self.vault / '.env').write_text(
            'EMBED_PROVIDER=http\nEMBED_MODEL=fake-embed\n', encoding='utf-8')
        result = self.run_script('embed.py')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('成对', result.stdout)

    def test_half_config_does_not_fall_back_to_builtin(self):
        # 只填 base_url：必须报配置错误，不能静默改用本地模型
        (self.vault / '.env').write_text(
            f'EMBED_BASE_URL={self.server.base_url}\n', encoding='utf-8')
        result = self.run_script('embed.py')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('成对', result.stdout)

    @unittest.skipUnless(importlib.util.find_spec('fastembed'),
                         '需要 fastembed 才能验证内置兜底')
    def test_auto_uses_builtin_when_fastembed_present(self):
        # 不加载模型，只验证配置解析：auto + 无端点 → builtin（这是必需的基础能力）
        (self.vault / '.env').write_text('EMBED_PROVIDER=auto\n', encoding='utf-8')
        code = ("import sys; sys.path.insert(0, r'%s');"
                "from common import embed_settings as s;"
                "c = s(); print(c['provider'], c['model'], c['reason'])" % self.scripts)
        result = self.subprocess_py(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('builtin BAAI/bge-small-zh-v1.5', result.stdout)

    def test_auto_prefers_http_when_base_url_present(self):
        # 本机装了 fastembed 时也必须优先走 http（显式配置优先于降级）
        (self.vault / '.env').write_text(
            f'EMBED_BASE_URL={self.server.base_url}\nEMBED_MODEL=fake-embed\n'
            'PRIMARY_HOST=\nAPI_RATE_LIMIT_MS=0\nMAINTAIN_CONCURRENCY=1\n',
            encoding='utf-8')
        result = self.run_script('embed.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('http · fake-embed', result.stdout)

    @unittest.skipUnless(os.environ.get('KB_TEST_FASTEMBED') == '1',
                         'set KB_TEST_FASTEMBED=1 to run the builtin-model e2e')
    def test_builtin_model_end_to_end(self):
        (self.vault / '.env').write_text(
            'EMBED_PROVIDER=builtin\nEMBED_MODEL=BAAI/bge-small-zh-v1.5\n'
            'PRIMARY_HOST=\n', encoding='utf-8')
        build = self.run_script('embed.py')
        self.assertEqual(build.returncode, 0, build.stderr)
        self.assertIn('builtin', build.stdout)
        self.assertEqual(self.meta(), ('BAAI/bge-small-zh-v1.5', 512, 450, 50))
        hit = self.run_script('ask.py', '索引重建')
        self.assertEqual(hit.returncode, 0, hit.stderr)
        self.assertIn('notes/a-维护.md', hit.stdout)

    def test_build_and_query(self):
        build = self.run_script('embed.py')
        self.assertEqual(build.returncode, 0, build.stderr)
        self.assertEqual(self.meta(), ('fake-embed', 8, 3000, 300))
        count, blob = self.rows()[0]
        self.assertGreater(count, 0)
        self.assertEqual(blob, 8 * 4)

        hit = self.run_script('ask.py', '索引重建')
        self.assertEqual(hit.returncode, 0, hit.stderr)
        self.assertIn('notes/a-维护.md', hit.stdout)

    def test_probe_failure_leaves_db_untouched(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        before_rows = self.rows()
        before_meta = self.meta()

        self.server.fail = True
        result = self.run_script('embed.py', '--full')
        self.assertEqual(result.returncode, 1)
        self.assertIn('嵌入来源不可用', result.stdout)
        self.assertEqual(self.rows(), before_rows)
        self.assertEqual(self.meta(), before_meta)

    def test_dim_change_requires_explicit_full(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        before = self.rows()

        self.server.dim = 4  # 端点换了模型
        result = self.run_script('embed.py')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('--full', result.stdout)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.meta()[1], 8)

    def test_full_swap_updates_meta_and_dims(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        self.server.dim = 4
        result = self.run_script('embed.py', '--full')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.meta(), ('fake-embed', 4, 3000, 300))
        for count, blob in self.rows():
            self.assertEqual(blob, 16)
        self.assertIn('已切换为新一代向量', result.stdout)

    def test_failed_full_keeps_old_vectors(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        before = self.rows()

        self.server.dim = 4
        self.server.fail_after = self.server.calls + 1  # 探针通过，随后全部失败
        result = self.run_script('embed.py', '--full')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.meta()[1], 8)
        con = sqlite3.connect(str(self.db_path))
        try:
            staging = con.execute(
                "SELECT name FROM sqlite_master WHERE name='embeddings_staging'").fetchone()
        finally:
            con.close()
        self.assertIsNone(staging)

    def test_partial_full_rebuild_does_not_switch(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        before = self.rows()
        before_meta = self.meta()

        # 新增一篇"整篇都会失败"的笔记：其余篇成功也不能换表
        (self.notes / 'c-fail.md').write_text(
            '# FAILME\n\n## x\nFAILME 内容', encoding='utf-8')
        self.server.fail_texts = ('FAILME',)
        self.server.dim = 4
        result = self.run_script('embed.py', '--full')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('未写入成功', result.stdout)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.meta(), before_meta)
        con = sqlite3.connect(str(self.db_path))
        try:
            self.assertIsNone(con.execute(
                "SELECT name FROM sqlite_master WHERE name='embeddings_staging'").fetchone())
        finally:
            con.close()

    def test_ask_falls_back_to_bm25_when_disabled(self):
        self.write_env(enabled=False)
        self.assertEqual(self.run_script('bm25_index.py', '--build').returncode, 0)
        result = self.run_script('ask.py', '索引重建')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('语义层不可用', result.stderr)
        self.assertIn('notes/a-维护.md', result.stdout)

    def test_links_are_built_without_llm(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        self.write_env(chunks=3000, overlap=300)
        # 阈值降到 0：只验证"有向量就能产出 links"，不依赖假向量的分布
        env = (self.vault / '.env').read_text(encoding='utf-8')
        (self.vault / '.env').write_text(env + 'LINKS_SIM_THRESHOLD=0\n', encoding='utf-8')
        links = self.run_script('build_links.py')
        self.assertEqual(links.returncode, 0, links.stderr)
        for rel in ('notes/a-维护.md', 'notes/b-周报.md'):
            p = self.vault / '.meta' / 'links' / rel
            self.assertTrue(p.exists(), f'缺少 links sidecar: {rel}')
            text = p.read_text(encoding='utf-8')
            self.assertIn('model: cosine(', text)
            self.assertIn('- [[', text)
        graph = self.run_script('build_graph.py')
        self.assertEqual(graph.returncode, 0, graph.stderr)
        g = json.loads((self.vault / '.meta' / 'graph.json').read_text(encoding='utf-8'))
        self.assertIn('fake-embed', g['model'])

    def test_hybrid_returns_none_when_both_layers_unavailable(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        self.server.dim = 4                       # 语义库与端点不一致
        (self.vault / '.meta' / 'bm25_index.pkl.gz').unlink(missing_ok=True)  # BM25 也不可用
        code = ("import sys; sys.path.insert(0, r'%s');"
                "from ask import hybrid_search;"
                "print(hybrid_search('索引重建'))" % self.scripts)
        result = self.subprocess_py(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertIn('None', result.stdout)

    def test_deep_hybrid_does_not_crash_when_nothing_available(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        self.server.dim = 4
        (self.vault / '.meta' / 'bm25_index.pkl.gz').unlink(missing_ok=True)
        (self.vault / '.env').write_text(
            (self.vault / '.env').read_text(encoding='utf-8')
            + 'DEEPSEEK_API_KEY=test\nDEEPSEEK_BASE_URL=http://127.0.0.1:9/v1\n',
            encoding='utf-8')
        result = self.run_script('ask.py', '--deep', '--hybrid', '索引重建')
        self.assertNotIn('Traceback', result.stderr)
        self.assertIn(result.returncode, (0, 2))

    def test_full_with_no_embeddable_content_fails(self):
        # 只剩一篇不可读笔记：没有任何用户语料向量 → 不得报成功
        (self.notes / 'a-维护.md').unlink()
        (self.notes / 'b-周报.md').unlink()
        (self.notes / 'gbk.md').write_bytes('# GBK\n\n## x\n国标'.encode('gbk'))
        result = self.run_script('embed.py', '--full')
        self.assertEqual(result.returncode, 1)
        self.assertIn('没有可嵌入的内容', result.stdout)

    def test_inbox_scan_respects_llm_gate(self):
        (self.vault / '收件箱条目.md').write_text('# 待归类\n\n## x\n内容', encoding='utf-8')
        (self.vault / '.env').write_text(
            'HARNESS_MODE=lite\nPRIMARY_HOST=\nDEEPSEEK_API_KEY=test\n'
            'DEEPSEEK_BASE_URL=http://127.0.0.1:9/v1\n', encoding='utf-8')
        result = self.run_script('inbox_scan.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('仅列出', result.stdout)
        self.assertNotIn('[LLM]', result.stdout)

    def test_threshold_defaults_follow_provider(self):
        (self.vault / '.env').write_text('EMBED_PROVIDER=builtin\n', encoding='utf-8')
        code = ("import sys; sys.path.insert(0, r'%s');"
                "from common import threshold_defaults;"
                "print(threshold_defaults()['links'])" % self.scripts)
        builtin = self.subprocess_py(code)
        self.assertEqual(builtin.returncode, 0, builtin.stderr)
        self.assertIn('0.58', builtin.stdout)

        (self.vault / '.env').write_text(
            f'EMBED_PROVIDER=http\nEMBED_BASE_URL={self.server.base_url}\n'
            'EMBED_MODEL=fake-embed\n', encoding='utf-8')
        http = self.subprocess_py(code)
        self.assertEqual(http.returncode, 0, http.stderr)
        self.assertIn('0.7', http.stdout)

    def test_calibrate_thresholds_outputs_suggestions(self):
        (self.notes / 'a').mkdir(exist_ok=True)
        (self.notes / 'b').mkdir(exist_ok=True)
        (self.notes / 'a' / 'x.md').write_text('# X\n\n## s\n甲', encoding='utf-8')
        (self.notes / 'b' / 'y.md').write_text('# Y\n\n## s\n乙', encoding='utf-8')
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        result = self.run_script('calibrate_thresholds.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('LINKS_SIM_THRESHOLD=', result.stdout)
        self.assertIn('SEMANTIC_THRESHOLD=', result.stdout)

    def test_hash_in_filename_keeps_semantic_edge(self):
        (self.notes / '报告 #1.md').write_text('# 报告\n\n## x\n一号报告', encoding='utf-8')
        (self.notes / 'index.md').write_text('# 索引\n\n## x\n入口', encoding='utf-8')
        env = (self.vault / '.env').read_text(encoding='utf-8')
        (self.vault / '.env').write_text(
            env + 'LINKS_SIM_THRESHOLD=0\nSEMANTIC_THRESHOLD=0\n', encoding='utf-8')
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        self.assertEqual(self.run_script('build_links.py').returncode, 0)
        side = (self.vault / '.meta' / 'links' / 'notes' / 'index.md').read_text(encoding='utf-8')
        self.assertIn('%231', side)
        self.assertEqual(self.run_script('build_graph.py').returncode, 0)
        g = json.loads((self.vault / '.meta' / 'graph.json').read_text(encoding='utf-8'))
        targets = {e['target'] for e in g['edges'] if e.get('source') == 'notes/index.md'}
        self.assertIn('notes/报告 #1.md', targets)

    def test_privacy_dirs_from_dotenv(self):
        (self.vault / '.env').write_text(
            'EMBED_PROVIDER=off\nPRIVACY_DIRS=secret\n', encoding='utf-8')
        code = ("import sys; sys.path.insert(0, r'%s');"
                "import common; print('secret' in common.EXCLUDE_DIRS)" % self.scripts)
        result = self.subprocess_py(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('True', result.stdout)

    def test_office_extracts_count_as_corpus(self):
        for name in ('a-维护.md', 'b-周报.md'):
            (self.notes / name).unlink()
        (self.vault / '.meta' / 'office-extracts').mkdir(parents=True, exist_ok=True)
        (self.vault / '.meta' / 'office-extracts' / '报表.xlsx.md').write_text(
            '# 报表\n\n## x\n营收数据', encoding='utf-8')
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        code = ("import sys; sys.path.insert(0, r'%s');"
                "import maintain; print('count', maintain._corpus_vector_count())" % self.scripts)
        result = self.subprocess_py(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('count 1', result.stdout)

    def test_thresholds_fall_back_to_stored_provider(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)   # 库内 provider=http
        (self.vault / '.env').write_text('EMBED_PROVIDER=off\n', encoding='utf-8')
        code = ("import sys; sys.path.insert(0, r'%s');"
                "from common import threshold_defaults;"
                "print(threshold_defaults()['graph'])" % self.scripts)
        result = self.subprocess_py(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('0.75', result.stdout)

    def test_literal_percent_name_resolves_before_unquote(self):
        (self.notes / 'a%23b.md').write_text('# 百分号\n\n## x\n内容', encoding='utf-8')
        code = ("import sys; sys.path.insert(0, r'" + str(self.scripts) + "');"
                "from common import resolve_wikilink;"
                "print(resolve_wikilink('notes/a%23b', 'notes/x.md', {}, {}, {'notes/a%23b.md'}))")
        result = self.subprocess_py(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("notes/a%23b.md", result.stdout)

    def test_usage_log_redacts_query(self):
        self.write_env(enabled=False)
        self.run_script('ask.py', '绝密查询词')
        log = (self.vault / '.meta' / 'usage-log.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('绝密查询词', log)
        self.assertIn('query_sha256', log)

    def test_health_report_sections_order(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        self.assertEqual(self.run_script('build_graph.py').returncode, 0)
        self.assertEqual(self.run_script('health_report.py').returncode, 0)
        text = (self.vault / '.meta' / 'health-report.md').read_text(encoding='utf-8')
        self.assertIn('## 语义层', text)
        self.assertLess(text.index('## 告警'), text.index('## 语义层'))

    def test_bm25_granularity_is_independent_of_embed_provider(self):
        # BM25 是基础层，其分块粒度不能随语义层配置变化
        self.write_env(enabled=False)
        self.assertEqual(self.run_script('bm25_index.py', '--build').returncode, 0)
        self.write_env()  # 切到 http + 默认 3000
        result = self.run_script('bm25_index.py', '--build')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('分块规则已变化', result.stderr)
        self.assertIn('复用缓存 2', result.stdout)

    def test_partial_chunk_failure_does_not_swap(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        before = self.rows()
        before_meta = self.meta()

        self.server.fail_texts = ('周报',)   # 让 b-周报.md 的块失败
        self.server.dim = 4                 # 强制走 --full 重建
        result = self.run_script('embed.py', '--full')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('未写入成功', result.stdout)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.meta(), before_meta)
        con = sqlite3.connect(str(self.db_path))
        try:
            self.assertIsNone(con.execute(
                "SELECT name FROM sqlite_master WHERE name='embeddings_staging'").fetchone())
        finally:
            con.close()

    def test_ask_falls_back_when_meta_mismatch(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        self.assertEqual(self.run_script('bm25_index.py', '--build').returncode, 0)
        self.server.dim = 4  # 库内 8 维，端点现在返回 4 维
        result = self.run_script('ask.py', '索引重建')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('BM25', result.stdout + result.stderr)
        self.assertIn('notes/a-维护.md', result.stdout)

    def test_zero_byte_db_falls_back_to_bm25(self):
        self.assertEqual(self.run_script('bm25_index.py', '--build').returncode, 0)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_bytes(b'')
        result = self.run_script('ask.py', '索引重建')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('notes/a-维护.md', result.stdout)

    def test_maintain_does_not_infer_llm_layer_from_semantic_db(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        # 去掉 HARNESS_MODE：语义层库存在 ≠ LLM 层
        (self.vault / '.env').write_text(
            f'EMBED_PROVIDER=http\nEMBED_BASE_URL={self.server.base_url}\n'
            'EMBED_MODEL=fake-embed\nPRIMARY_HOST=\n', encoding='utf-8')
        result = self.run_script('maintain.py', '--no-git')
        self.assertIn('HARNESS_MODE=lite', result.stdout)
        self.assertNotIn('摘要 / tag', result.stdout)

    def test_maintain_survives_embed_failure_but_exits_3(self):
        mem = self.vault / '.meta' / 'memory'
        mem.mkdir(parents=True, exist_ok=True)
        (mem / 'MEMORY.md').write_text(
            '# 记忆索引\n\n<!-- memory-index:start -->\n<!-- memory-index:end -->\n',
            encoding='utf-8')
        (self.vault / '.env').write_text(
            'HARNESS_MODE=lite\nPRIMARY_HOST=\nEMBED_PROVIDER=http\n'
            'EMBED_BASE_URL=http://127.0.0.1:9/v1\nEMBED_MODEL=x\n', encoding='utf-8')
        result = self.run_script('maintain.py', '--no-git')
        self.assertIn('语义层本轮失败', result.stdout)
        self.assertTrue((self.vault / '.meta' / 'bm25_index.pkl.gz').exists())
        self.assertEqual(result.returncode, 3)

    def test_links_rebuild_on_model_or_threshold_change(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        env = (self.vault / '.env').read_text(encoding='utf-8')
        (self.vault / '.env').write_text(env + 'LINKS_SIM_THRESHOLD=0\n', encoding='utf-8')
        first = self.run_script('build_links.py')
        self.assertEqual(first.returncode, 0, first.stderr)

        # 阈值变化 → 必须重建（而不是"无需更新"）
        (self.vault / '.env').write_text(
            env + 'LINKS_SIM_THRESHOLD=0.99\n', encoding='utf-8')
        second = self.run_script('build_links.py')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('篇更新', second.stdout)
        self.assertNotIn('无需更新', second.stdout)

    def test_corrupt_bm25_index_is_not_reported_as_no_hits(self):
        self.assertEqual(self.run_script('bm25_index.py', '--build').returncode, 0)
        (self.vault / '.meta' / 'bm25_index.pkl.gz').write_bytes(b'corrupted')
        plain = self.run_script('ask.py', '索引重建')
        self.assertEqual(plain.returncode, 2)
        self.assertNotIn('未找到相关笔记', plain.stdout)
        check = self.run_script('ask.py', '--check', '索引重建')
        self.assertEqual(check.returncode, 2)
        self.assertIn('查重不可用', check.stderr)

    def test_unreadable_note_does_not_block_full_rebuild(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        # GBK 编码笔记：不可读，但不应阻塞换代
        (self.notes / 'gbk.md').write_bytes(
            '# GBK\n\n## x\n中文国标编码'.encode('gbk'))
        self.server.dim = 4
        result = self.run_script('embed.py', '--full')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.meta()[1], 4)
        self.assertIn('已跳过', result.stdout)
        self.assertTrue((self.vault / '.meta' / 'embed-skipped.md').exists())

    def test_same_stem_notes_keep_distinct_semantic_edges(self):
        (self.notes / 'a').mkdir()
        (self.notes / 'b').mkdir()
        (self.notes / 'a' / 'topic.md').write_text('# 甲主题\n\n## x\n甲内容', encoding='utf-8')
        (self.notes / 'b' / 'topic.md').write_text('# 乙主题\n\n## x\n乙内容', encoding='utf-8')
        (self.notes / 'index.md').write_text('# 索引\n\n## x\n入口', encoding='utf-8')
        env = (self.vault / '.env').read_text(encoding='utf-8')
        (self.vault / '.env').write_text(
            env + 'LINKS_SIM_THRESHOLD=0\nSEMANTIC_THRESHOLD=0\n', encoding='utf-8')
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        self.assertEqual(self.run_script('build_links.py').returncode, 0)
        side = (self.vault / '.meta' / 'links' / 'notes' / 'index.md').read_text(encoding='utf-8')
        self.assertIn('[[notes/a/topic|topic]]', side)
        self.assertIn('[[notes/b/topic|topic]]', side)
        self.assertEqual(self.run_script('build_graph.py').returncode, 0)
        g = json.loads((self.vault / '.meta' / 'graph.json').read_text(encoding='utf-8'))
        targets = {e['target'] for e in g['edges'] if e.get('source') == 'notes/index.md'}
        self.assertIn('notes/a/topic.md', targets)
        self.assertIn('notes/b/topic.md', targets)

    def test_builtin_ignores_zhipu_http_model(self):
        (self.vault / '.env').write_text(
            'EMBED_PROVIDER=builtin\nZHIPU_API_KEY=test\n'
            'ZHIPU_EMBED_MODEL=embedding-3\n', encoding='utf-8')
        code = ("import sys; sys.path.insert(0, r'%s');"
                "from common import embed_settings as s;"
                "c = s(); print(c['provider'], c['model'])" % self.scripts)
        result = self.subprocess_py(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('builtin BAAI/bge-small-zh-v1.5', result.stdout)

    def test_invalid_mode_is_treated_as_lite(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        (self.vault / '.env').write_text(
            'HARNESS_MODE=ful\nDEEPSEEK_API_KEY=test\nPRIMARY_HOST=\n'
            f'EMBED_PROVIDER=http\nEMBED_BASE_URL={self.server.base_url}\n'
            'EMBED_MODEL=fake-embed\n', encoding='utf-8')
        result = self.run_script('maintain.py', '--no-git')
        self.assertIn('HARNESS_MODE=lite', result.stdout)
        self.assertNotIn('摘要 / tag', result.stdout)

    def test_office_failure_is_reported_without_half_sidecar(self):
        (self.notes / 'broken.docx').write_bytes(b'not a docx')
        (self.vault / '.env').write_text(
            'PRIMARY_HOST=\nOFFICE_EXTRACT_TIMEOUT_S=30\nOFFICE_EXTRACT_WORKERS=1\n',
            encoding='utf-8')
        result = self.run_script('extract_office.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        failures = self.vault / '.meta' / 'office-extracts' / '_failures.md'
        self.assertTrue(failures.exists())
        self.assertIn('broken.docx', failures.read_text(encoding='utf-8'))
        self.assertEqual(list((self.vault / '.meta' / 'office-extracts').rglob('*.tmp')), [])

    def test_office_timeout_skips_file(self):
        (self.vault / 'notes' / 'big.docx').write_bytes(b'PK-not-a-real-docx')
        (self.vault / '.env').write_text(
            'PRIMARY_HOST=\nOFFICE_EXTRACT_TIMEOUT_S=0.001\nOFFICE_EXTRACT_WORKERS=1\n',
            encoding='utf-8')
        result = self.run_script('extract_office.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.vault / '.meta' / 'office-extracts' / '_timeouts.md'
        self.assertTrue(report.exists())
        self.assertIn('big.docx', report.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()