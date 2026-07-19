#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
synthesize.py — 主题合成分析

基于多篇笔记生成主题层面的综合分析，写入 .meta/syntheses/。

用法:
    python .meta/scripts/synthesize.py --theme "某主题综述" --scope "某分类目录/2026*.md"
    python .meta/scripts/synthesize.py --theme "年度总结" --scope "工作/*.md,日记/2026*.md"
    python .meta/scripts/synthesize.py --theme "测试" --scope "*.md" --prompt "按时间线梳理"

注意:
    - 合成写作属于深层分析，脚本负责素材聚合与 DeepSeek 调用
    - 重要主题建议在生成后由当前 Agent 审查润色
"""

import sys
import re
import argparse
import fnmatch
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import VAULT_ROOT, scan_notes, rel_path, ensure_parent, DeepSeekClient, log_script_run


def glob_notes(scope_pattern: str) -> list[Path]:
    """解析 scope 模式（逗号分隔的 glob），返回匹配的笔记列表"""
    patterns = [p.strip() for p in scope_pattern.split(',')]
    matched = set()

    for pattern in patterns:
        # 支持两种形式：
        # 1. "某分类/2026*.md" — 相对 vault 根的路径 glob
        # 2. "*.md" — 全局 glob
        for note in scan_notes():
            r = rel_path(note)
            if fnmatch.fnmatch(r, pattern) or fnmatch.fnmatch(note.name, pattern):
                matched.add(note)

    return sorted(matched, key=lambda p: rel_path(p))


def read_note_content(note: Path, max_chars: int = 1500) -> str:
    """读取笔记内容，优先读 summary，否则读原文前 max_chars 字"""
    # 尝试读 summary
    from common import meta_mirror
    sp = meta_mirror(note, 'summaries')
    if sp.exists():
        try:
            text = sp.read_text(encoding='utf-8')
            parts = text.split('---', 2)
            body = parts[2].strip() if len(parts) >= 3 else text.strip()
            return body[:max_chars]
        except Exception:
            pass

    # fallback：读原文
    try:
        text = note.read_text(encoding='utf-8', errors='replace')
        # 去掉 frontmatter
        if text.startswith('---'):
            end = text.find('---', 3)
            if end != -1:
                text = text[end + 3:]
        return text[:max_chars].strip()
    except Exception as e:
        return f"[读取失败: {e}]"


def build_synthesis_prompt(theme: str, notes_data: list[dict], user_prompt: str = "") -> str:
    """构建发给 DeepSeek 的合成提示词"""
    base_prompt = user_prompt or (
        f'基于以下笔记，生成一份关于「{theme}」的综合分析。'
        '要求：\n'
        '1. 按主题逻辑组织，而非按笔记罗列\n'
        '2. 提炼共同观点、演变脉络或关键结论\n'
        '3. 标注信息来源（用 `文件名` 格式）\n'
        '4. 使用 Markdown 格式，层级清晰\n'
    )

    parts = [base_prompt, '\n--- 素材笔记 ---\n']
    for item in notes_data:
        parts.append(f"\n【{item['path']}】\n{item['content']}\n")

    return '\n'.join(parts)


def sanitize_filename(name: str) -> str:
    """把主题名转成文件名安全的字符串"""
    s = re.sub(r'[^\u4e00-\u9fff\w\s\-]', '', name)
    s = re.sub(r'\s+', '-', s.strip())
    return s or 'synthesis'


def write_synthesis(theme: str, content: str, scope: str, model: str):
    """写入 .meta/syntheses/<主题>.md"""
    now = datetime.now()
    filename = f"{sanitize_filename(theme)}.md"
    path = VAULT_ROOT / '.meta' / 'syntheses' / filename

    # 检查 governance 状态：若 review_status == reviewed，禁止覆盖
    status_path = VAULT_ROOT / '.meta' / 'syntheses-status' / filename
    if path.exists() and status_path.exists():
        try:
            status_text = status_path.read_text(encoding='utf-8')
            if 'review_status: reviewed' in status_text:
                # 生成新文件名
                filename = f"{sanitize_filename(theme)}-v2.md"
                path = VAULT_ROOT / '.meta' / 'syntheses' / filename
                status_path = VAULT_ROOT / '.meta' / 'syntheses-status' / filename
                print(f"⚠️  已 reviewed 的 synthesis 不覆盖，写入新文件: {filename}")
        except Exception:
            pass

    # 判断 confidence（基于素材数量启发式）
    confidence = 'low'

    frontmatter = (
        '---\n'
        f'source: {scope}\n'
        f'type: synthesis\n'
        f'generated_at: {now.isoformat()}\n'
        f'model: {model}\n'
        f'scope: {scope}\n'
        f'status: draft\n'
        f'confidence: {confidence}\n'
        f'confidence_reason: "自动生成，待人工评审"\n'
        f'review_status: unreviewed\n'
        f'provenance_level: synthesis-level\n'
        f'supersedes: []\n'
        f'superseded_by: []\n'
        '---\n\n'
    )

    header = f'# {theme}\n\n'
    disclaimer = (
        '> 本文件由 Agent 基于仓库内笔记自动生成，非用户原文。'
        '按主题逻辑组织，标记关键观点和演进脉络。\n\n'
    )

    full = frontmatter + header + disclaimer + content

    ensure_parent(path)
    path.write_text(full, encoding='utf-8', newline='')

    # 方案 A：写入 syntheses-status（持久化治理状态）
    ensure_parent(status_path)
    status_fm = (
        '---\n'
        f'synthesis: {filename}\n'
        f'status: draft\n'
        f'confidence: {confidence}\n'
        f'confidence_reason: "自动生成，待人工评审"\n'
        f'review_status: unreviewed\n'
        f'provenance_level: synthesis-level\n'
        f'supersedes: []\n'
        f'superseded_by: []\n'
        f'updated_at: {now.isoformat()}\n'
        '---\n'
    )
    status_path.write_text(status_fm, encoding='utf-8', newline='')

    return path


def main():
    log_script_run()
    parser = argparse.ArgumentParser(description='主题合成分析')
    parser.add_argument('--theme', required=True, help='合成主题名称')
    parser.add_argument('--scope', required=True, help='笔记范围（glob 模式，逗号分隔）')
    parser.add_argument('--prompt', default='', help='自定义合成提示词（覆盖默认）')
    parser.add_argument('--max-notes', type=int, default=30, help='最多处理多少篇笔记（默认30）')
    parser.add_argument('--max-chars', type=int, default=1200, help='每篇笔记最大字符数（默认1200）')
    args = parser.parse_args()

    print(f"=== 主题合成: {args.theme} ===")
    print(f"范围: {args.scope}")

    notes = glob_notes(args.scope)
    print(f"匹配到 {len(notes)} 篇笔记")

    if not notes:
        print("⚠️  没有匹配的笔记，退出")
        return 1

    if len(notes) > args.max_notes:
        print(f"⚠️  笔记过多，只取前 {args.max_notes} 篇")
        notes = notes[:args.max_notes]

    # 读取素材
    print("读取笔记内容...")
    notes_data = []
    for note in notes:
        content = read_note_content(note, max_chars=args.max_chars)
        notes_data.append({
            'path': rel_path(note),
            'content': content,
        })

    # 构建提示词
    prompt = build_synthesis_prompt(args.theme, notes_data, args.prompt)

    # 调用 DeepSeek
    print("调用 DeepSeek 生成合成...")
    client = DeepSeekClient()
    messages = [
        {"role": "system", "content": "你是一位知识库分析师，擅长从多篇笔记中提取主题脉络，生成结构化的综合分析。"},
        {"role": "user", "content": prompt},
    ]

    try:
        content = client.chat(messages, temperature=0.5, max_tokens=4096)
    except Exception as e:
        print(f"✗ DeepSeek 调用失败: {e}")
        return 1

    # 写入文件
    path = write_synthesis(args.theme, content, args.scope, client.model)
    print(f"✓ 合成已写入: {rel_path(path)}")
    return 0


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
    sys.exit(main())
