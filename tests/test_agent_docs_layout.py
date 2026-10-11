"""Cross-skill smoke tests; set AGENT_DOCS_ASSETS to the upstream assets/scripts."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

KB_SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
DOC_ASSETS = os.environ.get('AGENT_DOCS_ASSETS')


@unittest.skipUnless(DOC_ASSETS, 'set AGENT_DOCS_ASSETS for cross-skill integration')
class AgentDocsLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / '知识库'
        self.scripts = self.root / '.meta' / 'scripts'
        self.scripts.mkdir(parents=True)
        for source in KB_SCRIPTS.glob('*.py'):
            if source.name != 'changelog_append.py':
                shutil.copyfile(source, self.scripts / source.name)
        for name in ('changelog.py', 'audit.py', 'check_all.py', 'maintain.py'):
            shutil.copyfile(Path(DOC_ASSETS) / name, self.scripts / ('docs_maintain.py' if name == 'maintain.py' else name))
        self.write('AGENTS.md', '# Knowledge base\n## 项目记忆\n.meta/memory/MEMORY.md\n')
        self.write('.env', 'HARNESS_MODE=lite\nDEEPSEEK_API_KEY=\nZHIPU_API_KEY=\n')
        self.write('docs/initialization.md', '# Initialization\n')
        self.write('docs/CHANGELOG.md', '# CHANGELOG\n\n## 2026-10-04\n\n### Initialization\n\n- Fixture\n')
        self.write('docs/problems/bugfix/retained.md', '# Retained bugfix\nUseful finding.\n')
        self.write('docs/problems/bugfix/_template.md', '# Not an experience\n')
        self.write('.meta/memory/MEMORY.md', '# Memory\nFree text remains.\n')
        self.write('.meta/memory/user/role.md', '# User\nPreserved profile.\n')
        self.write('notes/hello.md', '---\ntitle: Hello\ntags: [fixture]\n---\n# Hello\nTest note.\n')

    def write(self, relative, text):
        p = self.root / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode('utf-8'))

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, name, *args, expected=0):
        result = subprocess.run([sys.executable, str(self.scripts / name), *args], cwd=self.root,
                                capture_output=True, text=True, encoding='utf-8',
                                env=dict(os.environ, PYTHONUTF8='1', PRIMARY_HOST=''), timeout=90)
        self.assertEqual(expected, result.returncode, result.stdout + result.stderr)
        return result.stdout

    def test_single_index_keeps_bugfix_and_free_text_and_is_idempotent(self):
        self.run_tool('memory_index.py')
        index = self.root / '.meta' / 'memory' / 'MEMORY.md'
        before = index.read_bytes()
        self.assertIn(b'Free text remains.', before)
        self.assertIn(b'../../docs/problems/bugfix/retained.md', before)
        self.assertNotIn(b'_template.md', before)
        self.run_tool('docs_maintain.py', '--check')
        self.run_tool('docs_maintain.py', '--memory-index')
        self.run_tool('memory_index.py', '--check')
        self.assertEqual(before, index.read_bytes())
        self.run_tool('check_all.py', '--quiet')

    def test_lite_full_pipeline_includes_docs_audit(self):
        output = self.run_tool('maintain.py', '--full', '--no-git', '--skip-changelog')
        self.assertIn('audit.py check (exit 0)', output)
        self.run_tool('memory_index.py', '--check')
        self.assertFalse((self.root / 'scripts').exists())
        self.assertFalse((self.root / '.agents' / 'memory').exists())

    def test_lite_pipeline_propagates_docs_audit_failure(self):
        self.write('AGENTS.md', '# Missing memory pointer\n')
        output = self.run_tool('maintain.py', '--full', '--no-git', '--skip-changelog', expected=1)
        self.assertIn('audit.py check (exit 1)', output)

    def test_missing_memory_is_a_failure_in_index_and_pipeline(self):
        shutil.rmtree(self.root / '.meta' / 'memory')
        self.run_tool('memory_index.py', '--check', expected=1)
        self.run_tool('memory_index.py', expected=1)
        self.run_tool('maintain.py', '--full', '--no-git', '--skip-changelog', expected=1)

    def test_special_bugfix_filename_survives_index_and_audit(self):
        self.write('docs/problems/bugfix/修复(Windows) [经验].md', '# Preserved special filename\n')
        self.run_tool('memory_index.py')
        self.run_tool('audit.py', 'memory')
        self.run_tool('docs_maintain.py', '--check')

    def test_real_hook_blocks_stale_index_for_bugfix_add_delete_and_rename(self):
        bash = os.environ.get('TEST_BASH') or shutil.which('bash')
        self.assertIsNotNone(bash, 'Bash is required for actual hook verification')
        self.write('.githooks/pre-commit', (KB_SCRIPTS.parent / 'refs' / 'pre-commit-template').read_text(encoding='utf-8'))
        def git(*args):
            result = subprocess.run(['git', '-c', 'core.quotepath=false', *args], cwd=self.root, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)
        git('init')
        self.run_tool('memory_index.py')
        git('add', '.')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            '-c', 'core.hooksPath=.unused-hooks', 'commit', '-m', 'fixture')
        def hook(expected):
            env = dict(os.environ, PYTHONUTF8='1', PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
            result = subprocess.run([bash, '.githooks/pre-commit'], cwd=self.root, env=env,
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(expected, result.returncode, result.stdout + result.stderr)
        self.write('docs/problems/bugfix/新经验(Win).md', '# New finding\n')
        git('add', 'docs/problems/bugfix')
        hook(1)
        self.run_tool('memory_index.py')
        git('add', '.meta/memory/MEMORY.md')
        hook(0)
        (self.root / 'docs/problems/bugfix/retained.md').unlink()
        git('add', 'docs/problems/bugfix')
        hook(1)
        self.run_tool('memory_index.py')
        git('add', '.meta/memory/MEMORY.md')
        hook(0)
        (self.root / 'docs/problems/bugfix/新经验(Win).md').rename(self.root / 'docs/problems/bugfix/重命名.md')
        git('add', 'docs/problems/bugfix')
        hook(1)
        self.run_tool('memory_index.py')
        git('add', '.meta/memory/MEMORY.md')
        hook(0)

    def test_kb_maintenance_reuses_relocated_changelog_tool(self):
        program = (
            "import sys; sys.path.insert(0, '.meta/scripts'); "
            "from maintain import _insert_changelog_entry_safe; from datetime import datetime; "
            "from pathlib import Path; "
            "assert _insert_changelog_entry_safe(Path('docs/CHANGELOG.md'), datetime(2026,10,4), "
            "True, ['Preserved maintenance logging'], False)"
        )
        result = subprocess.run([sys.executable, '-c', program], cwd=self.root, capture_output=True,
                                text=True, encoding='utf-8', env=dict(os.environ, PYTHONUTF8='1'))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('Preserved maintenance logging', (self.root / 'docs/CHANGELOG.md').read_text(encoding='utf-8'))
        self.assertFalse((self.scripts / 'changelog_append.py').exists())


if __name__ == '__main__':
    unittest.main()
