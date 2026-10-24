"""派生件 git 管理 + file-dates 单次扫描测试。

自包含：临时 git 仓库，不触碰真实知识库。
"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


class GitDerivedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / 'vault'
        self.scripts = self.vault / '.meta' / 'scripts'
        self.scripts.mkdir(parents=True)
        (self.vault / 'AGENTS.md').write_text('', encoding='utf-8')
        for src in SCRIPTS.glob('*.py'):
            shutil.copyfile(src, self.scripts / src.name)
        self.notes = self.vault / 'notes'
        self.notes.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.com')
        self.git('config', 'user.name', 'test')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.vault,
                              capture_output=True, text=True, encoding='utf-8')

    def commit(self, msg):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', msg)

    def run_py(self, code):
        return subprocess.run([sys.executable, '-c', code], cwd=self.vault,
                              capture_output=True, text=True, encoding='utf-8')

    def test_derived_artifacts_untracked_and_dates_single_pass(self):
        (self.notes / 'a.md').write_text('# A\n', encoding='utf-8')
        (self.notes / 'b.md').write_text('# B\n', encoding='utf-8')
        self.commit('init')
        time.sleep(1)
        (self.notes / 'a.md').write_text('# A v2\n', encoding='utf-8')
        self.commit('update a')

        # 模拟历史版本：向量库曾被 git 跟踪
        db = self.vault / '.meta' / 'embeddings.sqlite'
        db.write_bytes(b'fake-sqlite')
        self.git('add', '-f', '.meta/embeddings.sqlite')
        self.git('commit', '-q', '-m', 'track db')
        self.assertIn('.meta/embeddings.sqlite', self.git('ls-files').stdout)

        code = (
            f"import sys; sys.path.insert(0, r'{self.scripts}');"
            "from common import ensure_derived_gitignore, git_last_dates;"
            "print('changed', ensure_derived_gitignore());"
            "d = git_last_dates(['notes/a.md', 'notes/b.md']);"
            "print('dates', sorted(d));"
            "print('a_ts', d.get('notes/a.md'));"
            "print('again', ensure_derived_gitignore())"
        )
        result = self.run_py(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('changed True', result.stdout)
        self.assertIn("dates ['notes/a.md', 'notes/b.md']", result.stdout)
        self.assertIn('again False', result.stdout)

        # 工作区文件保留，但索引里已移除；.gitignore 有受管块
        self.assertTrue(db.exists())
        tracked = self.git('ls-files', '--', '.meta/embeddings.sqlite').stdout.strip()
        self.assertEqual(tracked, '')
        ignore = (self.vault / '.gitignore').read_text(encoding='utf-8')
        self.assertIn('init-kb-harness: derived artifacts', ignore)
        self.assertIn('.meta/embeddings.sqlite', ignore)

        # file-dates 的时间戳等于该文件最近一次提交
        expected = self.git('log', '-1', '--format=%at', '--', 'notes/a.md').stdout.strip()
        self.assertIn(f"a_ts {float(expected)}", result.stdout)


if __name__ == '__main__':
    unittest.main()