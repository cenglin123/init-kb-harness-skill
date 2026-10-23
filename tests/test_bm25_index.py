"""BM25 v2 索引测试：构建/查询/增量一致性/缓存清理/并行路径。

自包含：临时 vault（AGENTS.md + .meta/scripts 拷贝）+ 子进程调用脚本，
不触碰任何真实知识库。
"""
from pathlib import Path
import gzip
import pickle
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'

NOTE_A = """# 知识库维护流程

## 日常维护
每周末运行 maintain.py 完成索引重建与健康检查。
BM25 索引支持增量构建，只重新分词变化的文件。
"""

NOTE_B = """# 周报模板

每周五填写本周工作总结，包含交付事项与风险。
与知识库索引无关的普通内容。
"""

NOTE_C = """# 会议纪要

项目启动会讨论了品牌视觉规范与交付排期。
"""


class BM25IndexTests(unittest.TestCase):
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

    def tearDown(self):
        self.tmp.cleanup()

    # ── helpers ──
    def write_note(self, name, content):
        path = self.notes / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def run_bm25(self, *args):
        return subprocess.run(
            [sys.executable, str(self.scripts / 'bm25_index.py'), *args],
            cwd=self.vault, capture_output=True, text=True, encoding='utf-8',
        )

    def query_paths(self, query, top_k=5):
        result = self.run_bm25('--query', query, '--top-k', str(top_k))
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = []
        for line in result.stdout.splitlines():
            if line.startswith('['):
                paths.append(line.split('] ', 1)[1].rsplit(' (chunk', 1)[0])
        return paths

    def query_scores(self, query, top_k=5):
        """返回 {path: score}（取每 path 最高分），用于增量一致性精确比对。"""
        result = self.run_bm25('--query', query, '--top-k', str(top_k))
        self.assertEqual(result.returncode, 0, result.stderr)
        best = {}
        for line in result.stdout.splitlines():
            if not line.startswith('['):
                continue
            score = float(line[1:line.index(']')])
            path = line.split('] ', 1)[1].rsplit(' (chunk', 1)[0]
            best[path] = max(best.get(path, 0.0), score)
        return best

    def seed_three_notes(self):
        self.write_note('a-维护流程.md', NOTE_A)
        self.write_note('b-周报.md', NOTE_B)
        self.write_note('c-会议纪要.md', NOTE_C)
        result = self.run_bm25('--build')
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    # ── tests ──
    def test_build_and_query(self):
        self.seed_three_notes()
        self.assertTrue((self.vault / '.meta' / 'bm25_index.pkl.gz').exists())
        paths = self.query_paths('知识库维护')
        self.assertTrue(paths)
        self.assertIn('notes/a-维护流程.md', paths[0])
        # 不相关词不应命中
        self.assertEqual(self.query_paths('量子纠缠'), [])

    def test_incremental_matches_full_rebuild(self):
        self.seed_three_notes()
        # 变更：改 A、新增 D、删除 B
        self.write_note('a-维护流程.md', NOTE_A + '\n补充：嵌入数据库只在完整版启用。\n')
        self.write_note('sub/d-深度学习.md', '# 深度学习笔记\n\n梯度下降与反向传播是训练神经网络的基础。')
        (self.notes / 'b-周报.md').unlink()

        incremental = self.run_bm25('--build')
        self.assertEqual(incremental.returncode, 0, incremental.stderr)
        # 增量应复用未变文件、只重算变更（A 改、D 新增、B 删除 → 2 重算 + 1 复用）
        self.assertIn('重新分词 2', incremental.stdout)
        self.assertIn('复用缓存 1', incremental.stdout)

        queries = ['知识库维护', '深度学习 梯度', '会议纪要 品牌视觉', '嵌入数据库']
        after_incr = {q: self.query_scores(q) for q in queries}

        full = self.run_bm25('--build', '--rebuild')
        self.assertEqual(full.returncode, 0, full.stderr)
        for q in queries:
            self.assertEqual(after_incr[q], self.query_scores(q),
                             f'增量与全量重建结果不一致（query={q}）')

        # 删除的文件不应再被命中
        for paths in [self.query_paths('周报工作总结')]:
            self.assertFalse(any('b-周报' in p for p in paths))

    def test_cache_reuse_and_cleanup(self):
        self.seed_three_notes()
        import pickle
        cache_path = self.vault / '.meta' / 'bm25_doc_cache.pkl.gz'
        self.assertTrue(cache_path.exists())

        def cache_docs():
            with gzip.open(cache_path, 'rb') as f:
                return pickle.load(f)['docs']

        docs_before = cache_docs()
        self.assertEqual(len(docs_before), 3)
        self.write_note('c-会议纪要.md', NOTE_C + '\n追加一段内容使 hash 变化。')
        result = self.run_bm25('--build')
        self.assertEqual(result.returncode, 0, result.stderr)
        docs_after = cache_docs()
        # 未变文件 hash 不变；变更文件 hash 变化
        self.assertEqual(docs_before['notes/a-维护流程.md']['hash'],
                         docs_after['notes/a-维护流程.md']['hash'])
        self.assertNotEqual(docs_before['notes/c-会议纪要.md']['hash'],
                            docs_after['notes/c-会议纪要.md']['hash'])
        # 删除文件后缓存清理
        (self.notes / 'b-周报.md').unlink()
        result = self.run_bm25('--build')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('notes/b-周报.md', cache_docs())

    def test_parallel_path(self):
        # 文件数超过 PARALLEL_MIN_FILES(32)，触发 ProcessPoolExecutor 路径
        for i in range(40):
            self.write_note(f'p{i:02d}-会议纪要.md', NOTE_C + f'\n编号 {i}。\n')
        result = self.run_bm25('--build')
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = self.query_paths('品牌视觉', top_k=3)
        self.assertTrue(paths)

    def test_old_index_format_rejected(self):
        import pickle
        self.seed_three_notes()
        # 伪造无 index_version 的旧格式索引
        with gzip.open(self.vault / '.meta' / 'bm25_index.pkl.gz', 'wb') as f:
            pickle.dump({'k1': 1.5, 'b': 0.75, 'N': 1, 'avgdl': 1.0}, f)
        result = self.run_bm25('--query', '知识库维护')
        self.assertEqual(result.returncode, 0)
        self.assertIn('重建', result.stderr)


if __name__ == '__main__':
    unittest.main()
