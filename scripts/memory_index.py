#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memory_index.py — MEMORY.md 记忆索引自动维护（硬约束，禁止手改索引段）

扫描 .meta/memory/{project,reference,user,workflows,feedback}/ 下的记忆文件，
自动重建 MEMORY.md 中 <!-- memory-index:start/end --> 标记之间的"当前记忆条目"段，
保证索引始终反映记忆系统真实内容，不依赖 agent 记得手动维护。

调用方：
    - maintain.py 每次维护自动运行
    - pre-commit hook：.meta/memory/** 有变更且索引过期时拦截提交

用法:
    python .meta/scripts/memory_index.py           # 重建索引段（原地更新 MEMORY.md）
    python .meta/scripts/memory_index.py --check   # 只检查不写入；索引过期时 exit 1
"""

import sys
import argparse
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import VAULT_ROOT, scan_memory_notes, rel_path, log_script_run

MEMORY_MD = VAULT_ROOT / '.meta' / 'memory' / 'MEMORY.md'

MARK_START = '<!-- memory-index:start -->'
MARK_END = '<!-- memory-index:end -->'

# 索引分类（展示顺序固定）；不在表内的顶层目录归入"其他"
CATEGORIES = ['project', 'reference', 'user', 'workflows', 'feedback']

SKIP_NAMES = {'MEMORY.md', 'README.md'}


def _split_frontmatter(text: str):
    """切分 frontmatter。返回 (fm_block, body)；无闭合分隔符返回 (None, None) 表示非法。"""
    if not text.startswith('---'):
        return '', text
    closing = re.compile(r'^---[ \t]*$', flags=re.MULTILINE).search(text, 3)
    if not closing:
        return None, None
    return text[3:closing.start()], text[closing.start():]


def extract_title(md_path: Path) -> str:
    """标题提取优先级：frontmatter title → 首个 # 标题 → 文件名 stem。"""
    try:
        text = md_path.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return md_path.stem
    fm_block, body = _split_frontmatter(text)
    if fm_block is None or body is None:
        # frontmatter 未闭合（非法格式），保守取 stem，避免把 fm 内 # 注释行当标题
        return md_path.stem
    if fm_block:
        m = re.search(r'^title:[ \t]*(.+)$', fm_block, flags=re.MULTILINE)
        if m:
            title = m.group(1).strip().strip('"\'').strip()
            if title:
                return title
    m = re.search(r'^#\s+(.+)$', body, flags=re.MULTILINE)
    if m:
        return m.group(1).strip()
    return md_path.stem


def extract_field(md_path: Path, key: str) -> str:
    """从 frontmatter 提取单字段（status / last_updated 等），无则返回空串。"""
    try:
        text = md_path.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return ''
    fm_block, _ = _split_frontmatter(text)
    if not fm_block:
        return ''
    m = re.search(rf'^{re.escape(key)}:[ \t]*(.+)$', fm_block, flags=re.MULTILINE)
    return m.group(1).strip().strip('"\'') if m else ''


def build_index_section() -> str:
    """生成标记段内的索引内容（不含标记行本身）。"""
    grouped = {cat: [] for cat in CATEGORIES}
    others = []

    for md in scan_memory_notes():
        if md.name in SKIP_NAMES:
            continue
        rel = rel_path(md)  # 形如 .meta/memory/project/xxx.md
        parts = Path(rel).parts  # ('.meta', 'memory', <cat>, ...)
        cat = parts[2] if len(parts) > 3 else None
        title = extract_title(md)
        status = extract_field(md, 'status')
        last_updated = extract_field(md, 'last_updated') or extract_field(md, 'generated_at')

        meta_bits = []
        if status:
            meta_bits.append(status)
        if last_updated:
            meta_bits.append(last_updated[:10])
        suffix = f"（{' · '.join(meta_bits)}）" if meta_bits else ''

        link = f"[[{md.stem}|{title}]]" if title != md.stem else f"[[{md.stem}]]"
        entry = f"- {link}{suffix} — `{rel}`"
        if cat in grouped:
            grouped[cat].append(entry)
        else:
            others.append(entry)

    lines = []
    for cat in CATEGORIES:
        lines.append(f"### {cat}/")
        entries = sorted(grouped[cat])
        lines.extend(entries if entries else ['- （暂无）'])
        lines.append('')
    if others:
        lines.append('### 其他')
        lines.extend(sorted(others))
        lines.append('')
    return '\n'.join(lines).rstrip()


def render(text: str, section: str) -> str:
    """用新索引段替换标记段；无标记时追加到文件末尾。"""
    block = f"{MARK_START}\n{section}\n{MARK_END}"
    pattern = re.compile(
        re.escape(MARK_START) + r'.*?' + re.escape(MARK_END),
        flags=re.DOTALL,
    )
    if pattern.search(text):
        return pattern.sub(block, text, count=1)
    sep = '' if text.endswith('\n\n') else ('\n' if text.endswith('\n') else '\n\n')
    return f"{text}{sep}---\n\n{block}\n"


def main():
    parser = argparse.ArgumentParser(description='MEMORY.md 记忆索引自动维护')
    parser.add_argument('--check', action='store_true',
                        help='只检查索引是否过期（不写入）；过期时 exit 1')
    args = parser.parse_args()
    if not args.check:
        log_script_run()  # --check 是 pre-commit 只读检查，不写 usage-log 避免污染工作区

    if not MEMORY_MD.exists():
        print(f"  [skip] {rel_path(MEMORY_MD)} 不存在（memory 骨架未安装）")
        return 0

    old_text = MEMORY_MD.read_text(encoding='utf-8')
    section = build_index_section()
    new_text = render(old_text, section)

    if args.check:
        if new_text != old_text:
            print("✗ MEMORY.md 记忆索引已过期（与 .meta/memory/ 实际内容不一致）")
            print("  修复：python .meta/scripts/memory_index.py && git add .meta/memory/MEMORY.md")
            return 1
        print("✓ MEMORY.md 记忆索引为最新")
        return 0

    if new_text == old_text:
        print("✓ MEMORY.md 记忆索引无变化")
        return 0

    MEMORY_MD.write_text(new_text, encoding='utf-8', newline='')
    print("✓ MEMORY.md 记忆索引已重建")
    return 0


if __name__ == '__main__':
    sys.exit(main())
