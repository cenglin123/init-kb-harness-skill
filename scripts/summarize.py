#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
summarize.py — 为每篇笔记生成摘要与 tag 建议

属 LLM 层（HARNESS_MODE=full + DeepSeek）。语义关联（.meta/links/）由 build_links.py
独立生成（纯余弦、无 LLM），两者互不依赖。

用法: python .meta/scripts/summarize.py [--full]
"""

import sys
import re
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    VAULT_ROOT, scan_notes, scan_office_extracts, rel_path,
    meta_mirror, ensure_parent, DeepSeekClient,
    MAINTAIN_CONCURRENCY, llm_pipeline_check, llm_enabled, ScanCache,
)

VOCAB_PATH = VAULT_ROOT / '.meta' / 'tag-vocab.json'

SUMMARY_PROMPT = """给下面的笔记同时生成摘要和标签。

要求：
1. 1-2 句摘要（不超过 80 字），突出主题和核心观点
2. 3-5 个层级 tag（格式 #category/subcategory），优先从现有高频 tag 中选，仅当笔记明显引入新主题时才造新 tag

现有高频 tag：{vocab}

笔记内容：
{content}

按以下格式输出：
摘要：<摘要文字>
标签：#tag1 #tag2 #tag3"""


def load_vocab() -> dict:
    if VOCAB_PATH.exists():
        try:
            return json.loads(VOCAB_PATH.read_text(encoding='utf-8'))
        except:
            return {}
    return {}


def save_vocab(vocab: dict):
    VOCAB_PATH.write_text(json.dumps(vocab, ensure_ascii=False, indent=2), encoding='utf-8')


def extract_user_tags(content: str) -> list:
    tags = []
    for line in content.splitlines()[:20]:
        for m in re.finditer(r'(?<![a-zA-Z0-9/_-])#([a-zA-Z0-9_\u4e00-\u9fff][\w\u4e00-\u9fff/_-]*)', line):
            tags.append(m.group(1))
    return sorted(set(tags))


def write_summary(rel, h, summary, model):
    p = meta_mirror(VAULT_ROOT / rel, 'summaries')
    ensure_parent(p)
    p.write_text(
        f"---\n"
        f"source: {rel}\n"
        f"content_hash: {h}\n"
        f"generated_at: {datetime.now().isoformat()}\n"
        f"model: {model}\n"
        f"---\n\n"
        f"{summary}\n",
        encoding='utf-8'
    )


def write_tags(rel, h, user_tags, agent_tags, model):
    p = meta_mirror(VAULT_ROOT / rel, 'tags')
    ensure_parent(p)
    lines = [
        "---",
        f"source: {rel}",
        f"content_hash: {h}",
        f"generated_at: {datetime.now().isoformat()}",
        f"model: {model}",
        "---",
        "",
        "## Agent 建议 tag",
    ]
    if agent_tags:
        for t in agent_tags:
            lines.append(f"- #{t}")
    else:
        lines.append("- （无）")
    lines.append("")
    lines.append("## 用户已有 tag（扫自原文）")
    if user_tags:
        for t in user_tags:
            lines.append(f"- #{t}")
    else:
        lines.append("- （无）")
    lines.append("")
    p.write_text("\n".join(lines), encoding='utf-8')


def already_up_to_date(sidecar: Path, h: str) -> bool:
    if not sidecar.exists():
        return False
    try:
        return f"content_hash: {h}" in sidecar.read_text(encoding='utf-8')
    except:
        return False


def main(full=False):
    if not llm_enabled():
        print("  ✗ LLM 层未启用（HARNESS_MODE=full + DEEPSEEK_API_KEY）", file=sys.stderr)
        return 1
    try:
        client = DeepSeekClient()
    except RuntimeError as e:
        print(f"  ✗ {e}——请在 .env 配置后重跑维护")
        return 1
    vocab = load_vocab()

    # office 提取 sidecar 与普通笔记同等处理。其 summaries/tags 镜像会落在
    # .meta/summaries/.meta/office-extracts/... —— 嵌套是有意的：写读两侧统一走
    # meta_mirror，且与 check_sidecar_sources 的 source 契约（镜像相对路径）一致。
    scan_cache = ScanCache()
    notes = []
    for md in list(scan_notes()) + list(scan_office_extracts()):
        rel = rel_path(md)
        # 大文件闸门（硬约束）：超过阈值且无 allow 决定的文件不进 LLM 管线
        allowed, reason = llm_pipeline_check(md)
        if not allowed:
            size_mb = md.stat().st_size / 1048576
            print(f"  [挂起·{reason}] {rel}（{size_mb:.2f} MB 超阈值）——本轮不生成摘要/tag/关联，待用户决定")
            continue
        try:
            h = scan_cache.get_content_hash(md, rel)
        except Exception as e:
            print(f"    [skip] {rel}: {e}")
            continue
        notes.append((md, rel, h))
    scan_cache.save()
    print(f"  共 {len(notes)} 篇笔记（含 office 提取）")

    # 第一轮：摘要 + tag（合并为一次 API 调用；跨文件并发）
    pending = []
    for md, rel, h in notes:
        summary_file = meta_mirror(md, 'summaries')
        if not full and already_up_to_date(summary_file, h):
            continue
        try:
            content = md.read_text(encoding='utf-8')
        except Exception as e:
            print(f"    [skip] {rel}: {e}")
            continue
        pending.append((rel, h, content))

    print(f"  ── 生成摘要 + tag（{len(pending)} 篇 · 并发 {MAINTAIN_CONCURRENCY}）──")
    # vocab 在本轮开始时取快照供 prompt 使用；计数更新回主线程串行执行
    top_vocab = sorted(vocab.items(), key=lambda x: -x[1])[:30]
    vocab_str = " ".join(f"#{t}" for t, _ in top_vocab) or "(空)"

    def _summarize_one(item):
        rel, h, content = item
        preview = content[:2000]
        user_tags = extract_user_tags(content)
        summary = "(摘要生成失败)"
        agent_tags = []
        try:
            raw = client.chat([{
                "role": "user",
                "content": SUMMARY_PROMPT.format(vocab=vocab_str, content=preview)
            }], max_tokens=300)
            sm = re.search(r'摘要[：:]\s*(.+?)$', raw, re.MULTILINE)
            tm = re.search(r'标签[：:]\s*(.+?)$', raw, re.MULTILINE)
            if sm:
                summary = sm.group(1).strip()
            if tm:
                agent_tags = [m.group(1) for m in re.finditer(r'#([\w一-鿿/_-]+)', tm.group(1))][:5]
            if not sm and not tm:
                summary = raw.strip()[:200]
        except Exception as e:
            print(f"    摘要+tag {rel}: {e}")
        return rel, h, summary, agent_tags, user_tags

    done = 0
    with ThreadPoolExecutor(max_workers=MAINTAIN_CONCURRENCY) as pool:
        futures = {pool.submit(_summarize_one, it): it[0] for it in pending}
        for fut in as_completed(futures):
            done += 1
            rel, h, summary, agent_tags, user_tags = fut.result()
            print(f"  [{done}/{len(pending)}] {rel}")
            for t in agent_tags + user_tags:
                vocab[t] = vocab.get(t, 0) + 1
            write_summary(rel, h, summary, client.model)
            write_tags(rel, h, user_tags, agent_tags, client.model)

    save_vocab(vocab)
    print("  ✓ 完成 summaries / tags")
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
    full = '--full' in sys.argv
    sys.exit(main(full=full))
