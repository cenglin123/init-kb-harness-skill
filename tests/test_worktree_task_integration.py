"""Integration tests for the optional helper shared with init-agent-docs.

Set AGENT_DOCS_ASSETS to the upstream assets/scripts directory. Tests use a
temporary Git repository and its linked worktree; they never touch the host repo.
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


DOC_ASSETS = os.environ.get('AGENT_DOCS_ASSETS')


@unittest.skipUnless(DOC_ASSETS, 'set AGENT_DOCS_ASSETS for helper integration')
class WorktreeTaskIntegrationTests(unittest.TestCase):
    def setUp(self):
        source = Path(DOC_ASSETS) / 'worktree_task.py'
        if not source.is_file():
            self.skipTest('upstream worktree_task.py is not available')
        self.temp = tempfile.TemporaryDirectory(prefix='kb-worktree-bridge-')
        self.parent = Path(self.temp.name)
        self.root = self.parent / 'vault'
        self.root.mkdir()
        self.helper = self.root / '.meta' / 'scripts' / 'worktree_task.py'
        self.helper.parent.mkdir(parents=True)
        shutil.copyfile(source, self.helper)
        self.write('README.md', '# Fixture\n')
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('add', '.')
        self.git('commit', '-m', 'fixture')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def git(self, *args, cwd=None):
        result = subprocess.run(['git', *args], cwd=cwd or self.root,
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result.stdout.strip()

    def run_helper(self, action, *args, repo=None):
        helper = (Path(repo) if repo else self.root) / '.meta' / 'scripts' / 'worktree_task.py'
        command = [sys.executable, str(helper), action, *args,
                   '--repo', str(repo or self.root)]
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding='utf-8', timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_create_claim_resume_intent_and_read_only_views_share_common_registry(self):
        created = self.run_helper('create', '--name', 'Fixture task',
                                  '--owner', 'fixture-agent', repo=self.root)
        task_id = created['id']
        task_root = Path(created['path'])
        self.assertTrue(task_root.is_dir())

        common = Path(self.git('rev-parse', '--path-format=absolute', '--git-common-dir'))
        registry = common / 'worktree-task' / 'tasks'
        record = registry / f'{task_id}.json'
        self.assertTrue(record.is_file())
        self.assertEqual([record], sorted(registry.glob('*.json')))

        # create with an owner is the initial claim; resume validates that same
        # owner and generation without creating a second registry record.
        resumed = self.run_helper('resume', task_id, '--owner', 'fixture-agent',
                                  '--generation', '1', repo=task_root)
        self.assertEqual(task_id, resumed['id'])
        self.assertEqual('fixture-agent', resumed['owner'])
        self.assertEqual(1, resumed['generation'])

        intent = self.run_helper('intent', task_id, '--owner', 'fixture-agent',
                                 '--generation', '1', '--goal', 'Verify shared task state',
                                 '--non-goal', 'Modify the host repository',
                                 '--scope', 'isolated fixture', repo=task_root)
        self.assertEqual(1, intent['intent']['revision'])
        state_before_reads = record.read_bytes()

        for checkout in (self.root, task_root):
            listed = self.run_helper('list', repo=checkout)
            self.assertEqual([task_id], [row['id'] for row in listed['tasks']])
            reviewed = self.run_helper('review', task_id, '--owner', 'fixture-agent',
                                       '--generation', '1', repo=checkout)
            self.assertEqual(task_id, reviewed['id'])

        self.assertEqual([record], sorted(registry.glob('*.json')))
        self.assertEqual(record.read_bytes(), state_before_reads)
        self.assertEqual(1, json.loads(record.read_text(encoding='utf-8'))['intent']['revision'])


if __name__ == '__main__':
    unittest.main()
