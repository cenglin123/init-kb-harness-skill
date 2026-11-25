"""Tests for docs/plans/ directory conventions and check_plan_status.py."""
from pathlib import Path
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / 'scripts' / 'check_plan_status.py'
POLICY_PATH = REPO_ROOT / 'templates' / 'host-write-policy.json'


def _load_module():
    spec = importlib.util.spec_from_file_location('check_plan_status', SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_plan_status = _load_module()


class PlanStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kb-plan-status-')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_plan(self, relative: str, content: str) -> Path:
        path = self.root / 'docs' / 'plans' / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def test_valid_statuses_across_active_completed_done_and_not_planned(self):
        self.write_plan('active/p1.md', '---\nstatus: active\n---\n# Active plan\n- [ ] todo\n')
        self.write_plan('active/p2.md', '---\nstatus: in_progress\n---\n# In progress plan\n- [ ] todo\n')
        self.write_plan('active/backlog.md', '# Backlog without frontmatter\n')
        self.write_plan('completed/c1.md', '---\nstatus: done\n---\n# Canonical completed\n')
        self.write_plan('completed/c2.md', '---\nstatus: completed\n---\n# Completed status\n')
        self.write_plan('done/legacy.md', '---\nstatus: done\n---\n# Legacy done dir\n')
        self.write_plan('not_planned/n1.md', '---\nstatus: not_planned\n---\n# Not planned\n')
        self.write_plan('not_planned/n2.md', '---\nstatus: cancelled\n---\n# Cancelled\n')

        self.assertEqual([], check_plan_status.check(self.root))

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = check_plan_status.main(['--root', str(self.root)])
        self.assertEqual(0, rc, buf.getvalue())

    def test_mismatched_and_missing_statuses_are_reported(self):
        bad_active = self.write_plan('active/bad.md', '---\nstatus: done\n---\n# Wrong\n')
        bad_completed = self.write_plan('completed/bad.md', '---\nstatus: active\n---\n# Wrong\n')
        bad_done = self.write_plan('done/bad.md', '---\nstatus: active\n---\n# Wrong\n')
        bad_np = self.write_plan('not_planned/bad.md', '---\nstatus: active\n---\n# Wrong\n')
        missing_fm = self.write_plan('completed/missing.md', '# No frontmatter\n')

        mismatches = check_plan_status.check(self.root)
        by_path = {m.path: m for m in mismatches}
        self.assertEqual(
            {bad_active, bad_completed, bad_done, bad_np, missing_fm},
            set(by_path.keys()),
        )
        self.assertIsNone(by_path[missing_fm].actual)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = check_plan_status.main(['--root', str(self.root)])
        self.assertEqual(1, rc)
        self.assertIn('docs/plans/completed/bad.md', buf.getvalue())
        self.assertIn('docs/plans/done/bad.md', buf.getvalue())
        self.assertIn('docs/plans/not_planned/bad.md', buf.getvalue())

    def test_archive_ready_recommends_completed_dir(self):
        ready_active = self.write_plan(
            'active/ready-active.md',
            '---\nstatus: active\n---\n# Ready\n- [x] step 1\n- [x] step 2\n',
        )
        ready_in_progress = self.write_plan(
            'active/ready-in-progress.md',
            '---\nstatus: in_progress\n---\n# Ready\n- [x] step 1\n',
        )
        self.write_plan(
            'active/still-open.md',
            '---\nstatus: active\n---\n# Open\n- [x] step 1\n- [ ] step 2\n',
        )
        self.write_plan(
            'active/backlog.md',
            '- [x] done item\n',
        )

        ready = check_plan_status.check_archive_ready(self.root)
        self.assertEqual([ready_active, ready_in_progress], ready)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = check_plan_status.main(['--root', str(self.root), '--archive-check'])
        self.assertEqual(0, rc)
        out = buf.getvalue()
        self.assertIn('docs/plans/completed/', out)
        self.assertIn('ready-active.md', out)
        self.assertIn('ready-in-progress.md', out)

    def test_host_write_policy_protects_completed_legacy_done_and_not_planned(self):
        policy = json.loads(POLICY_PATH.read_text(encoding='utf-8'))
        rules = {rule['name']: rule for rule in policy['deny_rules']}
        archive_paths = rules['plans-readme-and-archive']['paths']
        self.assertIn('docs/plans/completed/**', archive_paths)
        self.assertIn('docs/plans/done/**', archive_paths)
        self.assertIn('docs/plans/not_planned/**', archive_paths)
        self.assertIn('docs/plans/README.md', archive_paths)


if __name__ == '__main__':
    unittest.main()
