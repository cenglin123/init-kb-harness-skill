#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_sidecar_sources.py — 检查/修复伴生文件 source frontmatter

只处理 .meta/summaries、.meta/links、.meta/tags 下的脚本产物。
规则：source 必须等于该 sidecar 相对其根目录的镜像路径。
"""

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import VAULT_ROOT, log_script_run

SIDECAR_DIRS = ("summaries", "links", "tags")
SOURCE_RE = re.compile(r"(?m)^source\s*:\s*(.+?)\s*$")


@dataclass(frozen=True)
class SidecarIssue:
    kind: str
    path: str
    expected: str
    actual: str = ""


def sidecar_bases(root: Path) -> list[Path]:
    return [root / ".meta" / name for name in SIDECAR_DIRS]


def split_frontmatter(text: str) -> tuple[str | None, str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---", 4)
    if end == -1:
        return None, text
    return text[:end], text[end:]


def expected_source(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def check_sidecars(root: Path = VAULT_ROOT) -> list[SidecarIssue]:
    issues = []
    for base in sidecar_bases(root):
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.md")):
            expected = expected_source(path, base)
            rel = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8")
            frontmatter, _ = split_frontmatter(text)
            if frontmatter is None:
                issues.append(SidecarIssue("missing-frontmatter", rel, expected))
                continue
            match = SOURCE_RE.search(frontmatter)
            if not match:
                issues.append(SidecarIssue("missing-source", rel, expected))
                continue
            actual = match.group(1).strip().strip('"')
            if actual != expected:
                issues.append(SidecarIssue("wrong-source", rel, expected, actual))
    return sorted(issues, key=lambda issue: (issue.kind, issue.path))


def fix_one(path: Path, base: Path) -> bool:
    expected = expected_source(path, base)
    text = path.read_text(encoding="utf-8")
    frontmatter, rest = split_frontmatter(text)

    if frontmatter is None:
        now = datetime.now().replace(microsecond=0).isoformat()
        repaired = (
            "---\n"
            f"source: {expected}\n"
            f"generated_at: {now}\n"
            "model: sidecar-source-repair\n"
            "---\n\n"
            f"{text}"
        )
        path.write_text(repaired, encoding="utf-8")
        return True

    match = SOURCE_RE.search(frontmatter)
    if match:
        actual = match.group(1).strip().strip('"')
        if actual == expected:
            return False
        updated = SOURCE_RE.sub(f"source: {expected}", frontmatter, count=1)
        path.write_text(updated + rest, encoding="utf-8")
        return True

    lines = frontmatter.splitlines()
    updated = "\n".join([lines[0], f"source: {expected}", *lines[1:]])
    path.write_text(updated + rest, encoding="utf-8")
    return True


def fix_sidecars(root: Path = VAULT_ROOT) -> int:
    fixed = 0
    for base in sidecar_bases(root):
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.md")):
            if fix_one(path, base):
                fixed += 1
    return fixed


def print_issues(issues: list[SidecarIssue]):
    for issue in issues:
        detail = f" actual={issue.actual}" if issue.actual else ""
        print(f"{issue.kind}\t{issue.path}\texpected={issue.expected}{detail}")


def main(argv=None) -> int:
    log_script_run()
    parser = argparse.ArgumentParser(description="检查/修复 sidecar source frontmatter")
    parser.add_argument("--fix", action="store_true", help="自动修复 source 不一致")
    args = parser.parse_args(argv)

    if args.fix:
        fixed = fix_sidecars(VAULT_ROOT)
        remaining = check_sidecars(VAULT_ROOT)
        print(f"  ✓ sidecar source fixed: {fixed}, remaining issues: {len(remaining)}")
        if remaining:
            print_issues(remaining)
            return 1
        return 0

    issues = check_sidecars(VAULT_ROOT)
    if issues:
        print(f"  ✗ sidecar source issues: {len(issues)}")
        print_issues(issues)
        return 1
    print("  ✓ sidecar source issues: 0")
    return 0


if __name__ == "__main__":
    # --- host guard ---
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from common import is_primary_host
    if not is_primary_host():
        print("FATAL: this script must run on PRIMARY_HOST", file=sys.stderr)
        sys.exit(1)
    # --- /host guard ---
    sys.exit(main())
