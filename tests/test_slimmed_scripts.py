"""Regression tests for slimmed scripts/whoami.py and scripts/changelog_append.py."""
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
WHOAMI_SCRIPT = REPO_ROOT / 'scripts' / 'whoami.py'
CHANGELOG_SCRIPT = REPO_ROOT / 'scripts' / 'changelog_append.py'


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


whoami = _load_module('whoami_under_test', WHOAMI_SCRIPT)


def _clean_whoami_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith('ANTHROPIC_')
        and k not in {
            'CODEX_THREAD_ID', 'CLAUDECODE', 'OPENCODE_BIN_PATH',
            'CC_SWITCH_DB', 'WHOAMI_AGENT', 'PRIMARY_HOST', 'VAULT_ROOT',
        }
    }
    env['PYTHONUTF8'] = '1'
    if extra:
        env.update(extra)
    return env


class WhoamiUnitTests(unittest.TestCase):
    def test_detect_harness_priority(self):
        with mock.patch.dict(os.environ, {
            'CODEX_THREAD_ID': 't-1',
            'CLAUDECODE': '1',
            'OPENCODE_BIN_PATH': '/bin/opencode',
        }, clear=True):
            self.assertEqual(whoami.detect_harness(), 'codex')

        with mock.patch.dict(os.environ, {
            'CLAUDECODE': '1',
            'OPENCODE_BIN_PATH': '/bin/opencode',
        }, clear=True):
            self.assertEqual(whoami.detect_harness(), 'claude-code')

        with mock.patch.dict(os.environ, {'OPENCODE_BIN_PATH': '/bin/opencode'}, clear=True):
            self.assertEqual(whoami.detect_harness(), 'opencode')

    def test_select_session_model_cases(self):
        now = 1_000_000
        rows = [
            {'id': '1', 'directory': 'C:/Work/Vault', 'time_updated': now - 10_000, 'model': 'm-cur'},
            {'id': '2', 'directory': 'C:/Other/Repo', 'time_updated': now - 1_000, 'model': 'm-other'},
        ]
        # Windows backslash + case-insensitive match isolates cross-dir concurrency
        model, degraded, reason = whoami.select_session_model(rows, r'c:\work\vault', now)
        self.assertEqual((model, degraded, reason), ('m-cur', False, ''))

        # Same-dir concurrency -> degraded
        rows_conc = rows + [
            {'id': '3', 'directory': 'C:/Work/Vault', 'time_updated': now - 5_000, 'model': 'm-cur-2'},
        ]
        model, degraded, reason = whoami.select_session_model(rows_conc, 'C:/Work/Vault', now)
        self.assertEqual((model, degraded, reason), ('m-cur-2', True, 'same-dir concurrency'))

        # Stale in cwd -> degraded
        rows_stale = [
            {'id': '1', 'directory': 'C:/Work/Vault', 'time_updated': now - 300_000, 'model': 'm-old'},
        ]
        model, degraded, reason = whoami.select_session_model(rows_stale, 'C:/Work/Vault', now)
        self.assertEqual((model, degraded, reason), ('m-old', True, 'stale, no active session in window'))

        # No session in cwd -> global fallback & empty fallback
        model, degraded, reason = whoami.select_session_model(rows_stale, 'C:/Missing', now)
        self.assertEqual((model, degraded, reason), ('m-old', True, 'no session in cwd'))
        self.assertEqual(whoami.select_session_model([], 'C:/Missing', now), (None, True, 'no session in cwd'))

    def test_ccswitch_sqlite_and_provider_precedence(self):
        with tempfile.TemporaryDirectory(prefix='kb-whoami-') as td:
            db = Path(td) / 'cc-switch.db'
            con = sqlite3.connect(db)
            con.execute(
                'CREATE TABLE providers (app_type TEXT, is_current INT, name TEXT, '
                'category TEXT, settings_config TEXT, api_key TEXT)'
            )
            con.execute(
                'INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)',
                ('claude', 1, 'Claude Official', 'official', json.dumps({'model': 'opus', 'apiKey': 'secret'}), 'sk-secret'),
            )
            con.commit()
            con.close()

            with mock.patch.dict(os.environ, {'CC_SWITCH_DB': str(db)}, clear=False):
                cc = whoami.read_ccswitch_active('claude')
            self.assertEqual(cc, {'name': 'Claude Official', 'category': 'official', 'model': 'opus'})
            self.assertEqual(whoami.ccswitch_identity(cc), 'claude-opus')
            self.assertIn('Anthropic', whoami.detect_provider('', 'deepseek-v3', cc))
            self.assertEqual(whoami.detect_provider('https://api.deepseek.com', 'claude', cc), 'DeepSeek')

    def test_opencode_config_whitelist_and_fallback(self):
        with tempfile.TemporaryDirectory(prefix='kb-opencode-') as td:
            root = Path(td)
            state_dir = root / 'state'
            cfg_dir = root / 'cfg'
            share_dir = root / 'share'
            state_dir.mkdir()
            cfg_dir.mkdir()
            share_dir.mkdir()

            (state_dir / 'model.json').write_text(json.dumps({
                'recent': [{'providerID': 'deepseek', 'modelID': 'deepseek-chat'}]
            }), encoding='utf-8')
            (cfg_dir / 'opencode.json').write_text(json.dumps({
                'provider': {
                    'custom-gw': {
                        'name': 'MyGW',
                        'options': {'baseURL': 'https://gw.example/v1', 'apiKey': 'DO-NOT-READ'},
                        'models': {'m1': {'name': 'Model One'}},
                    }
                }
            }), encoding='utf-8')

            with mock.patch.object(whoami, 'OPENCODE_STATE_DIR', state_dir), \
                 mock.patch.object(whoami, 'OPENCODE_CONFIG_DIR', cfg_dir), \
                 mock.patch.object(whoami, 'OPENCODE_SHARE_DIR', share_dir):
                st = whoami.read_opencode_model_state()
                self.assertEqual(st['provider_id'], 'deepseek')
                self.assertEqual(st['model_id'], 'deepseek-chat')
                self.assertTrue(st['degraded'])

                cfg = whoami.read_opencode_provider_config()
                self.assertEqual(cfg, {
                    'custom-gw': {
                        'name': 'MyGW',
                        'models': {'m1': 'Model One'},
                        'baseURL': 'https://gw.example/v1',
                    }
                })
                self.assertNotIn('apiKey', json.dumps(cfg))
                desc, base_url = whoami.detect_provider_opencode({'provider_id': 'custom-gw'}, cfg)
                self.assertEqual(desc, 'custom-gw（未映射）')
                self.assertEqual(base_url, 'https://gw.example/v1')


class WhoamiCliTests(unittest.TestCase):
    def _run(self, args: list[str], extra_env: dict[str, str]):
        return subprocess.run(
            [sys.executable, '-X', 'utf8', str(WHOAMI_SCRIPT), *args],
            capture_output=True,
            text=True,
            encoding='utf-8',
            env=_clean_whoami_env(extra_env),
        )

    def test_codex_cli_and_frontmatter(self):
        r = self._run([], {'CODEX_THREAD_ID': 'thread-42'})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Harness:  codex', r.stdout)
        self.assertIn('Provider: OpenAI / Codex', r.stdout)

        rf = self._run(['--frontmatter'], {'CODEX_THREAD_ID': 'thread-42'})
        self.assertEqual(rf.returncode, 0, rf.stderr)
        self.assertIn('agent: codex', rf.stdout)
        self.assertIn('model: openai/codex', rf.stdout)
        self.assertIn('generated_at: ', rf.stdout)

    def test_claude_cli_frontmatter_overrides_and_host(self):
        rf = self._run(
            ['--frontmatter'],
            {
                'CLAUDECODE': '1',
                'ANTHROPIC_BASE_URL': 'https://api.deepseek.com',
                'ANTHROPIC_MODEL': 'deepseek-reasoner',
                'WHOAMI_AGENT': 'custom-host',
                'PRIMARY_HOST': 'definitely-different-primary-host',
            },
        )
        self.assertEqual(rf.returncode, 0, rf.stderr)
        self.assertIn('host: ', rf.stdout)
        self.assertIn('agent: custom-host', rf.stdout)
        self.assertIn('model: deepseek-reasoner', rf.stdout)

        r = self._run(
            [],
            {
                'CLAUDECODE': '1',
                'ANTHROPIC_BASE_URL': 'https://api.deepseek.com',
                'ANTHROPIC_MODEL': 'deepseek-reasoner',
                'ANTHROPIC_DEFAULT_SONNET_MODEL': 'deepseek-reasoner',
                'ANTHROPIC_DEFAULT_OPUS_MODEL': 'deepseek-reasoner',
                'ANTHROPIC_DEFAULT_HAIKU_MODEL': 'deepseek-reasoner',
            },
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Harness:  claude-code', r.stdout)
        self.assertIn('Provider: DeepSeek', r.stdout)
        self.assertIn('Sonnet / Opus / Haiku 全部 → deepseek-reasoner（统一后端）', r.stdout)


class ChangelogAppendCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kb-changelog-')
        self.vault = Path(self.temp.name)
        (self.vault / 'AGENTS.md').write_text('# Vault\n', encoding='utf-8')
        (self.vault / '.meta').mkdir()
        (self.vault / 'docs').mkdir()
        self.changelog = self.vault / 'docs' / 'CHANGELOG.md'
        self.changelog.write_text('# CHANGELOG\n\n## 2026-04-20\n\n### 2026-04-20 10:00 · agent 初始条目\n\n- 第一条\n', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def _run(self, args: list[str], extra_env: dict[str, str] | None = None, stdin_text: str | None = None):
        env = dict(os.environ)
        env['PYTHONUTF8'] = '1'
        env['VAULT_ROOT'] = str(self.vault)
        env.pop('PRIMARY_HOST', None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, '-X', 'utf8', str(CHANGELOG_SCRIPT), *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding='utf-8',
            env=env,
        )

    def test_append_sorting_iso_time_and_bullet_preservation(self):
        # Earlier time on same date -> inserted before 10:00
        r1 = self._run([
            '--time', '2026-04-20T09:15:30',
            '--prefix', 'user',
            '--title', '早间修正',
            '--content', r'- 已有横杠\n无横杠条目',
        ])
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertIn('Entry appended: 2026-04-20 09:15 · user 早间修正', r1.stdout)

        # Newer date -> inserted before 2026-04-20 with --- separator
        r2 = self._run([
            '--time', '2026-04-21 14:30',
            '--prefix', 'agent',
            '--title', '新日期',
            '--content', '-',
        ], stdin_text='* 星号条目\n第二条\n')
        self.assertEqual(r2.returncode, 0, r2.stderr)

        text = self.changelog.read_text(encoding='utf-8')
        self.assertLess(text.index('## 2026-04-21'), text.index('## 2026-04-20'))
        self.assertLess(text.index('### 2026-04-20 09:15'), text.index('### 2026-04-20 10:00'))
        self.assertIn('- 已有横杠\n- 无横杠条目', text)
        self.assertIn('* 星号条目\n- 第二条', text)

    def test_content_file_and_error_exits(self):
        bf = self.vault / 'bullets.txt'
        bf.write_text('文件条目A\n文件条目B\n', encoding='utf-8')
        r = self._run([
            '--time', '2026-04-20 18:00',
            '--prefix', 'manual',
            '--title', '文件输入',
            '--content-file', str(bf),
        ])
        self.assertEqual(r.returncode, 0, r.stderr)

        # Missing required args -> exit 2
        r_missing = self._run(['--prefix', 'agent'])
        self.assertNotEqual(r_missing.returncode, 0)

        # Empty content -> exit 1
        r_empty = self._run(['--prefix', 'agent', '--title', '空', '--content', '   '])
        self.assertEqual(r_empty.returncode, 1)

    def test_subcommands_titles_show_recent_normalize(self):
        today = datetime.now().strftime('%Y-%m-%d')
        r_add = self._run([
            '--time', f'{today} 12:00',
            '--prefix', 'agent',
            '--title', '今日同步',
            '--content', '同步完成',
        ])
        self.assertEqual(r_add.returncode, 0, r_add.stderr)

        r_titles = self._run(['titles', '--limit', '1'])
        self.assertEqual(r_titles.returncode, 0, r_titles.stderr)
        self.assertIn(f'## {today}', r_titles.stdout)
        self.assertNotIn('## 2026-04-20', r_titles.stdout)

        r_show = self._run(['show', '--match', '今日同步', '--section', '同步'])
        self.assertEqual(r_show.returncode, 0, r_show.stderr)
        self.assertIn('- 同步完成', r_show.stdout)

        r_recent = self._run(['recent', '--days', '7'])
        self.assertEqual(r_recent.returncode, 0, r_recent.stderr)
        self.assertIn(f'## {today}', r_recent.stdout)

        # Inject an intra-day separator and missing inter-date separator, then normalize
        self.changelog.write_text(
            '# CHANGELOG\n\n---\n\n## 2026-04-21\n\n### 2026-04-21 10:00 · agent A\n\n- a\n\n---\n\n'
            '### 2026-04-21 11:00 · agent B\n\n- b\n\n## 2026-04-20\n\n### 2026-04-20 09:00 · agent C\n\n- c\n',
            encoding='utf-8',
        )
        r_norm = self._run(['normalize'])
        self.assertEqual(r_norm.returncode, 0, r_norm.stderr)
        self.assertIn('Removed 1 intra-day separator(s), added 1 inter-date separator(s).', r_norm.stdout)

    def test_host_guard_blocks_non_primary_host(self):
        (self.vault / '.env').write_text('PRIMARY_HOST=non-existent-primary-host\n', encoding='utf-8')
        r = self._run(['titles'])
        self.assertEqual(r.returncode, 1)
        self.assertIn('FATAL: this script must run on PRIMARY_HOST', r.stderr)


if __name__ == '__main__':
    unittest.main()
