"""Batch 0 bugfix 单元测试：
1. detect_renames.py 主机护栏（非主机不改库/不移伴生文件，主机正常执行）
2. health_report.py 从 .env 读取 SCRIPT_COLLECTION_DIR（兼容 os.environ 兜底）
3. health_report.py health-trend.csv 同日去重更新（不重复追加）
4. host_guard.py 移除硬编码个人目录名，改为由策略配置驱动 Bash 隐私前缀检查
"""
from datetime import datetime
from pathlib import Path
import csv
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / 'scripts'


def _copy_scripts(vault: Path) -> Path:
    scripts = vault / '.meta' / 'scripts'
    scripts.mkdir(parents=True, exist_ok=True)
    (vault / 'AGENTS.md').write_text('# test vault\n', encoding='utf-8')
    for src in SCRIPTS_DIR.glob('*.py'):
        shutil.copyfile(src, scripts / src.name)
    return scripts


class DetectRenamesHostGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / 'vault'
        self.scripts = _copy_scripts(self.vault)

        # 构造一条已被重命名的笔记 + 旧伴生文件 + embeddings.sqlite
        self.note_content = '# 测试笔记\n\n用于验证 detect_renames 主机护栏。\n'

        new_note = self.vault / 'notes' / 'new.md'
        new_note.parent.mkdir(parents=True, exist_ok=True)
        new_note.write_text(self.note_content, encoding='utf-8')
        self.note_hash = hashlib.sha256(new_note.read_bytes()).hexdigest()[:16]

        self.old_sidecar = self.vault / '.meta' / 'summaries' / 'notes' / 'old.md'
        self.new_sidecar = self.vault / '.meta' / 'summaries' / 'notes' / 'new.md'
        self.old_sidecar.parent.mkdir(parents=True, exist_ok=True)
        self.old_sidecar.write_text(
            '---\nsource: notes/old.md\n---\n摘要正文\n', encoding='utf-8'
        )

        self.db_path = self.vault / '.meta' / 'embeddings.sqlite'
        con = sqlite3.connect(str(self.db_path))
        con.execute(
            'CREATE TABLE embeddings (path TEXT, chunk_index INTEGER, content_hash TEXT)'
        )
        con.execute(
            'INSERT INTO embeddings (path, chunk_index, content_hash) VALUES (?, ?, ?)',
            ('notes/old.md', 0, self.note_hash),
        )
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _db_paths(self) -> list[str]:
        con = sqlite3.connect(str(self.db_path))
        try:
            return [r[0] for r in con.execute('SELECT path FROM embeddings')]
        finally:
            con.close()

    def _run_detect_renames(self) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.pop('PRIMARY_HOST', None)
        env['VAULT_ROOT'] = str(self.vault)
        return subprocess.run(
            [sys.executable, '-X', 'utf8', str(self.scripts / 'detect_renames.py')],
            cwd=self.vault,
            env=env,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )

    def test_non_primary_host_exits_without_mutating_state(self):
        (self.vault / '.env').write_text(
            'PRIMARY_HOST=non-existent-primary-host-xyz\n', encoding='utf-8'
        )
        res = self._run_detect_renames()
        self.assertEqual(res.returncode, 1)
        self.assertIn('PRIMARY_HOST', res.stderr)
        # sqlite 与伴生文件均未被修改
        self.assertEqual(self._db_paths(), ['notes/old.md'])
        self.assertTrue(self.old_sidecar.exists())
        self.assertFalse(self.new_sidecar.exists())

    def test_primary_host_applies_rename(self):
        (self.vault / '.env').write_text(
            f'PRIMARY_HOST={socket.gethostname()}\n', encoding='utf-8'
        )
        res = self._run_detect_renames()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self._db_paths(), ['notes/new.md'])
        self.assertFalse(self.old_sidecar.exists())
        self.assertTrue(self.new_sidecar.exists())
        self.assertIn('source: notes/new.md', self.new_sidecar.read_text(encoding='utf-8'))


class HealthReportFixesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / 'vault'
        self.scripts = _copy_scripts(self.vault)

        # 基础笔记与最小 graph.json
        note = self.vault / 'notes' / 'a.md'
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text('# 笔记 A\n\n正文。\n', encoding='utf-8')
        self._write_graph(['notes/a.md'])

        # 脚本收集目录
        tools = self.vault / 'tools'
        tools.mkdir()
        (tools / 'alpha.py').write_text('print(1)\n', encoding='utf-8')
        (tools / 'beta.py').write_text('print(2)\n', encoding='utf-8')
        (tools / 'beta_说明文档.md').write_text('# beta 说明\n', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def _write_graph(self, node_ids: list[str]) -> None:
        graph = {
            'nodes': [{'id': nid, 'in_degree_wiki': 0} for nid in node_ids],
            'edges': [],
        }
        (self.vault / '.meta' / 'graph.json').write_text(
            json.dumps(graph, ensure_ascii=False), encoding='utf-8'
        )

    def _run_health_report(self, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.pop('SCRIPT_COLLECTION_DIR', None)
        env.pop('PRIMARY_HOST', None)
        env['VAULT_ROOT'] = str(self.vault)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, '-X', 'utf8', str(self.scripts / 'health_report.py')],
            cwd=self.vault,
            env=env,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )

    def test_script_collection_dir_from_dotenv(self):
        (self.vault / '.env').write_text(
            f'PRIMARY_HOST={socket.gethostname()}\n'
            'EMBED_PROVIDER=off\n'
            'SCRIPT_COLLECTION_DIR=tools\n',
            encoding='utf-8',
        )
        res = self._run_health_report()
        self.assertEqual(res.returncode, 0, res.stderr)
        report = (self.vault / '.meta' / 'health-report.md').read_text(encoding='utf-8')
        self.assertIn('脚本总数: 2', report)
        self.assertIn('有说明文档: 1', report)
        self.assertIn('缺说明文档: 1', report)
        self.assertIn('`alpha.py`', report)

    def test_script_collection_dir_from_os_environ_fallback(self):
        (self.vault / '.env').write_text(
            f'PRIMARY_HOST={socket.gethostname()}\n'
            'EMBED_PROVIDER=off\n',
            encoding='utf-8',
        )
        res = self._run_health_report(extra_env={'SCRIPT_COLLECTION_DIR': 'tools'})
        self.assertEqual(res.returncode, 0, res.stderr)
        report = (self.vault / '.meta' / 'health-report.md').read_text(encoding='utf-8')
        self.assertIn('脚本总数: 2', report)
        self.assertIn('`alpha.py`', report)

    def test_script_collection_dir_unset_skips(self):
        (self.vault / '.env').write_text(
            f'PRIMARY_HOST={socket.gethostname()}\n'
            'EMBED_PROVIDER=off\n',
            encoding='utf-8',
        )
        res = self._run_health_report()
        self.assertEqual(res.returncode, 0, res.stderr)
        report = (self.vault / '.meta' / 'health-report.md').read_text(encoding='utf-8')
        self.assertIn('未配置 SCRIPT_COLLECTION_DIR，跳过', report)

    def test_health_trend_same_day_dedup_and_update(self):
        (self.vault / '.env').write_text(
            f'PRIMARY_HOST={socket.gethostname()}\n'
            'EMBED_PROVIDER=off\n',
            encoding='utf-8',
        )
        today = datetime.now().strftime('%Y-%m-%d')
        trend_path = self.vault / '.meta' / 'health-trend.csv'

        # 预置历史日期 + 历史遗留的同日重复行
        with open(trend_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['date', 'total_notes', 'orphans'])
            writer.writerow(['2020-01-01', '5', '2'])
            writer.writerow([today, '99', '99'])
            writer.writerow([today, '88', '88'])

        # 第一次运行：2 篇笔记（notes/a.md + tools/beta_说明文档.md），1 个孤儿（应覆盖折叠同日行）
        res1 = self._run_health_report()
        self.assertEqual(res1.returncode, 0, res1.stderr)
        with open(trend_path, 'r', newline='', encoding='utf-8') as f:
            rows1 = list(csv.reader(f))
        self.assertEqual(rows1, [
            ['date', 'total_notes', 'orphans'],
            ['2020-01-01', '5', '2'],
            [today, '2', '1'],
        ])

        # 新增一篇笔记并再次运行：今日行应就地更新为 3/2，不新增重复行
        (self.vault / 'notes' / 'b.md').write_text('# 笔记 B\n', encoding='utf-8')
        self._write_graph(['notes/a.md', 'notes/b.md'])

        res2 = self._run_health_report()
        self.assertEqual(res2.returncode, 0, res2.stderr)
        with open(trend_path, 'r', newline='', encoding='utf-8') as f:
            rows2 = list(csv.reader(f))
        self.assertEqual(rows2, [
            ['date', 'total_notes', 'orphans'],
            ['2020-01-01', '5', '2'],
            [today, '3', '2'],
        ])


class HostGuardPrivacyConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / 'vault'
        self.scripts = _copy_scripts(self.vault)
        (self.vault / '.meta' / 'rules').mkdir(parents=True, exist_ok=True)
        (self.vault / '.env').write_text(
            f'PRIMARY_HOST={socket.gethostname()}\n', encoding='utf-8'
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _write_policy(self, policy: dict) -> None:
        (self.vault / '.meta' / 'rules' / 'host-write-policy.json').write_text(
            json.dumps(policy, ensure_ascii=False), encoding='utf-8'
        )

    def _run_pre_tool(self, payload: dict) -> tuple[int, dict]:
        env = os.environ.copy()
        env['HOST_GUARD_VAULT_ROOT'] = str(self.vault)
        res = subprocess.run(
            [sys.executable, '-X', 'utf8', str(self.scripts / 'host_guard.py'), 'pre-tool'],
            cwd=self.vault,
            env=env,
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        data = json.loads(res.stdout) if res.stdout.strip() else {}
        return res.returncode, data

    def test_no_hardcoded_personal_dir_in_source(self):
        source = (SCRIPTS_DIR / 'host_guard.py').read_text(encoding='utf-8')
        self.assertNotIn('000【备忘录', source)

    def test_bash_privacy_check_uses_policy_privacy_dirs(self):
        self._write_policy({
            'privacy_dirs': ['secret_vault/**', 'private_notes*/**'],
            'deny_rules': [],
        })

        # 命中配置的隐私目录（POSIX 与 Windows 反斜杠路径）
        rc, data = self._run_pre_tool({
            'tool_name': 'Bash',
            'tool_input': {'command': 'cat secret_vault/diary.md'},
        })
        self.assertEqual(rc, 1)
        self.assertEqual(data.get('decision'), 'deny')

        rc, data = self._run_pre_tool({
            'tool_name': 'Bash',
            'tool_input': {'command': r'type secret_vault\diary.md'},
        })
        self.assertEqual(rc, 1)
        self.assertEqual(data.get('decision'), 'deny')

        # 命中带通配符前缀的隐私目录
        rc, data = self._run_pre_tool({
            'tool_name': 'Bash',
            'tool_input': {'command': 'ls private_notes_2026/a.md'},
        })
        self.assertEqual(rc, 1)
        self.assertEqual(data.get('decision'), 'deny')

        # 普通目录放行
        rc, data = self._run_pre_tool({
            'tool_name': 'Bash',
            'tool_input': {'command': 'cat notes/public.md'},
        })
        self.assertEqual(rc, 0)
        self.assertEqual(data.get('decision'), 'allow')

    def test_bash_privacy_check_supports_explicit_privacy_prefixes(self):
        self._write_policy({
            'privacy_dirs': [],
            'privacy_prefixes': ['confidential_box'],
            'deny_rules': [],
        })

        rc, data = self._run_pre_tool({
            'tool_name': 'Bash',
            'tool_input': {'command': 'ls confidential_box'},
        })
        self.assertEqual(rc, 1)
        self.assertEqual(data.get('decision'), 'deny')

        rc, data = self._run_pre_tool({
            'tool_name': 'Bash',
            'tool_input': {'command': 'ls notes'},
        })
        self.assertEqual(rc, 0)
        self.assertEqual(data.get('decision'), 'allow')

    def test_empty_privacy_policy_allows_bash(self):
        self._write_policy({
            'privacy_dirs': [],
            'deny_rules': [],
        })
        rc, data = self._run_pre_tool({
            'tool_name': 'Bash',
            'tool_input': {'command': 'ls notes/any.md'},
        })
        self.assertEqual(rc, 0)
        self.assertEqual(data.get('decision'), 'allow')

    def test_append_trend_handles_utf8_bom_and_invalid_encoding(self):
        """P2-3: health-trend.csv 带 UTF-8 BOM 或坏编码时能正确剥 BOM / 降级重建并原子写回。"""
        (self.vault / 'AGENTS.md').write_text('# Vault\n', encoding='utf-8')
        scripts_dir = self.vault / '.meta' / 'scripts'
        scripts_dir.mkdir(parents=True, exist_ok=True)
        for src in (Path(__file__).resolve().parent.parent / 'scripts').glob('*.py'):
            shutil.copyfile(src, scripts_dir / src.name)
        trend = self.vault / '.meta' / 'health-trend.csv'
        trend.parent.mkdir(parents=True, exist_ok=True)
        trend.write_bytes(b'\xef\xbb\xbfdate,total_notes,orphans\r\n2026-01-01,10,1\r\n')
        code = f"""
import sys
from pathlib import Path
sys.path.insert(0, r'{scripts_dir}')
import health_report as hr
hr.TREND_PATH = Path(r'{trend}')
hr.append_trend(12, 2)
"""
        proc = subprocess.run(
            [sys.executable, '-X', 'utf8', '-c', code],
            cwd=self.vault, capture_output=True, text=True, encoding='utf-8',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = trend.read_text(encoding='utf-8').splitlines()
        self.assertEqual(lines[0], 'date,total_notes,orphans')
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1], '2026-01-01,10,1')

        # 非法 UTF-8 字节应优雅重建而非崩溃
        trend.write_bytes(b'\xff\xfe\x80\x81')
        proc2 = subprocess.run(
            [sys.executable, '-X', 'utf8', '-c', code],
            cwd=self.vault, capture_output=True, text=True, encoding='utf-8',
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        lines2 = trend.read_text(encoding='utf-8').splitlines()
        self.assertEqual(lines2[0], 'date,total_notes,orphans')
        self.assertEqual(len(lines2), 2)

    def test_maintain_parses_current_health_report_summary(self):
        """P2-2: maintain.py 正则必须匹配 health_report.py 现行 '孤儿 X=Y活+Z档' 输出。"""
        (self.vault / 'AGENTS.md').write_text('# Vault\n', encoding='utf-8')
        scripts_dir = self.vault / '.meta' / 'scripts'
        scripts_dir.mkdir(parents=True, exist_ok=True)
        for src in (Path(__file__).resolve().parent.parent / 'scripts').glob('*.py'):
            shutil.copyfile(src, scripts_dir / src.name)
        code = f"""
import json, sys
sys.path.insert(0, r'{scripts_dir}')
import maintain
sample = '✓ health-report.md (42 篇, 孤儿 5=3活+2档, 7 收件箱)'
print(json.dumps(maintain.summarize_command_output(sample), ensure_ascii=False))
"""
        proc = subprocess.run(
            [sys.executable, '-X', 'utf8', '-c', code],
            cwd=self.vault, capture_output=True, text=True, encoding='utf-8',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        bullets = json.loads(proc.stdout.strip())
        self.assertTrue(
            any('42 篇笔记' in b and '5 篇孤儿' in b and '7 项收件箱' in b for b in bullets),
            bullets,
        )


if __name__ == '__main__':
    unittest.main()
