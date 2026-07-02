#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
changelog_append.py — CHANGELOG 条目追加工具

用法:
    python .meta/scripts/changelog_append.py \\
        --time "2026-04-27 14:30" \\
        --prefix agent \\
        --title "功能：xxx" \\
        --content "第一条摘要\\n第二条摘要"

    # 或从文件读取内容:
    python .meta/scripts/changelog_append.py \\
        --time "2026-04-27 14:30" --prefix agent --title "xxx" \\
        --content-file /tmp/bullets.txt

    # 或从 stdin 读取:
    echo "第一条\\n第二条" | python .meta/scripts/changelog_append.py \\
        --time "2026-04-27 14:30" --prefix agent --title "xxx" --content -
"""

import sys
import re
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import VAULT_ROOT, log_script_run


def _strip_trailing_separators(text: str) -> str:
    """Remove trailing ````---```` separator lines and surrounding blanks."""
    lines = text.rstrip().split('\n')
    while lines and lines[-1].strip() == '---':
        lines.pop()
        while lines and lines[-1].strip() == '':
            lines.pop()
    return '\n'.join(lines)


def _ensure_bullet(b: str) -> str:
    """Return b as a bullet point; preserve an existing '- '/'* ' marker instead of doubling it."""
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
    """
    Insert a changelog entry at the correct HH:MM-sorted position.

    Args:
        path:   Path to CHANGELOG.md
        time:   "YYYY-MM-DD HH:MM"
        prefix: "agent" | "user" | "manual"
        title:  Entry title (without prefix)
        bullets: List of bullet point strings

    Returns:
        True if written, False if bullets is empty.
    """
    if not bullets:
        return False

    date_part = time[:10]  # YYYY-MM-DD
    date_heading = f"## {date_part}"
    heading = f"### {time} · {prefix} {title}"
    block_lines = [heading, ''] + [_ensure_bullet(b) for b in bullets] + ['', '']
    block = '\n'.join(block_lines)

    content = path.read_text(encoding='utf-8')

    # --- Locate date section ---
    date_match = re.search(
        rf'^{re.escape(date_heading)}\s*$', content, re.MULTILINE
    )

    if date_match:
        # Date section exists — find section boundaries
        rest = content[date_match.end():]
        next_date = re.search(r'^##\s+\d{4}-\d{2}-\d{2}\s*$', rest, re.MULTILINE)
        section_end = date_match.end() + (next_date.start() if next_date else len(rest))
        section_text = content[date_match.end():section_end]

        # Find insertion point by HH:MM ascending order
        # Collect all ### headings with their positions (relative to section start)
        entry_pattern = re.compile(r'^### (\d{4}-\d{2}-\d{2} \d{2}:\d{2})', re.MULTILINE)
        insert_pos = None
        for m in entry_pattern.finditer(section_text):
            existing_time = m.group(1)
            if time < existing_time:
                # Insert before this entry — convert to absolute position
                insert_pos = date_match.end() + m.start()
                break

        if insert_pos is not None:
            # Insert before the entry at insert_pos
            before = content[:insert_pos].rstrip()
            after = content[insert_pos:].lstrip('\n')
            updated = before + '\n\n' + block.rstrip('\n')
            if after:
                updated += '\n\n' + after
            else:
                updated += '\n'
        else:
            # Append to end of date section (new entry is later than all existing)
            before = _strip_trailing_separators(content[:section_end])
            after = content[section_end:].lstrip('\n')
            updated = before + '\n\n' + block.rstrip('\n')
            if after:
                updated += '\n\n---\n\n' + after
            else:
                updated += '\n'
    else:
        # Date section does not exist — create it
        new_section = f"{date_heading}\n\n{block}".rstrip('\n')
        first_date = re.search(r'^##\s+\d{4}-\d{2}-\d{2}\s*$', content, re.MULTILINE)
        if first_date:
            # Insert before first existing date (reverse chronological)
            # Strip accumulated --- from prefix, then add exactly two:
            # one before the new date section, one before the old first date section
            prefix_text = _strip_trailing_separators(content[:first_date.start()])
            updated = (prefix_text + '\n\n---\n\n' + new_section
                       + '\n\n---\n\n' + content[first_date.start():])
        else:
            # No date sections at all — append to end
            prefix_text = _strip_trailing_separators(content)
            updated = prefix_text + '\n\n---\n\n' + new_section

    path.write_text(updated, encoding='utf-8', newline='')
    return True


def normalize_separators(path: Path) -> tuple[int, int]:
    """
    Remove intra-day --- and add missing inter-date ---.

    Rule: --- only appears immediately before a ## YYYY-MM-DD heading.
    - Intra-day --- (not followed by a date heading): removed.
    - Missing --- before a date heading: inserted.

    Returns (removed_count, added_count).
    """
    content = path.read_text(encoding='utf-8')
    lines = content.split('\n')
    date_heading_re = re.compile(r'^##\s+\d{4}-\d{2}-\d{2}\s*$')

    # Pass 1: Remove intra-day ---
    cleaned: list[str] = []
    removed = 0

    for i, line in enumerate(lines):
        if line.strip() == '---':
            # Look ahead: skip blank lines, check if next non-blank is a date heading
            j = i + 1
            while j < len(lines) and lines[j].strip() == '':
                j += 1
            if j < len(lines) and date_heading_re.match(lines[j]):
                cleaned.append(line)  # Keep --- before date heading
            else:
                removed += 1  # Remove intra-day ---
                # Also consume one trailing blank line if present
                if cleaned and cleaned[-1] == '':
                    cleaned.pop()
        else:
            cleaned.append(line)

    # Pass 2: Add missing --- before date headings
    result: list[str] = []
    added = 0

    for line in cleaned:
        if date_heading_re.match(line):
            # Look back through result for existing ---
            j = len(result) - 1
            while j >= 0 and result[j].strip() == '':
                j -= 1
            needs_sep = j >= 0 and result[j].strip() != '---'

            if needs_sep:
                # Strip trailing blanks, then insert --- + blanks
                while result and result[-1].strip() == '':
                    result.pop()
                result.append('')
                result.append('---')
                result.append('')
                added += 1

        result.append(line)

    path.write_text('\n'.join(result), encoding='utf-8', newline='')
    return removed, added


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def _parse_entries(path: Path) -> list[dict]:
    """Parse CHANGELOG into a list of date sections with entries (newest first)."""
    content = path.read_text(encoding='utf-8')
    if not content.strip():
        return []

    date_re = re.compile(r'^## (\d{4}-\d{2}-\d{2})\s*$', re.MULTILINE)
    heading_re = re.compile(
        r'^### (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) · (.+)$', re.MULTILINE
    )

    sections: list[dict] = []
    date_matches = list(date_re.finditer(content))

    for i, dm in enumerate(date_matches):
        sec_start = dm.end()
        sec_end = (date_matches[i + 1].start()
                   if i + 1 < len(date_matches) else len(content))
        sec_text = content[sec_start:sec_end]

        entries: list[dict] = []
        ems = list(heading_re.finditer(sec_text))
        for j, em in enumerate(ems):
            e_start = em.end()
            e_end = ems[j + 1].start() if j + 1 < len(ems) else len(sec_text)
            entries.append({
                'heading': em.group(0)[4:],   # strip "### "
                'time': em.group(1),
                'title': em.group(2),
                'body': sec_text[e_start:e_end].strip(),
            })

        sections.append({'date': dm.group(1), 'entries': entries})

    return sections


def cmd_titles(path: Path, limit: int) -> None:
    """Print recent ### headings grouped by date, newest first."""
    sections = _parse_entries(path)
    n = 0
    for sec in sections:
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
            heading_lower = entry['heading'].lower()
            title_lower = entry['title'].lower()
            if match and match.lower() not in heading_lower:
                continue
            if section and section.lower() not in title_lower:
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
    from datetime import datetime, timedelta

    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    sections = _parse_entries(path)
    found = False
    for sec in sections:
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

    # Default (no subcommand): append entry
    parser.add_argument('--time', default=None, help='YYYY-MM-DD HH:MM（缺省取当前时间）')
    parser.add_argument('--prefix', choices=['agent', 'user', 'manual'], help='条目前缀')
    parser.add_argument('--title', help='条目标题')
    content_group = parser.add_mutually_exclusive_group()
    content_group.add_argument('--content', help='条目内容（\\n 分隔多条），- 表示 stdin')
    content_group.add_argument('--content-file', help='从文件读取内容')

    # Subcommand: normalize separators
    sub.add_parser('normalize', help='清理日内 --- 分隔线，仅保留日期节间的 ---')

    # Subcommand: titles
    p_titles = sub.add_parser('titles', help='输出最近 N 条 ### 标题')
    p_titles.add_argument('--limit', type=int, default=0,
                          help='限制输出条目数（0=全部）')

    # Subcommand: show
    p_show = sub.add_parser('show', help='查看特定条目内容')
    show_filter = p_show.add_mutually_exclusive_group(required=True)
    show_filter.add_argument('--date', metavar='YYYY-MM-DD',
                             help='查看指定日期的条目')
    show_filter.add_argument('--match',
                             help='搜索标题包含关键词的条目')
    p_show.add_argument('--section',
                        help='进一步筛选标题包含关键词的条目')
    p_show.add_argument('--limit', type=int, default=0,
                        help='限制输出条目数（0=全部）')

    # Subcommand: recent
    p_recent = sub.add_parser('recent', help='最近 N 天的标题树')
    p_recent.add_argument('--days', type=int, default=7,
                          help='天数（默认 7）')

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

    # Append mode — validate required args
    if not args.prefix or not args.title or (args.content is None and args.content_file is None):
        parser.error('--prefix, --title, and --content/--content-file are required for append mode')

    # Default time to now
    if args.time is None:
        from datetime import datetime
        args.time = datetime.now().strftime('%Y-%m-%d %H:%M')
    else:
        # Normalize ISO-8601 / T-separated input to "YYYY-MM-DD HH:MM"
        import re as _re
        m = _re.match(r'(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::\d{2})?', args.time)
        if m:
            args.time = f"{m.group(1)} {m.group(2)}:{m.group(3)}"

    # Resolve bullets
    if args.content_file:
        raw = Path(args.content_file).read_text(encoding='utf-8')
    elif args.content == '-':
        raw = sys.stdin.read()
    else:
        # Unescape literal \n from shell argument
        raw = args.content.replace('\\n', '\n')

    bullets = [line for line in raw.split('\n') if line.strip()]

    if not bullets:
        print("No content provided, skipping.", file=sys.stderr)
        sys.exit(1)

    ok = insert_changelog_entry(path, args.time, args.prefix, args.title, bullets)
    if ok:
        print(f"Entry appended: {args.time} · {args.prefix} {args.title}")
    else:
        print("No entry written (empty bullets).", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    # --- host guard ---
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from common import is_primary_host
    if not is_primary_host():
        print("FATAL: this script must run on PRIMARY_HOST", file=sys.stderr)
        sys.exit(1)
    # --- /host guard ---
    main()
