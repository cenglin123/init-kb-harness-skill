#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detect_renames.py — 检测文件重命名、移动、删除

逻辑：
1. 对比 sqlite 中记录的 path 集合 vs 当前文件系统的 path 集合
2. 用 content_hash 交叉匹配 → 识别纯重命名
3. 用 git diff -M 辅助识别"改名+编辑"
4. 应用重命名：更新 sqlite path + 移动 .meta/ 伴生文件
5. 未匹配的旧文件：归档 .meta/ 伴生文件到 .meta/.archive/

用法: python .meta/scripts/detect_renames.py
"""

import sys
import sqlite3
import re
import shutil
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    VAULT_ROOT, scan_indexable_notes, rel_path, content_hash,
    meta_mirror, git, git_available
)

DB_PATH = VAULT_ROOT / '.meta' / 'embeddings.sqlite'


def get_git_renames():
    """
    用 git diff -M 检测最近一次 commit 中的重命名。
    仅在 HEAD commit 是 user: 或 manual: 前缀时运行（即用户改动）。
    """
    if not git_available():
        return []

    head_msg = git('log', '-1', '--format=%s', check=False).stdout.strip()
    if not (head_msg.startswith('user:') or head_msg.startswith('manual:')):
        return []

    r = git('diff', '--name-status', '-M80', 'HEAD~1', 'HEAD', check=False)
    if r.returncode != 0:
        return []

    renames = []
    for line in r.stdout.strip().splitlines():
        m = re.match(r'^R(\d+)\s+(.+?)\s+(.+)$', line)
        if m:
            old_path = m.group(2).strip().replace('\\', '/')
            new_path = m.group(3).strip().replace('\\', '/')
            renames.append((old_path, new_path))
    return renames


def get_sqlite_state(db):
    """返回 {path: hash}"""
    return {
        row[0]: row[1]
        for row in db.execute("SELECT DISTINCT path, content_hash FROM embeddings")
    }


def get_fs_state():
    """返回 {path: hash}"""
    result = {}
    for md in scan_indexable_notes(scope='all'):
        result[rel_path(md)] = content_hash(md)
    return result


def apply_rename(db, old_rel, new_rel):
    """更新 sqlite + 移动 .meta/ 伴生文件"""
    db.execute("UPDATE embeddings SET path = ? WHERE path = ?", (new_rel, old_rel))

    for subdir in ('summaries', 'links', 'tags'):
        old_p = meta_mirror(VAULT_ROOT / old_rel, subdir)
        new_p = meta_mirror(VAULT_ROOT / new_rel, subdir)
        if old_p.exists():
            new_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_p), str(new_p))
            update_sidecar_source(new_p, new_rel)

    print(f"  ✓ 重命名: {old_rel} → {new_rel}")


def update_sidecar_source(path: Path, new_rel: str):
    """同步伴生文件 frontmatter 中的 source 字段。"""
    text = path.read_text(encoding='utf-8')
    if not text.startswith('---\n'):
        return

    end = text.find('\n---', 4)
    if end == -1:
        return

    frontmatter = text[:end]
    updated, count = re.subn(
        r'(?m)^source\s*:\s*.*$',
        f'source: {new_rel}',
        frontmatter,
        count=1,
    )
    if count:
        path.write_text(updated + text[end:], encoding='utf-8')


def archive_orphan(rel, date_str):
    """将 .meta/ 伴生文件归档。返回是否归档了任何文件。"""
    archived_any = False
    for subdir in ('summaries', 'links', 'tags'):
        src = meta_mirror(VAULT_ROOT / rel, subdir)
        if src.exists():
            rel_to_meta = src.relative_to(VAULT_ROOT / '.meta' / subdir)
            dst = VAULT_ROOT / '.meta' / '.archive' / date_str / subdir / rel_to_meta
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            archived_any = True
    return archived_any


def clean_empty_dirs(root: Path):
    """从底向上清理空目录"""
    for p in sorted(root.rglob('*'), key=lambda x: -len(x.parts)):
        if p.is_dir() and p != root:
            try:
                if not any(p.iterdir()):
                    p.rmdir()
            except OSError:
                pass


def find_hash_matches(sqlite_hashes, fs_hashes, exclude_new=None):
    """
    用 content_hash 交叉匹配。
    返回: (matches: [(old, new)], orphaned: [old_path])
    """
    sqlite_paths = set(sqlite_hashes.keys())
    fs_paths = set(fs_hashes.keys())

    missing_from_fs = sqlite_paths - fs_paths
    missing_from_sqlite = fs_paths - sqlite_paths

    if exclude_new:
        missing_from_sqlite -= set(exclude_new)

    hash_to_fs = {}
    for p, h in fs_hashes.items():
        if p in missing_from_sqlite:
            hash_to_fs.setdefault(h, []).append(p)

    matches = []
    orphaned = []
    for old_path in missing_from_fs:
        h = sqlite_hashes[old_path]
        candidates = hash_to_fs.get(h, [])
        if len(candidates) == 1:
            matches.append((old_path, candidates[0]))
        else:
            orphaned.append(old_path)

    return matches, orphaned


def main():
    if not DB_PATH.exists():
        print("  embeddings.sqlite 不存在，跳过重命名检测")
        return 0

    db = sqlite3.connect(str(DB_PATH))
    print("  ── 重命名检测 ──")

    sqlite_hashes = get_sqlite_state(db)
    fs_hashes = get_fs_state()

    git_renames = get_git_renames()
    git_handled_new = set()

    # 阶段 1：应用 git 检测到的重命名
    if git_renames:
        print(f"  Git 检测到 {len(git_renames)} 个重命名")
        for old_rel, new_rel in git_renames:
            if new_rel not in fs_hashes:
                print(f"    ⚠  git 报告的目标不存在，跳过: {new_rel}")
                continue
            apply_rename(db, old_rel, new_rel)
            git_handled_new.add(new_rel)

    # 阶段 2：hash 交叉匹配（补充 git 未捕获的纯改名）
    matches, orphaned = find_hash_matches(sqlite_hashes, fs_hashes, exclude_new=git_handled_new)

    if matches:
        print(f"  Hash 匹配发现 {len(matches)} 个重命名")
        for old_rel, new_rel in matches:
            apply_rename(db, old_rel, new_rel)

    # 阶段 3：归档未匹配的孤儿
    date_str = datetime.now().strftime("%Y%m%d")
    archived_count = 0
    for old_path in orphaned:
        if archive_orphan(old_path, date_str):
            archived_count += 1
        db.execute("DELETE FROM embeddings WHERE path = ?", (old_path,))

    if archived_count:
        print(f"  ✓ 归档 {archived_count} 个已删除文件的元数据")

    # 清理空目录
    for subdir in ('summaries', 'links', 'tags'):
        clean_empty_dirs(VAULT_ROOT / '.meta' / subdir)

    db.commit()
    db.close()

    total_renames = len(git_renames) + len(matches)
    if total_renames:
        print(f"  ✓ 共处理 {total_renames} 个重命名")
    elif archived_count:
        print(f"  ✓ 无重命名，归档 {archived_count} 个已删除文件")
    else:
        print("  ✓ 无变化")
    return 0


if __name__ == "__main__":
    sys.exit(main())
