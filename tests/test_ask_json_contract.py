"""Tests for ask.py --json / --no-log contract and query-path optimizations."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'

NOTE_A = """---
tags: [维护, 索引]
---
# 维护流程

## 日常维护
每周末运行 maintain.py 完成索引重建与健康检查。

## 故障排查
当嵌入库损坏时执行 embed.py --full 进行原子重建。
"""

NOTE_B = """---
tags: [周报]
---
# 周报模板

## 填写要点
每周五填写本周工作总结，包含交付事项与风险评估。
"""


def _det_vec(text: str, dim: int) -> list:
    h = 0
    for ch in text:
        h = (h * 131 + ord(ch)) % 1000003
    return [((h + i * 17) % 1000) / 1000.0 + 0.01 for i in range(dim)]


class FakeOpenAIEmbedServer:
    """Minimal OpenAI-compatible /v1/embeddings mock server."""

    def __init__(self, dim=8):
        self.dim = dim
        self.status_code = 200
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
                if outer.status_code != 200:
                    self.send_error(outer.status_code, 'mock error')
                    return
                texts = payload.get('input') or []
                body = json.dumps({
                    'object': 'list',
                    'data': [
                        {'object': 'embedding', 'index': i, 'embedding': _det_vec(t, outer.dim)}
                        for i, t in enumerate(texts)
                    ],
                    'model': payload.get('model', 'fake-embed'),
                }).encode('utf-8')
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
    def base_url(self) -> str:
        return f'http://127.0.0.1:{self.port}/v1'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeSidecarServer:
    """Minimal Ollama-compatible /api/embed sidecar mock server."""

    def __init__(self, dim=512, model='BAAI/bge-small-zh-v1.5'):
        self.dim = dim
        self.model = model
        self.status_code = 200
        self.calls = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                if self.path != '/api/embed':
                    self.send_error(404)
                    return
                length = int(self.headers.get('Content-Length') or 0)
                payload = json.loads(self.rfile.read(length) or b'{}')
                outer.calls += 1
                if outer.status_code != 200:
                    self.send_error(outer.status_code, 'sidecar error')
                    return
                raw_in = payload.get('input')
                texts = [raw_in] if isinstance(raw_in, str) else list(raw_in or [])
                body = json.dumps({
                    'embeddings': [_det_vec(t, outer.dim) for t in texts],
                    'model': outer.model,
                }).encode('utf-8')
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
    def embed_url(self) -> str:
        return f'http://127.0.0.1:{self.port}/api/embed'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class AskJsonContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / 'vault'
        self.scripts = self.vault / '.meta' / 'scripts'
        self.scripts.mkdir(parents=True)
        (self.vault / 'AGENTS.md').write_text('# Vault\n', encoding='utf-8')
        for source in SCRIPTS.glob('*.py'):
            shutil.copyfile(source, self.scripts / source.name)
        self.notes = self.vault / 'notes'
        self.notes.mkdir()
        (self.notes / 'a-maintenance.md').write_text(NOTE_A, encoding='utf-8', newline='\n')
        (self.notes / 'b-weekly.md').write_text(NOTE_B, encoding='utf-8', newline='\n')
        self.db_path = self.vault / '.meta' / 'embeddings.sqlite'
        self.server = FakeOpenAIEmbedServer(dim=8)
        self.write_env()

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def write_env(self, *, enabled=True, model='fake-embed', extra=''):
        lines = [
            'HARNESS_MODE=lite',
            'PRIMARY_HOST=',
            'MAINTAIN_CONCURRENCY=1',
            'API_RATE_LIMIT_MS=0',
            'EMBED_CHUNK_CHARS=3000',
            'EMBED_CHUNK_OVERLAP=300',
        ]
        if enabled:
            lines += [
                'EMBED_PROVIDER=http',
                f'EMBED_BASE_URL={self.server.base_url}',
                f'EMBED_MODEL={model}',
            ]
        else:
            lines += ['EMBED_PROVIDER=off']
        if extra:
            lines.append(extra.strip())
        (self.vault / '.env').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def run_script(self, name, *args, env_override=None):
        env = os.environ.copy()
        env['KB_QUERY_SIDECAR_URL'] = 'off'
        if env_override:
            env.update(env_override)
        return subprocess.run(
            [sys.executable, '-X', 'utf8', str(self.scripts / name), *args],
            cwd=self.vault,
            capture_output=True,
            text=True,
            encoding='utf-8',
            env=env,
        )

    def parse_single_envelope(self, stdout: str) -> dict:
        lines = [ln for ln in stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, f'expected exactly 1 JSON line on stdout, got: {stdout!r}')
        return json.loads(lines[0])

    # ── 1. BM25 --json --no-log contract ──
    def test_json_bm25_contract_and_exact_chunk_text(self):
        self.write_env(enabled=False)
        self.assertEqual(self.run_script('bm25_index.py', '--build').returncode, 0)

        res = self.run_script('ask.py', '--json', '--no-log', '--top-k', '5', '--bm25', '--', '索引重建')
        self.assertEqual(res.returncode, 0, res.stderr)
        env = self.parse_single_envelope(res.stdout)
        self.assertEqual(env['contract_version'], 1)
        self.assertTrue(env['ok'])
        self.assertEqual(Path(env['vault_root']).resolve(), self.vault.resolve())
        self.assertEqual(env['detail']['mode'], 'bm25')
        self.assertGreaterEqual(len(env['results']), 1)

        first = env['results'][0]
        self.assertEqual(first['path'], 'notes/a-maintenance.md')
        self.assertIsInstance(first['chunk_index'], int)
        self.assertIsInstance(first['score'], float)
        self.assertIn('索引重建', first['chunk_text'])
        # Exact byte-level unique match against file on disk (as kb.rs requires)
        raw = (self.vault / first['path']).read_bytes()
        needle = first['chunk_text'].encode('utf-8')
        self.assertEqual(raw.count(needle), 1)
        # Zero telemetry writes
        self.assertFalse((self.vault / '.meta' / 'usage-log.jsonl').exists())
        self.assertFalse((self.vault / '.meta' / 'escalation-stats.jsonl').exists())

    # ── 2. Dense & Hybrid --json --no-log contract ──
    def test_json_dense_and_hybrid_contract(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        self.assertEqual(self.run_script('bm25_index.py', '--build').returncode, 0)

        # Dense with --check (disables auto_escalate so mode stays 'dense')
        res = self.run_script('ask.py', '--json', '--no-log', '--check', '--top-k', '5', '--', '维护')
        self.assertEqual(res.returncode, 0, res.stderr)
        env = self.parse_single_envelope(res.stdout)
        self.assertEqual(env['contract_version'], 1)
        self.assertTrue(env['ok'])
        self.assertEqual(env['detail']['mode'], 'dense')
        self.assertGreaterEqual(len(env['results']), 1)
        for item in env['results']:
            self.assertIn('path', item)
            self.assertIsInstance(item['chunk_index'], int)
            self.assertIsInstance(item['score'], float)
            self.assertIn('chunk_text', item)
            raw = (self.vault / item['path']).read_bytes()
            self.assertEqual(raw.count(item['chunk_text'].encode('utf-8')), 1)

        # Hybrid mode
        hyb = self.run_script('ask.py', '--json', '--no-log', '--hybrid', '--top-k', '5', '--', '周报')
        self.assertEqual(hyb.returncode, 0, hyb.stderr)
        henv = self.parse_single_envelope(hyb.stdout)
        self.assertEqual(henv['contract_version'], 1)
        self.assertTrue(henv['ok'])
        self.assertEqual(henv['detail']['mode'], 'hybrid')
        self.assertGreaterEqual(len(henv['results']), 1)
        self.assertFalse((self.vault / '.meta' / 'usage-log.jsonl').exists())

    def test_json_crlf_note_matches_raw_disk_bytes(self):
        crlf_note = self.notes / 'c-crlf.md'
        crlf_content = (
            '---\r\ntags: [回车换行]\r\n---\r\n'
            '# CRLF 笔记\r\n\r\n'
            '## 唯一章节\r\n'
            '用于验证 Windows 回车换行文件的字节级唯一匹配。\r\n'
        )
        crlf_note.write_bytes(crlf_content.encode('utf-8'))
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        self.assertEqual(self.run_script('bm25_index.py', '--build').returncode, 0)

        # BM25 path on CRLF file
        res_bm25 = self.run_script('ask.py', '--json', '--no-log', '--bm25', '--', '回车换行')
        self.assertEqual(res_bm25.returncode, 0, res_bm25.stderr)
        env_bm25 = self.parse_single_envelope(res_bm25.stdout)
        bm25_by_path = {r['path']: r for r in env_bm25['results']}
        self.assertIn('notes/c-crlf.md', bm25_by_path)
        item_bm25 = bm25_by_path['notes/c-crlf.md']
        self.assertIn('chunk_text', item_bm25)
        raw_bytes = crlf_note.read_bytes()
        self.assertEqual(raw_bytes.count(item_bm25['chunk_text'].encode('utf-8')), 1)

        # Dense path on CRLF file
        res_dense = self.run_script('ask.py', '--json', '--no-log', '--check', '--', '回车换行')
        self.assertEqual(res_dense.returncode, 0, res_dense.stderr)
        env_dense = self.parse_single_envelope(res_dense.stdout)
        dense_by_path = {r['path']: r for r in env_dense['results']}
        self.assertIn('notes/c-crlf.md', dense_by_path)
        item_dense = dense_by_path['notes/c-crlf.md']
        self.assertIn('chunk_text', item_dense)
        self.assertEqual(raw_bytes.count(item_dense['chunk_text'].encode('utf-8')), 1)

    def test_json_stale_file_omits_chunk_text(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        # Mutate file on disk after embedding so chunk_text in SQLite no longer matches
        (self.notes / 'a-maintenance.md').write_text('# 维护流程\n\n## 日常维护\n已被修改的内容。\n', encoding='utf-8')
        res = self.run_script('ask.py', '--json', '--no-log', '--check', '--', '维护')
        self.assertEqual(res.returncode, 0, res.stderr)
        env = self.parse_single_envelope(res.stdout)
        by_path = {r['path']: r for r in env['results']}
        self.assertIn('notes/a-maintenance.md', by_path)
        self.assertIsNone(by_path['notes/a-maintenance.md']['chunk_index'])
        self.assertNotIn('chunk_text', by_path['notes/a-maintenance.md'])

    def test_json_dash_prefixed_query_after_double_dash(self):
        self.write_env(enabled=False)
        self.assertEqual(self.run_script('bm25_index.py', '--build').returncode, 0)
        res = self.run_script('ask.py', '--json', '--no-log', '--bm25', '--', '--json')
        self.assertEqual(res.returncode, 0, res.stderr)
        env = self.parse_single_envelope(res.stdout)
        self.assertTrue(env['ok'])
        self.assertEqual(env['detail']['mode'], 'bm25')

    # ── 3. Error exit codes (12 / 13 / 16 / 2) ──
    def test_exit_12_local_unavailable(self):
        # Missing embeddings.sqlite
        res = self.run_script('ask.py', '--json', '--no-log', '--', '维护')
        self.assertEqual(res.returncode, 12)
        env = self.parse_single_envelope(res.stdout)
        self.assertFalse(env['ok'])
        self.assertEqual(env['contract_version'], 1)
        self.assertEqual(env['error']['code'], 'LOCAL_UNAVAILABLE')
        # Must not have created a 0-byte db
        self.assertFalse(self.db_path.exists())

        # Missing bm25 index under --bm25
        res_bm25 = self.run_script('ask.py', '--json', '--no-log', '--bm25', '--', '维护')
        self.assertEqual(res_bm25.returncode, 12)
        env_bm25 = self.parse_single_envelope(res_bm25.stdout)
        self.assertEqual(env_bm25['error']['code'], 'LOCAL_UNAVAILABLE')

        # Unreachable endpoint (stdout must remain a single clean JSON envelope)
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        self.server.status_code = 500
        res_500 = self.run_script('ask.py', '--json', '--no-log', '--', '维护')
        self.assertEqual(res_500.returncode, 12)
        env_500 = self.parse_single_envelope(res_500.stdout)
        self.assertEqual(env_500['error']['code'], 'LOCAL_UNAVAILABLE')

    def test_exit_13_profile_mismatch(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)

        # Model name mismatch
        self.write_env(enabled=True, model='other-model')
        res_model = self.run_script('ask.py', '--json', '--no-log', '--', '维护')
        self.assertEqual(res_model.returncode, 13)
        env_model = self.parse_single_envelope(res_model.stdout)
        self.assertFalse(env_model['ok'])
        self.assertEqual(env_model['error']['code'], 'PROFILE_MISMATCH')

        # Dimension mismatch
        self.write_env(enabled=True, model='fake-embed')
        self.server.dim = 4
        res_dim = self.run_script('ask.py', '--json', '--no-log', '--', '维护')
        self.assertEqual(res_dim.returncode, 13)
        env_dim = self.parse_single_envelope(res_dim.stdout)
        self.assertEqual(env_dim['error']['code'], 'PROFILE_MISMATCH')

    def test_exit_16_remote_auth_required(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)

        # --rerank without HARNESS_MODE=full + DEEPSEEK_API_KEY
        res_rerank = self.run_script('ask.py', '--json', '--no-log', '--rerank', '--', '维护')
        self.assertEqual(res_rerank.returncode, 16)
        env_rerank = self.parse_single_envelope(res_rerank.stdout)
        self.assertFalse(env_rerank['ok'])
        self.assertEqual(env_rerank['error']['code'], 'REMOTE_AUTH_REQUIRED')

        # HTTP 401 from remote embedding endpoint
        self.server.status_code = 401
        res_401 = self.run_script('ask.py', '--json', '--no-log', '--', '维护')
        self.assertEqual(res_401.returncode, 16)
        env_401 = self.parse_single_envelope(res_401.stdout)
        self.assertEqual(env_401['error']['code'], 'REMOTE_AUTH_REQUIRED')

    def test_exit_2_invalid_request(self):
        cases = [
            ('--json',),
            ('--json', '--orphans'),
            ('--json', '--deep', '维护'),
            ('--json', '--save', '维护'),
            ('--json', '--hybrid', '--bm25', '维护'),
            ('--json', '--hybrid', '--hybrid-weights', 'bad', '维护'),
        ]
        for cli_args in cases:
            res = self.run_script('ask.py', *cli_args)
            self.assertEqual(res.returncode, 2, f'args={cli_args} stderr={res.stderr}')
            env = self.parse_single_envelope(res.stdout)
            self.assertFalse(env['ok'])
            self.assertEqual(env['error']['code'], 'INVALID_REQUEST')

    # ── 4. Zero telemetry writes under --no-log and --json ──
    def test_no_log_suppresses_usage_and_escalation_logs(self):
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        self.assertEqual(self.run_script('bm25_index.py', '--build').returncode, 0)

        # Human mode with --no-log -> zero telemetry files
        res_text = self.run_script('ask.py', '--no-log', '完全不存在的抽象概念')
        self.assertEqual(res_text.returncode, 0, res_text.stderr)
        self.assertFalse((self.vault / '.meta' / 'usage-log.jsonl').exists())
        self.assertFalse((self.vault / '.meta' / 'escalation-stats.jsonl').exists())

        # JSON mode (even without explicit --no-log) -> zero telemetry files
        res_json = self.run_script('ask.py', '--json', '完全不存在的抽象概念')
        self.assertEqual(res_json.returncode, 0, res_json.stderr)
        self.assertFalse((self.vault / '.meta' / 'usage-log.jsonl').exists())
        self.assertFalse((self.vault / '.meta' / 'escalation-stats.jsonl').exists())

    # ── 5. Time decay without N+1 git subprocesses ──
    def test_time_decay_batches_git_dates_once_without_per_path_git(self):
        # Create 8 notes, embed them, do NOT create .meta/file-dates.json
        for i in range(8):
            (self.notes / f'doc-{i}.md').write_text(
                f'# 文档 {i}\n\n## 正文\n第 {i} 篇测试文档内容。\n', encoding='utf-8'
            )
        self.assertEqual(self.run_script('embed.py').returncode, 0)
        self.assertFalse((self.vault / '.meta' / 'file-dates.json').exists())

        code = f"""
import json, subprocess, sys
sys.path.insert(0, r'{self.scripts}')
import ask

git_last_calls = []
orig_git_last = ask.git_last_dates
def spy_git_last(paths):
    git_last_calls.append(len(list(paths)))
    return {{}}
ask.git_last_dates = spy_git_last

subproc_calls = []
orig_run = subprocess.run
def spy_run(*a, **kw):
    subproc_calls.append(a[0] if a else None)
    return orig_run(*a, **kw)
subprocess.run = spy_run

# 1. With decay disabled (1.0,1.0,1.0): 0 git calls
ask.semantic_search('测试文档', top_k=5, decay_params=(1.0, 1.0, 1.0))
after_disabled = len(git_last_calls)

# 2. With default decay and missing file-dates.json: at most 1 batch git_last_dates call, 0 per-path subprocess.run
res = ask.semantic_search('测试文档', top_k=5, decay_params=None)
print(json.dumps({{
    'after_disabled': after_disabled,
    'git_last_calls': git_last_calls,
    'subproc_calls': len(subproc_calls),
    'result_count': len(res or []),
}}))
"""
        proc = subprocess.run(
            [sys.executable, '-X', 'utf8', '-c', code],
            cwd=self.vault,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stats = json.loads(proc.stdout.strip())
        self.assertEqual(stats['after_disabled'], 0)
        self.assertEqual(len(stats['git_last_calls']), 1)
        self.assertGreaterEqual(stats['git_last_calls'][0], 8)
        self.assertEqual(stats['subproc_calls'], 0)
        self.assertGreater(stats['result_count'], 0)

    # ── 6. Sidecar fast-path & silent fallback ──
    def test_sidecar_fast_path_dim_check_opt_out_and_fallback(self):
        sidecar = FakeSidecarServer(dim=512, model='BAAI/bge-small-zh-v1.5')
        try:
            code = f"""
import json, os, sys
sys.path.insert(0, r'{self.scripts}')
import ask

fallback_calls = []
class FakeClient:
    def embed_batch(self, texts):
        fallback_calls.append(list(texts))
        return [[0.25] * 512 for _ in texts]

ask.get_embeddings_client = lambda cfg, timeout=None: FakeClient()
cfg = {{'enabled': True, 'provider': 'builtin', 'model': 'BAAI/bge-small-zh-v1.5'}}
meta = {{'dim': 512}}

# Case 1: sidecar healthy + matching dim -> hits sidecar, skips fallback
os.environ['KB_QUERY_SIDECAR_URL'] = {sidecar.embed_url!r}
v1 = ask._embed_single_query('查询1', cfg, meta)
fb_after_hit = len(fallback_calls)

# Case 2: KB_QUERY_SIDECAR_URL=off -> skips sidecar, uses fallback
os.environ['KB_QUERY_SIDECAR_URL'] = 'off'
v2 = ask._embed_single_query('查询2', cfg, meta)
fb_after_off = len(fallback_calls)

# Case 3: unreachable sidecar -> silent fallback
os.environ['KB_QUERY_SIDECAR_URL'] = 'http://127.0.0.1:1/api/embed'
os.environ['KB_QUERY_SIDECAR_TIMEOUT'] = '0.2'
v3 = ask._embed_single_query('查询3', cfg, meta)
fb_after_down = len(fallback_calls)

print(json.dumps({{
    'len_v1': len(v1),
    'fb_after_hit': fb_after_hit,
    'len_v2': len(v2),
    'fb_after_off': fb_after_off,
    'len_v3': len(v3),
    'fb_after_down': fb_after_down,
}}))
"""
            proc = subprocess.run(
                [sys.executable, '-X', 'utf8', '-c', code],
                cwd=self.vault,
                capture_output=True,
                text=True,
                encoding='utf-8',
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout.strip())
            self.assertEqual(out['len_v1'], 512)
            self.assertEqual(out['fb_after_hit'], 0)
            self.assertEqual(sidecar.calls, 1)
            self.assertEqual(out['len_v2'], 512)
            self.assertEqual(out['fb_after_off'], 1)
            self.assertEqual(out['len_v3'], 512)
            self.assertEqual(out['fb_after_down'], 2)

            # Case 4: dimension mismatch from sidecar -> rejected, falls back silently
            sidecar.dim = 256
            code_dim = f"""
import json, os, sys
sys.path.insert(0, r'{self.scripts}')
import ask

fallback_calls = []
class FakeClient:
    def embed_batch(self, texts):
        fallback_calls.append(list(texts))
        return [[0.5] * 512 for _ in texts]

ask.get_embeddings_client = lambda cfg, timeout=None: FakeClient()
cfg = {{'enabled': True, 'provider': 'builtin', 'model': 'BAAI/bge-small-zh-v1.5'}}
os.environ['KB_QUERY_SIDECAR_URL'] = {sidecar.embed_url!r}
vec = ask._embed_single_query('维度不匹配', cfg, {{'dim': 512}})
print(json.dumps({{'len': len(vec), 'fallback': len(fallback_calls), 'first': vec[0]}}))
"""
            proc_dim = subprocess.run(
                [sys.executable, '-X', 'utf8', '-c', code_dim],
                cwd=self.vault,
                capture_output=True,
                text=True,
                encoding='utf-8',
            )
            self.assertEqual(proc_dim.returncode, 0, proc_dim.stderr)
            out_dim = json.loads(proc_dim.stdout.strip())
            self.assertEqual(out_dim['len'], 512)
            self.assertEqual(out_dim['fallback'], 1)
            self.assertEqual(out_dim['first'], 0.5)
        finally:
            sidecar.close()

    def test_json_rerank_timeout_and_error_emit_degraded_envelope(self):
        """P1-1: rerank 超时/异常时信封必须如实声明 degraded=True 与 detail.rerank.skipped，不谎报 mode='rerank'。"""
        self.write_env(enabled=True, extra='HARNESS_MODE=full\nDEEPSEEK_API_KEY=sk-test-dummy')
        emb = self.run_script('embed.py', '--full')
        self.assertEqual(emb.returncode, 0, emb.stderr)

        for err_expr, expected_reason in [
            ("TimeoutError('read timed out')", 'timeout'),
            ("RuntimeError('upstream 502')", 'error'),
        ]:
            code = f"""
import sys
sys.path.insert(0, r'{self.scripts}')
import common, ask

class FailingDeepSeek:
    def chat(self, *a, **kw):
        raise {err_expr}

common.DeepSeekClient = FailingDeepSeek
try:
    ask.main(['--json', '--no-log', '--rerank', '--', '设备维保'])
except SystemExit as e:
    sys.exit(e.code)
"""
            env = os.environ.copy()
            env['KB_QUERY_SIDECAR_URL'] = 'off'
            proc = subprocess.run(
                [sys.executable, '-X', 'utf8', '-c', code],
                cwd=self.vault,
                capture_output=True,
                text=True,
                encoding='utf-8',
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = self.parse_single_envelope(proc.stdout)
            self.assertTrue(payload['ok'])
            self.assertTrue(payload['degraded'])
            self.assertIn(payload['detail'].get('mode'), ('dense', 'hybrid'))
            self.assertNotEqual(payload['detail'].get('mode'), 'rerank')
            self.assertEqual(
                payload['detail'].get('rerank'),
                {'skipped': expected_reason},
            )
            self.assertGreaterEqual(len(payload['results']), 1)

    def test_embed_bad_env_warns_to_stderr_not_stdout(self):
        """P2-1: .env 中 EMBED_CHUNK_CHARS 非法值告警必须打到 stderr，不得污染 --json 的 stdout 单文档信封。"""
        self.write_env(enabled=False)
        bm = self.run_script('bm25_index.py', '--build')
        self.assertEqual(bm.returncode, 0, bm.stderr)
        (self.vault / '.env').write_text(
            'HARNESS_MODE=lite\nEMBED_PROVIDER=off\nEMBED_CHUNK_CHARS=not_an_int\n',
            encoding='utf-8',
        )
        proc = self.run_script('ask.py', '--json', '--no-log', '--bm25', '--', '设备维保')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('EMBED_CHUNK_CHARS', proc.stderr)
        payload = self.parse_single_envelope(proc.stdout)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['detail']['mode'], 'bm25')

    def test_preload_file_dates_scans_new_paths_incrementally(self):
        """P2-5: 同进程后续新增候选路径也能触发增量批量 git_last_dates，不静默退化为 mtime。"""
        code = f"""
import json, sys
sys.path.insert(0, r'{self.scripts}')
import ask

calls = []
def fake_git_last_dates(paths):
    calls.append(list(paths))
    return {{p: 1700000000.0 for p in paths}}

ask._file_dates = {{}}
ask._git_dates_cache.clear()
ask._git_dates_checked.clear()
ask.git_last_dates = fake_git_last_dates

ask._preload_file_dates(['notes/a.md'])
ask._preload_file_dates(['notes/a.md'])
ask._preload_file_dates(['notes/a.md', 'notes/b.md'])
print(json.dumps({{'calls': calls, 'keys': sorted(ask._git_dates_cache.keys())}}))
"""
        proc = subprocess.run(
            [sys.executable, '-X', 'utf8', '-c', code],
            cwd=self.vault,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip())
        self.assertEqual(out['calls'], [['notes/a.md'], ['notes/b.md']])
        self.assertEqual(out['keys'], ['notes/a.md', 'notes/b.md'])


if __name__ == '__main__':
    unittest.main()
