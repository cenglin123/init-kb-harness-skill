"""Integration tests for the optional helper shared with init-agent-docs.

Resolves the upstream assets/scripts directory from AGENT_DOCS_ASSETS or a
sibling init-agent-docs checkout. Tests use a temporary Git repository and its
linked worktree; they never touch the host repo.
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


def _resolve_doc_assets() -> Path | None:
    env_dir = os.environ.get('AGENT_DOCS_ASSETS')
    if env_dir:
        return Path(env_dir)
    sibling = Path(__file__).resolve().parents[2] / 'init-agent-docs' / 'assets' / 'scripts'
    if (sibling / 'worktree_task.py').is_file():
        return sibling
    return None


DOC_ASSETS = _resolve_doc_assets()


@unittest.skipUnless(DOC_ASSETS, 'set AGENT_DOCS_ASSETS or place init-agent-docs as a sibling repo')
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

    def write(self, relative, text, root=None):
        path = (Path(root) if root else self.root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def git(self, *args, cwd=None):
        result = subprocess.run(['git', *args], cwd=cwd or self.root,
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result.stdout.strip()

    def run_helper_raw(self, action, *args, repo=None):
        helper = (Path(repo) if repo else self.root) / '.meta' / 'scripts' / 'worktree_task.py'
        command = [sys.executable, str(helper), action, *args,
                   '--repo', str(repo or self.root)]
        return subprocess.run(command, capture_output=True, text=True,
                              encoding='utf-8', timeout=30)

    def run_helper(self, action, *args, repo=None, expected_rc=0):
        result = self.run_helper_raw(action, *args, repo=repo)
        self.assertEqual(expected_rc, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_eight_subcommand_lifecycle_and_no_registry(self):
        # Initial main-claim is unclaimed and read-only calls create no state files.
        common = Path(self.git('rev-parse', '--path-format=absolute', '--git-common-dir'))
        state_dir = common / 'worktree-task'
        self.assertEqual({'claimed': False}, self.run_helper('main-claim'))
        self.assertFalse(state_dir.exists())

        # Create a linked task worktree; no tasks/<id>.json registry is created.
        created = self.run_helper('create', '--name', 'Fixture task',
                                  '--owner', 'fixture-agent', repo=self.root)
        self.assertEqual('created', created['result'])
        task_id = created['id']
        task_root = Path(created['path'])
        self.assertTrue(task_root.is_dir())
        self.assertFalse((state_dir / 'tasks').exists())

        # Read-only list / check / main-claim agree across canonical and linked worktrees.
        wt_root = str(task_root.parent)
        for checkout in (self.root, task_root):
            listed = self.run_helper('list', repo=checkout)
            self.assertEqual('ok', listed['result'])
            self.assertEqual(1, len(listed['tasks']))
            self.assertTrue(listed['tasks'][0]['branch'].startswith(f'task/{task_id}-'))
            check_args = (task_id,) if checkout == self.root else (task_id, '--wt-root', wt_root)
            checked = self.run_helper('check', *check_args, repo=checkout)
            self.assertEqual('ok', checked['result'])
            self.assertTrue(checked['registered'])
            self.assertIsNone(checked['reg_error'])
            self.assertTrue(checked['clean'])
            self.assertEqual(0, checked['ahead'])
            self.assertEqual(0, checked['behind'])
            self.assertEqual({'claimed': False}, self.run_helper('main-claim', repo=checkout))
        self.assertFalse((state_dir / 'main-claim.json').exists())

        # Commit a change inside the task worktree.
        self.write('notes/from-task.md', '# Task note\n', root=task_root)
        self.git('add', 'notes/from-task.md', cwd=task_root)
        self.git('commit', '-m', 'task change', cwd=task_root)
        task_tip = self.git('rev-parse', 'HEAD', cwd=task_root)

        checked = self.run_helper('check', task_id, repo=self.root)
        self.assertEqual(1, checked['ahead'])
        self.assertEqual(0, checked['behind'])
        self.assertFalse(checked['integrated'])

        # Active main-claim is shared across worktrees, refuses force-takeover,
        # and mechanically blocks integrate until released by its owner.
        claimed = self.run_helper('claim-main', '--owner', 'primary', repo=self.root)
        self.assertEqual('ok', claimed['result'])
        self.assertFalse(claimed['renewed'])
        self.assertTrue((state_dir / 'main-claim.json').is_file())

        renewed = self.run_helper('claim-main', '--owner', 'primary', repo=task_root)
        self.assertEqual('ok', renewed['result'])
        self.assertTrue(renewed['renewed'])

        for checkout in (self.root, task_root):
            status = self.run_helper('main-claim', repo=checkout)
            self.assertTrue(status['claimed'])
            self.assertEqual('primary', status['owner'])

        refused_claim = self.run_helper('claim-main', '--owner', 'other', '--force',
                                        repo=self.root, expected_rc=1)
        self.assertEqual('refused', refused_claim['result'])
        self.assertEqual('primary', refused_claim['holder'])

        refused_release = self.run_helper('release-main', '--owner', 'other',
                                          repo=self.root, expected_rc=1)
        self.assertEqual('refused', refused_release['result'])

        blocked_integrate = self.run_helper('integrate', task_id, repo=self.root, expected_rc=1)
        self.assertEqual('refused', blocked_integrate['result'])
        self.assertEqual('primary', blocked_integrate['holder'])

        released = self.run_helper('release-main', '--owner', 'primary', repo=self.root)
        self.assertEqual('ok', released['result'])
        self.assertTrue(released['released'])
        self.assertEqual({'claimed': False}, self.run_helper('main-claim', repo=self.root))

        # Integrate fast-forwards canonical, retry is idempotent, cleanup removes worktree + branch.
        integrated = self.run_helper('integrate', task_id, repo=self.root)
        self.assertEqual('integrated', integrated['result'])
        self.assertEqual(task_tip, integrated['canonical_tip'])

        already = self.run_helper('integrate', task_id, repo=self.root)
        self.assertEqual('already-integrated', already['result'])

        cleaned = self.run_helper('cleanup', task_id, repo=self.root)
        self.assertEqual('cleaned', cleaned['result'])
        self.assertFalse(task_root.exists())
        self.assertEqual([], self.run_helper('list', repo=self.root)['tasks'])

        already_cleaned = self.run_helper('cleanup', task_id, repo=self.root)
        self.assertEqual('already-cleaned', already_cleaned['result'])

    def test_removed_legacy_subcommands_are_rejected(self):
        for legacy_action in ('resume', 'intent', 'review'):
            result = self.run_helper_raw(legacy_action, 'dummy', repo=self.root)
            self.assertNotEqual(0, result.returncode, f'{legacy_action} should not be accepted')


if __name__ == '__main__':
    unittest.main()
