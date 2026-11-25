"""Batch 3 性能优化单元测试：
1. common.ScanCache：命中/未命中/size 与 mtime_ns 失效/损坏恢复/过期清理
2. bm25_index._load_index：进程内缓存命中与 mtime 变更失效
3. build_links.compute_top_k_neighbors：分块向量化 + argpartition 与朴素参考实现数值等价
4. dream.py：单次 git log 批量扫描（mock subprocess 断言 <= 1 次 git 调用）及非 git 仓回退
"""

import gzip
import importlib
import json
import os
import pickle
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import numpy as np

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / 'scripts'


def _load_scripts_for_vault(vault_dir: Path):
    """在临时 vault 上加载脚本模块，避免依赖宿主目录结构。"""
    os.environ['VAULT_ROOT'] = str(vault_dir)
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    for name in ('common', 'embed', 'bm25_index', 'build_links', 'dream'):
        sys.modules.pop(name, None)
    common = importlib.import_module('common')
    bm25_index = importlib.import_module('bm25_index')
    build_links = importlib.import_module('build_links')
    dream = importlib.import_module('dream')
    return common, bm25_index, build_links, dream


class PerfOptimizationsTests(unittest.TestCase):
    def setUp(self):
        self._orig_vault_root = os.environ.get('VAULT_ROOT')
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / 'vault'
        (self.vault / '.meta').mkdir(parents=True)
        (self.vault / 'AGENTS.md').write_text('# test vault\n', encoding='utf-8')
        self.common, self.bm25, self.build_links, self.dream = _load_scripts_for_vault(self.vault)

    def tearDown(self):
        if self._orig_vault_root is None:
            os.environ.pop('VAULT_ROOT', None)
        else:
            os.environ['VAULT_ROOT'] = self._orig_vault_root
        self.tmp.cleanup()

    # ── 1. ScanCache 测试 ──────────────────────────────────────────────────

    def test_scan_cache_hit_miss_invalidation_and_prune(self):
        notes = self.vault / 'notes'
        notes.mkdir()
        note_a = notes / 'a.md'
        note_b = notes / 'b.md'
        note_a.write_text('hello world', encoding='utf-8')
        note_b.write_text('second note', encoding='utf-8')

        # 首次冷启动：计算并落盘
        cache1 = self.common.ScanCache()
        sha_a = cache1.get_sha256(note_a)
        sha_b = cache1.get_sha256(note_b)
        self.assertEqual(len(sha_a), 64)
        self.assertEqual(cache1.get_content_hash(note_a), self.common.content_hash(note_a))
        self.assertTrue(cache1.save())
        self.assertTrue((self.vault / '.meta' / 'scan-cache.json').exists())

        # 二次加载：完全命中，不触发磁盘哈希计算
        cache2 = self.common.ScanCache()
        with mock.patch.object(self.common, '_sha256_file', side_effect=AssertionError('should hit cache')):
            self.assertEqual(cache2.get_sha256(note_a), sha_a)
            self.assertEqual(cache2.get_sha256(note_b), sha_b)

        # size 变化 → 失效重算
        note_a.write_text('hello world updated longer content', encoding='utf-8')
        sha_a2 = cache2.get_sha256(note_a)
        self.assertNotEqual(sha_a2, sha_a)

        # 同长度但 mtime_ns 变化 → 失效重算
        note_b.write_text('SECOND NOTE', encoding='utf-8')
        st_b = note_b.stat()
        os.utime(note_b, ns=(st_b.st_atime_ns, st_b.st_mtime_ns + 1_000_000_000))
        sha_b2 = cache2.get_sha256(note_b)
        self.assertNotEqual(sha_b2, sha_b)

        # 删除 note_b 后 save() 应自动清理过期条目
        note_b.unlink()
        self.assertTrue(cache2.save())
        raw = json.loads((self.vault / '.meta' / 'scan-cache.json').read_text(encoding='utf-8'))
        self.assertIn('notes/a.md', raw)
        self.assertNotIn('notes/b.md', raw)

    def test_scan_cache_corrupt_file_recovery(self):
        cache_path = self.vault / '.meta' / 'scan-cache.json'
        cache_path.write_text('{corrupt json!!!', encoding='utf-8')

        note = self.vault / 'notes' / 'c.md'
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text('recover me', encoding='utf-8')

        cache = self.common.ScanCache()
        self.assertEqual(cache.entries, {})
        h = cache.get_sha256(note)
        self.assertEqual(h[:16], self.common.content_hash(note))
        self.assertTrue(cache.save())

        # 修复后应是合法 JSON
        repaired = json.loads(cache_path.read_text(encoding='utf-8'))
        self.assertIn('notes/c.md', repaired)
        self.assertEqual(repaired['notes/c.md']['sha256'], h)

    # ── 2. BM25 进程内缓存测试 ─────────────────────────────────────────────

    def test_bm25_load_index_cache_hit_and_mtime_invalidation(self):
        idx_path = self.bm25.BM25_INDEX_PATH
        idx_path.parent.mkdir(parents=True, exist_ok=True)

        def write_dummy_index(marker_n):
            payload = {
                'index_version': self.bm25.INDEX_VERSION,
                'k1': 1.5,
                'b': 0.75,
                'N': marker_n,
                'avgdl': 10.0,
                'term_to_id': {'test': 0},
                'idf': [1.0],
                'postings': [[[0, 1]]],
                'docs_paths': ['notes/a.md'],
                'docs_chunks': [0],
                'docs_lens': [10],
            }
            with gzip.open(idx_path, 'wb') as f:
                pickle.dump(payload, f, protocol=4)

        self.bm25._INDEX_CACHE.clear()
        write_dummy_index(1)

        # 第一次加载走磁盘
        idx1 = self.bm25._load_index()
        self.assertIsNotNone(idx1)
        self.assertEqual(idx1['N'], 1)

        # 第二次加载应命中进程内缓存，不调用 gzip.open
        with mock.patch.object(self.bm25.gzip, 'open', side_effect=AssertionError('should use in-process cache')):
            idx2 = self.bm25._load_index()
        self.assertIs(idx2, idx1)

        # 更新磁盘文件并推进 mtime_ns → 缓存失效并重载
        write_dummy_index(2)
        st = idx_path.stat()
        os.utime(idx_path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))

        idx3 = self.bm25._load_index()
        self.assertIsNotNone(idx3)
        self.assertEqual(idx3['N'], 2)
        self.assertIsNot(idx3, idx1)

        # 删除索引文件后返回 None，不返回陈旧缓存
        idx_path.unlink()
        self.assertIsNone(self.bm25._load_index())

    # ── 3. build_links 向量化等价性测试 ────────────────────────────────────

    def test_build_links_vectorized_matches_naive_reference(self):
        rng = np.random.default_rng(42)
        n, dim = 120, 64
        mat = rng.standard_normal((n, dim)).astype(np.float32)
        mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12)
        paths = [f'notes/doc_{i:03d}.md' for i in range(n)]
        top_k = 8
        threshold = 0.15

        # 朴素参考实现（原循环逻辑）
        def naive_neighbors(query_indices):
            out = []
            for idx in query_indices:
                sims = mat @ mat[idx]
                order = np.argsort(-sims)[:top_k + 1]
                related = []
                for j in order:
                    if j == idx:
                        continue
                    s = float(sims[j])
                    if s >= threshold:
                        related.append((paths[int(j)], s))
                    if len(related) >= top_k:
                        break
                out.append(related)
            return out

        query_indices = list(range(n))
        expected = naive_neighbors(query_indices)
        # 用较小的 block_size=17 强制覆盖多分块路径
        actual = self.build_links.compute_top_k_neighbors(
            mat, query_indices, paths, top_k=top_k, threshold=threshold, block_size=17
        )

        self.assertEqual(len(actual), len(expected))
        for i, (exp_row, act_row) in enumerate(zip(expected, actual)):
            self.assertEqual(
                [p for p, _ in act_row],
                [p for p, _ in exp_row],
                f'neighbor paths mismatch at row {i}',
            )
            for (_, exp_s), (_, act_s) in zip(exp_row, act_row):
                self.assertAlmostEqual(act_s, exp_s, places=5)

        # 边界情况：空查询与单文档
        self.assertEqual(self.build_links.compute_top_k_neighbors(mat, [], paths), [])
        self.assertEqual(self.build_links.compute_top_k_neighbors(mat[:1], [0], paths[:1]), [[]])

    # ── 4. dream.py 单次 git 扫描与回退测试 ────────────────────────────────

    def test_dream_single_git_call_for_archive_and_memory(self):
        mem_root = self.vault / '.meta' / 'memory'
        archive_dir = mem_root / '.archive'
        fb_dir = mem_root / 'feedback'
        proj_dir = mem_root / 'projects'
        for d in (archive_dir, fb_dir, proj_dir):
            d.mkdir(parents=True, exist_ok=True)

        # 多个归档文件 + 缺日期的 feedback/project 文件
        for i in range(5):
            (archive_dir / f'old_{i}.md').write_text(f'# archived {i}\n', encoding='utf-8')
        (fb_dir / 'fb1.md').write_text('---\ntype: feedback\nliveness: active\n---\nbody\n', encoding='utf-8')
        (proj_dir / 'p1.md').write_text('---\ntype: project\nstatus: complete\n---\nbody\n', encoding='utf-8')

        ts = int(datetime(2025, 1, 15, 12, 0, 0).timestamp())
        fake_stdout = (
            f"\x01{ts}\n"
            ".meta/memory/.archive/old_0.md\n"
            ".meta/memory/.archive/old_3.md\n"
            ".meta/memory/feedback/fb1.md\n"
            ".meta/memory/projects/p1.md\n"
        )
        fake_proc = mock.Mock(returncode=0, stdout=fake_stdout, stderr='')

        with mock.patch('subprocess.run', return_value=fake_proc) as mocked_run:
            self.dream.main()
            self.assertLessEqual(mocked_run.call_count, 1)
            self.assertEqual(mocked_run.call_count, 1)

        report = (self.vault / '.meta' / 'dream-report.md').read_text(encoding='utf-8')
        self.assertIn('.meta/memory/.archive/old_0.md', report)
        self.assertIn('.meta/memory/.archive/old_3.md', report)
        self.assertIn('2025-01-15', report)

        # 独立调用 detect_wake() 也只触发 <= 1 次 git 调用
        with mock.patch('subprocess.run', return_value=fake_proc) as mocked_run:
            wakes = self.dream.detect_wake()
            self.assertLessEqual(mocked_run.call_count, 1)
            self.assertEqual(len(wakes), 2)

    def test_dream_non_git_repo_fallback(self):
        mem_root = self.vault / '.meta' / 'memory'
        archive_dir = mem_root / '.archive'
        proj_dir = mem_root / 'projects'
        archive_dir.mkdir(parents=True, exist_ok=True)
        proj_dir.mkdir(parents=True, exist_ok=True)

        (archive_dir / 'a1.md').write_text('# a1\n', encoding='utf-8')
        (archive_dir / 'a2.md').write_text('# a2\n', encoding='utf-8')
        (proj_dir / 'p1.md').write_text('---\ntype: project\nstatus: complete\n---\nbody\n', encoding='utf-8')

        fail_proc = mock.Mock(returncode=128, stdout='', stderr='fatal: not a git repository')
        with mock.patch('subprocess.run', return_value=fail_proc) as mocked_run:
            self.dream.main()
            self.assertLessEqual(mocked_run.call_count, 1)

        # 非 git 仓下报告照常生成，detect_wake 返回空列表
        self.assertTrue((self.vault / '.meta' / 'dream-report.md').exists())
        with mock.patch('subprocess.run', return_value=fail_proc) as mocked_run:
            self.assertEqual(self.dream.detect_wake(), [])
            self.assertLessEqual(mocked_run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
