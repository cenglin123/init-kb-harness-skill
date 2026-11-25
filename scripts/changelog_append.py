#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""changelog_append.py — CHANGELOG 条目追加与查询工具（旧库兼容）。

用法:
    python .meta/scripts/changelog_append.py --time "2026-04-27 14:30" --prefix agent --title "功能：xxx" --content "第一条\\n第二条"
    python .meta/scripts/changelog_append.py --prefix agent --title "xxx" --content-file bullets.txt
    echo "第一条" | python .meta/scripts/changelog_append.py --prefix agent --title "xxx" --content -
    python .meta/scripts/changelog_append.py {normalize|titles|show|recent} ...
"""

import argparse
from datetime import datetime, timedelta
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).parent))
from common import VAULT_ROOT, is_primary_host, log_script_run

_DATE_HEADING_RE = re.compile(r'^##\s+(\d{4}-\d{2}-\d{2})\s*$', re.MULTILINE)
_ENTRY_TIME_RE = re.compile(r'^### (\d{4}-\d{2}-\d{2} \d{2}:\d{2})', re.MULTILINE)
_ENTRY_HEADING_RE = re.compile(r'^### (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) · (.+)$', re.MULTILINE)


def _strip_trailing_separators(text: str) -> str:
    """Remove trailing ``---`` separator lines and surrounding blanks."""
    lines = text.rstrip().split('\n')
    while lines and lines[-1].strip() == '---':
        lines.pop()
        while lines and lines[-1].strip() == '':
            lines.pop()
    return '\n'.join(lines)


def _ensure_bullet(b: str) -> str:
    """Return b as a bullet point without doubling an existing '- '/'* ' marker."""
    if re.match(r'^\s*[-*]\s+', b):
        return b.rstrip()
    return f"- {b.strip()}"


def insert_changelog_entry(
    path: Path,
    time: str,
    prefix: str,
    title: str,
    bullets: list[str],
) -> bool:
    """Insert a changelog entry at the HH:MM-sorted position; returns False if bullets is empty."""
    if not bullets:
        return False

    date_heading = f"## {time[:10]}"
    heading = f"### {time} · {prefix} {title}"
    block = '\n'.join([heading, ''] + [_ensure_bullet(b) for b in bullets])

    content = path.read_text(encoding='utf-8')
    date_match = re.search(rf'^{re.escape(date_heading)}\s*$', content, re.MULTILINE)

    if date_match:
        rest = content[date_match.end():]
        next_date = _DATE_HEADING_RE.search(rest)
        section_end = date_match.end() + (next_date.start() if next_date else len(rest))
        section_text = content[date_match.end():section_end]

        insert_pos = None
        for m in _ENTRY_TIME_RE.finditer(section_text):
            if time < m.group(1):
                insert_pos = date_match.end() + m.start()
                break

        if insert_pos is not None:
            before = content[:insert_pos].rstrip()
            after = content[insert_pos:].lstrip('\n')
            updated = before + '\n\n' + block + (f'\n\n{after}' if after else '\n')
        else:
            before = _strip_trailing_separators(content[:section_end])
            after = content[section_end:].lstrip('\n')
            updated = before + '\n\n' + block + (f'\n\n---\n\n{after}' if after else '\n')
    else:
        new_section = f"{date_heading}\n\n{block}"
        first_date = _DATE_HEADING_RE.search(content)
        if first_date:
            prefix_text = _strip_trailing_separators(content[:first_date.start()])
            updated = (prefix_text + '\n\n---\n\n' + new_section
                       + '\n\n---\n\n' + content[first_date.start():])
        else:
            prefix_text = _strip_trailing_separators(content)
            updated = prefix_text + '\n\n---\n\n' + new_section

    path.write_text(updated, encoding='utf-8', newline='')
    return True


def normalize_separators(path: Path) -> tuple[int, int]:
    """Remove intra-day --- and add missing inter-date ---. Returns (removed, added)."""
    lines = path.read_text(encoding='utf-8').split('\n')

    # Pass 1: Remove intra-day ---
    cleaned: list[str] = []
    removed = 0
    for i, line in enumerate(lines):
        if line.strip() == '---':
            j = i + 1
            while j < len(lines) and lines[j].strip() == '':
                j += 1
            if j < len(lines) and _DATE_HEADING_RE.match(lines[j]):
                cleaned.append(line)
            else:
                removed += 1
                if cleaned and cleaned[-1] == '':
                    cleaned.pop()
        else:
            cleaned.append(line)

    # Pass 2: Add missing --- before date headings
    result: list[str] = []
    added = 0
    for line in cleaned:
        if _DATE_HEADING_RE.match(line):
            j = len(result) - 1
            while j >= 0 and result[j].strip() == '':
                j -= 1
            if j >= 0 and result[j].strip() != '---':
                while result and result[-1].strip() == '':
                    result.pop()
                result.extend(['', '---', ''])
                added += 1
        result.append(line)

    path.write_text('\n'.join(result), encoding='utf-8', newline='')
    return removed, added


def _parse_entries(path: Path) -> list[dict]:
    """Parse CHANGELOG into date sections with entries (newest first)."""
    content = path.read_text(encoding='utf-8')
    if not content.strip():
        return []

    sections: list[dict] = []
    date_matches = list(_DATE_HEADING_RE.finditer(content))
    for i, dm in enumerate(date_matches):
        sec_end = date_matches[i + 1].start() if i + 1 < len(date_matches) else len(content)
        sec_text = content[dm.end():sec_end]

        ems = list(_ENTRY_HEADING_RE.finditer(sec_text))
        entries = [
            {
                'heading': em.group(0)[4:],
                'time': em.group(1),
                'title': em.group(2),
                'body': sec_text[em.end():(ems[j + 1].start() if j + 1 < len(ems) else len(sec_text))].strip(),
            }
            for j, em in enumerate(ems)
        ]
        sections.append({'date': dm.group(1), 'entries': entries})
    return sections


def cmd_titles(path: Path, limit: int) -> None:
    """Print recent ### headings grouped by date, newest first."""
    n = 0
    for sec in _parse_entries(path):
        if limit and n >= limit:
            break
        printed_date = False
        for entry in sec['entries']:
            if limit and n >= limit:
                break
            if not printed_date:
                print(f"## {sec['date']}")
                printed_date = True
            print(f"  ### {entry['heading']}")
            n += 1
    if n == 0:
        print("(no entries)", file=sys.stderr)


def cmd_show(path: Path, date: str | None, match: str | None,
             section: str | None, limit: int) -> None:
    """Print full entry content for matching entries."""
    sections = _parse_entries(path)
    if date:
        sections = [s for s in sections if s['date'] == date]
        if not sections:
            print(f"No section for {date}.", file=sys.stderr)
            return

    n = 0
    for sec in sections:
        printed_date = False
        for entry in sec['entries']:
            if limit and n >= limit:
                return
            if match and match.lower() not in entry['heading'].lower():
                continue
            if section and section.lower() not in entry['title'].lower():
                continue
            if not printed_date:
                print(f"## {sec['date']}")
                printed_date = True
            print(f"### {entry['heading']}")
            if entry['body']:
                print()
                print(entry['body'])
            print()
            n += 1

    if n == 0:
        print("No matching entries found.", file=sys.stderr)


def cmd_recent(path: Path, days: int) -> None:
    """Print title tree for the last N days."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    found = False
    for sec in _parse_entries(path):
        if sec['date'] < cutoff:
            break
        print(f"## {sec['date']}")
        for entry in sec['entries']:
            print(f"  ### {entry['heading']}")
        if not sec['entries']:
            print("  (empty)")
        found = True
    if not found:
        print(f"No entries in the last {days} day(s).", file=sys.stderr)


def main():
    log_script_run()
    parser = argparse.ArgumentParser(description='CHANGELOG 条目追加工具')
    sub = parser.add_subparsers(dest='command')

    parser.add_argument('--time', default=None, help='YYYY-MM-DD HH:MM（缺省取当前时间）')
    parser.add_argument('--prefix', choices=['agent', 'user', 'manual'], help='条目前缀')
    parser.add_argument('--title', help='条目标题')
    content_group = parser.add_mutually_exclusive_group()
    content_group.add_argument('--content', help='条目内容（\\n 分隔多条），- 表示 stdin')
    content_group.add_argument('--content-file', help='从文件读取内容')

    sub.add_parser('normalize', help='清理日内 --- 分隔线，仅保留日期节间的 ---')

    p_titles = sub.add_parser('titles', help='输出最近 N 条 ### 标题')
    p_titles.add_argument('--limit', type=int, default=0, help='限制输出条目数（0=全部）')

    p_show = sub.add_parser('show', help='查看特定条目内容')
    show_filter = p_show.add_mutually_exclusive_group(required=True)
    show_filter.add_argument('--date', metavar='YYYY-MM-DD', help='查看指定日期的条目')
    show_filter.add_argument('--match', help='搜索标题包含关键词的条目')
    p_show.add_argument('--section', help='进一步筛选标题包含关键词的条目')
    p_show.add_argument('--limit', type=int, default=0, help='限制输出条目数（0=全部）')

    p_recent = sub.add_parser('recent', help='最近 N 天的标题树')
    p_recent.add_argument('--days', type=int, default=7, help='天数（默认 7）')

    args = parser.parse_args()
    path = VAULT_ROOT / 'docs' / 'CHANGELOG.md'

    if args.command == 'normalize':
        removed, added = normalize_separators(path)
        print(f"Removed {removed} intra-day separator(s), added {added} inter-date separator(s).")
        return
    if args.command == 'titles':
        cmd_titles(path, args.limit)
        return
    if args.command == 'show':
        cmd_show(path, args.date, args.match, args.section, args.limit)
        return
    if args.command == 'recent':
        cmd_recent(path, args.days)
        return

    if not args.prefix or not args.title or (args.content is None and args.content_file is None):
        parser.error('--prefix, --title, and --content/--content-file are required for append mode')

    if args.time is None:
        args.time = datetime.now().strftime('%Y-%m-%d %H:%M')
    else:
        m = re.match(r'(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::\d{2})?', args.time)
        if m:
            args.time = f"{m.group(1)} {m.group(2)}:{m.group(3)}"

    if args.content_file:
        raw = Path(args.content_file).read_text(encoding='utf-8')
    elif args.content == '-':
        raw = sys.stdin.read()
    else:
        raw = args.content.replace('\\n', '\n')

    bullets = [line for line in raw.split('\n') if line.strip()]
    if not bullets:
        print("No content provided, skipping.", file=sys.stderr)
        sys.exit(1)

    if insert_changelog_entry(path, args.time, args.prefix, args.title, bullets):
        print(f"Entry appended: {args.time} · {args.prefix} {args.title}")
    else:
        print("No entry written (empty bullets).", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    # --- host guard ---
    if not is_primary_host():
        print("FATAL: this script must run on PRIMARY_HOST", file=sys.stderr)
        sys.exit(1)
    # --- /host guard ---
    main()
