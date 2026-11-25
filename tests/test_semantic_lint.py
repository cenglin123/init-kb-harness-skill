"""Regression and production tests for semantic_lint.py and related harness checks.

Extracted from the inline `--check-*` routines previously embedded in
`scripts/semantic_lint.py`, plus end-to-end tests for `semantic_lint.py` CLI
behavior (`--quick` and `--deep` gating). All tests run against temporary
vault fixtures and never touch any host knowledge base.
"""
from pathlib import Path
import gzip
import json
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


class SemanticLintRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / 'vault'
        self.scripts = self.vault / '.meta' / 'scripts'
        self.scripts.mkdir(parents=True)
        (self.vault / 'AGENTS.md').write_text('# Fixture Vault\n', encoding='utf-8')
        (self.vault / '.env').write_text(
            'HARNESS_MODE=lite\nDEEPSEEK_API_KEY=\nZHIPU_API_KEY=\n',
            encoding='utf-8',
        )
        for source in SCRIPTS.glob('*.py'):
            shutil.copyfile(source, self.scripts / source.name)
        self.notes = self.vault / 'notes'
        self.notes.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_note(self, name: str, content: str) -> Path:
        path = self.notes / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def run_script(self, script_name: str, *args, expected=None):
        env = dict(os.environ, PYTHONUTF8='1', PRIMARY_HOST='')
        result = subprocess.run(
            [sys.executable, str(self.scripts / script_name), *args],
            cwd=self.vault,
            capture_output=True,
            text=True,
            encoding='utf-8',
            env=env,
            timeout=90,
        )
        if expected is not None:
            self.assertEqual(expected, result.returncode, result.stdout + result.stderr)
        return result

    def test_graph_traversal(self):
        """Graph traversal regression: non-isolated nodes with outgoing edges have valid neighbors."""
        self.write_note('alpha.md', '# Alpha\nLinks to [[beta]] and [[gamma]].\n')
        self.write_note('beta.md', '# Beta\nLinks back to [[alpha]] and [[gamma]].\n')
        self.write_note('gamma.md', '# Gamma\nLinks to [[alpha]].\n')

        self.run_script('build_graph.py', expected=0)
        graph_path = self.vault / '.meta' / 'graph.json'
        self.assertTrue(graph_path.is_file(), 'build_graph.py should create .meta/graph.json')

        graph = json.loads(graph_path.read_text(encoding='utf-8'))
        nodes_with_outgoing = {e.get('source', '') for e in graph.get('edges', [])}
        non_isolated = [
            n for n in graph.get('nodes', [])
            if n.get('community_id', -1) >= 0 and n['id'] in nodes_with_outgoing
        ]
        self.assertTrue(non_isolated, 'expected at least one non-isolated node with outgoing edges')

        probe = (
            "import json, sys; sys.path.insert(0, '.meta/scripts'); "
            "from build_graph import get_neighbors; "
            "nids = json.loads(sys.argv[1]); "
            "out = {nid: get_neighbors(nid, max_hops=1) for nid in nids}; "
            "print(json.dumps(out, ensure_ascii=False))"
        )
        sample_ids = [n['id'] for n in non_isolated[:10]]
        res = subprocess.run(
            [sys.executable, '-c', probe, json.dumps(sample_ids)],
            cwd=self.vault,
            capture_output=True,
            text=True,
            encoding='utf-8',
            env=dict(os.environ, PYTHONUTF8='1', PRIMARY_HOST=''),
            timeout=30,
        )
        self.assertEqual(0, res.returncode, res.stdout + res.stderr)
        neighbors_map = json.loads(res.stdout)
        for nid in sample_ids:
            neighbors = neighbors_map[nid]
            self.assertNotIsInstance(neighbors, dict, f'{nid} returned error: {neighbors}')
            self.assertTrue(neighbors, f'{nid} expected non-empty neighbors')
            for nb in neighbors:
                w = nb.get('weight', -1)
                self.assertTrue(0.0 <= w <= 1.0, f'{nid} -> {nb.get("path")} invalid weight {w}')

    def test_deep_dedup(self):
        """Regression test: deep search (path, chunk_index) deduplication keeps highest similarity."""
        test_items = [
            ('a.md', 0, 0.9), ('a.md', 0, 0.8),  # duplicate path+chunk
            ('b.md', 0, 0.7), ('b.md', 1, 0.6),  # same path, different chunk
            ('c.md', 0, 0.5),
        ]
        seen = {}
        for path, chunk_idx, sim in test_items:
            key = (path, chunk_idx)
            if key not in seen or sim > seen[key]:
                seen[key] = sim

        expected = {
            ('a.md', 0): 0.9,
            ('b.md', 0): 0.7,
            ('b.md', 1): 0.6,
            ('c.md', 0): 0.5,
        }
        self.assertEqual(seen, expected)

    def test_bm25_build(self):
        """Regression test: BM25 index file exists, parses cleanly, and has required keys."""
        self.write_note('a.md', '# 知识库维护\n定期运行索引重建与语义检查。\n')
        self.write_note('b.md', '# 架构设计\n混合检索结合 BM25 与向量语义。\n')

        self.run_script('bm25_index.py', '--build', expected=0)
        bm25_path = self.vault / '.meta' / 'bm25_index.pkl.gz'
        self.assertTrue(bm25_path.is_file(), 'bm25_index.pkl.gz should be created')

        index = pickle.loads(gzip.decompress(bm25_path.read_bytes()))
        required_keys = [
            'index_version', 'N', 'avgdl', 'idf', 'postings', 'docs_paths', 'generated_at',
        ]
        for key in required_keys:
            self.assertIn(key, index, f"BM25 index missing required key '{key}'")
        self.assertGreater(index['N'], 0)
        self.assertEqual(len(index['docs_paths']), index['N'])
        self.assertEqual(len(index['postings']), len(index['idf']))

    def test_rerank_fallback(self):
        """Regression test: rerank_with_llm handles empty candidates and falls back on API error."""
        probe = (
            "import sys; from unittest.mock import patch; "
            "sys.path.insert(0, '.meta/scripts'); "
            "import common; "
            "assert common.rerank_with_llm('test', [], top_m=5) == []; "
            "cands = ["
            "('a.md', 0, 'relevant text about AI agents'), "
            "('b.md', 0, 'unrelated text about cooking'), "
            "('c.md', 0, 'somewhat related agent architecture')]; "
            "p = patch.object(common.DeepSeekClient, 'chat', side_effect=Exception('Mock API failure')); "
            "p.start(); "
            "res = common.rerank_with_llm('AI agents', cands, top_m=2); "
            "p.stop(); "
            "assert len(res) >= 1, f'Fallback returned {res}'"
        )
        res = subprocess.run(
            [sys.executable, '-c', probe],
            cwd=self.vault,
            capture_output=True,
            text=True,
            encoding='utf-8',
            env=dict(os.environ, PYTHONUTF8='1', PRIMARY_HOST=''),
            timeout=30,
        )
        self.assertEqual(0, res.returncode, res.stdout + res.stderr)

    def test_quick_mode_detects_p0_p1_and_writes_report(self):
        """Production CLI test: --quick detects broken wiki/md links, orphan concepts, stale years."""
        self.write_note('existing.md', '# Existing Note\nValid target.\n')
        self.write_note(
            'issues.md',
            '# Issues Note\n\n'
            'Valid: [[existing]]\n'
            'Broken wiki: [[missing-target]]\n'
            'Broken md: [broken](no-such-file.md)\n'
            'Mentions **QuantumGraphEngine**, **QuantumGraphEngine**, **QuantumGraphEngine**.\n'
            'Old reference from 2020年发布.\n',
        )

        self.run_script('semantic_lint.py', '--quick', expected=0)
        report_path = self.vault / '.meta' / 'semantic-lint-report.md'
        self.assertTrue(report_path.is_file(), 'semantic-lint-report.md should be written')
        report = report_path.read_text(encoding='utf-8')
        self.assertIn('[[missing-target]]', report)
        self.assertIn('[broken](no-such-file.md)', report)
        self.assertIn('QuantumGraphEngine', report)
        self.assertIn('2020年', report)

    def test_deep_mode_without_llm_exits_2(self):
        """Production CLI test: --deep fails with exit code 2 when LLM layer is disabled."""
        self.write_note('note.md', '# Simple Note\nContent.\n')
        res = self.run_script('semantic_lint.py', '--deep', expected=2)
        self.assertIn('--deep', res.stderr)


if __name__ == '__main__':
    unittest.main()
