#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_plan_status.py — 检查 docs/plans 路径与 frontmatter status 是否一致

规则：
- docs/plans/active/*.md       -> status: active
- docs/plans/done/*.md         -> status: done
- docs/plans/not_planned/*.md  -> status: not_planned

附加检查（--full 或 --archive-check）：
- docs/plans/active/ 下所有 - [ ] 均已勾选但 status 仍为 active 的计划，建议归档

特例：
- docs/plans/active/backlog.md 是开放项清单，不是标准 plan，跳过。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


EXPECTED_BY_DIR = {
    "active": "active",
    "done": "done",
    "not_planned": "not_planned",
}

EXEMPT_REL_PATHS = {
    Path("docs/plans/active/backlog.md"),
}


@dataclass(frozen=True)
class Mismatch:
    path: Path
    expected: str
    actual: str | None


def vault_root_from_script() -> Path:
    """从脚本位置向上查找 vault root。"""
    p = Path(__file__).resolve()
    while p != p.parent:
        if (p / "AGENTS.md").exists() and (p / ".meta").is_dir():
            return p
        p = p.parent
    raise RuntimeError("无法定位 vault root")


def extract_status(text: str) -> str | None:
    """从 YAML frontmatter 中提取 status。无 frontmatter 或无字段时返回 None。"""
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|\Z)", text, re.DOTALL)
    if not match:
        return None
    status_match = re.search(r"^status:\s*([^\s#]+)\s*(?:#.*)?$", match.group(1), re.MULTILINE)
    if not status_match:
        return None
    return status_match.group(1).strip("'\"")


def iter_plan_files(root: Path) -> list[tuple[Path, str]]:
    """列出需检查的 plan 文件及期望 status。"""
    items: list[tuple[Path, str]] = []
    for dirname, expected in EXPECTED_BY_DIR.items():
        plan_dir = root / "docs" / "plans" / dirname
        if not plan_dir.exists():
            continue
        for path in sorted(plan_dir.glob("*.md")):
            rel = path.relative_to(root)
            if rel in EXEMPT_REL_PATHS:
                continue
            items.append((path, expected))
    return items


def check(root: Path) -> list[Mismatch]:
    mismatches: list[Mismatch] = []
    for path, expected in iter_plan_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"无法以 UTF-8 读取 {path}: {exc}") from exc
        actual = extract_status(text)
        if actual != expected:
            mismatches.append(Mismatch(path=path, expected=expected, actual=actual))
    return mismatches


def format_rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def check_archive_ready(root: Path) -> list[Path]:
    """检测 active/ 下计划是否全部打勾但未归档。返回应归档的计划列表。"""
    ready: list[Path] = []
    plan_dir = root / "docs" / "plans" / "active"
    if not plan_dir.exists():
        return ready
    for path in sorted(plan_dir.glob("*.md")):
        rel = path.relative_to(root)
        if rel in EXEMPT_REL_PATHS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        # 只检查 status: active 的计划
        status = extract_status(text)
        if status != "active":
            continue
        # 找所有 checkbox：- [ ] 和 - [x]
        unchecked = len(re.findall(r"-\s+\[ \]", text))
        checked = len(re.findall(r"-\s+\[x\]", text))
        if checked > 0 and unchecked == 0:
            ready.append(path)
    return ready


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="检查 docs/plans 路径与 YAML frontmatter status 是否一致。"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=vault_root_from_script(),
        help="vault root；默认自动从脚本位置向上查找。",
    )
    parser.add_argument(
        "--archive-check",
        action="store_true",
        default=None,
        help="额外检查 active/ 下全部勾选但未归档的计划。",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="检查归档就绪；staged plan 另由 check_plan_review.py 核验。",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    mismatches = check(root)
    archive_ready = check_archive_ready(root) if (args.archive_check or args.full) else []

    ok = True

    if mismatches:
        print("ERROR: docs/plans 路径与 frontmatter status 不一致：")
        for item in mismatches:
            actual = item.actual if item.actual is not None else "<missing>"
            print(
                f"- {format_rel(item.path, root)} :: "
                f"expected={item.expected}, actual={actual}"
            )
        print("\n修复方式：先修改 frontmatter status，再重新 stage/commit。\n")
        ok = False

    if archive_ready:
        print("WARNING: 以下 active/ 计划已全部勾选，应归档至 done/：")
        for p in archive_ready:
            print(f"- {format_rel(p, root)}")
        print("\n操作：git mv <plan> docs/plans/done/ 并将 status 改为 done。\n")

    if not mismatches:
        print("OK: docs/plans 路径与 frontmatter status 一致")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
