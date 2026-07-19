#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
health_report.py — 生成仓库健康报告

生成:
- .meta/health-report.md   (快照型，每次覆盖)
- .meta/health-trend.csv   (追加型，时间序列)

用法: python .meta/scripts/health_report.py
"""

import sys
import csv
import json
from datetime import datetime
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from common import VAULT_ROOT, scan_notes, rel_path
from inbox_scan import main as inbox_scan_main

REPORT_PATH = VAULT_ROOT / '.meta' / 'health-report.md'
ORPHANS_PATH = VAULT_ROOT / '.meta' / 'orphans.md'
GRAPH_PATH = VAULT_ROOT / '.meta' / 'graph.json'
KNOWLEDGE_MAP_PATH = VAULT_ROOT / '.meta' / 'knowledge-map.md'
TREND_PATH = VAULT_ROOT / '.meta' / 'health-trend.csv'

# ─── 参数化阈值（env 可覆盖）──────────────────────────────────────────────────
import os as _os
ORPHAN_ALERT_THRESHOLD = int(_os.environ.get('ORPHAN_ALERT_THRESHOLD', '20'))
INBOX_ALERT_THRESHOLD = int(_os.environ.get('INBOX_ALERT_THRESHOLD', '5'))
SPARSE_CATEGORY_MIN = int(_os.environ.get('SPARSE_CATEGORY_MIN', '3'))
ARCHIVE_MARKERS = tuple(m.strip() for m in _os.environ.get('ARCHIVE_MARKERS', '归档').split(',') if m.strip())

TOP_EXCLUDES = {
    'AGENTS.md', 'CLAUDE.md', 'GEMINI.md',
    'README.md', '.env', '.env.example', '.gitignore',
}


def count_notes():
    total = 0
    by_category = defaultdict(list)
    for md in scan_notes():
        total += 1
        rel = rel_path(md)
        parts = rel.split('/')
        cat = parts[0] if len(parts) > 1 else '(收件箱)'
        by_category[cat].append({
            'path': rel,
            'filename': md.name,
        })
    return total, by_category


def find_inbox():
    """根目录下的非分类 .md 文件"""
    inbox = []
    for p in VAULT_ROOT.iterdir():
        if not p.is_file() or p.suffix != '.md':
            continue
        if p.name in TOP_EXCLUDES:
            continue
        try:
            lines = len(p.read_text(encoding='utf-8').splitlines())
        except:
            lines = 0
        inbox.append({'name': p.name, 'lines': lines})
    return inbox


def load_orphans_from_graph():
    """从 .meta/graph.json 派生孤儿：in_degree_wiki == 0 的节点。

    仅看用户手写链接（排除 Agent 生成的 semantic 边），符合宪法
    Article II 作者分离原则。in_degree_wiki 涵盖 wikilink [[target]]
    和 markdown [text](target.md) 两种语法。解析经过 Obsidian 官方算法
    （含 frontmatter aliases + basename 最短路径），比旧版 stem 直接比对更准确。

    依赖：.meta/graph.json 必须存在（由 build_graph.py 生成）。
    """
    if not GRAPH_PATH.exists():
        raise RuntimeError(
            f"{GRAPH_PATH.relative_to(VAULT_ROOT)} 不存在。"
            "请先运行 `python .meta/scripts/build_graph.py`。"
        )
    try:
        graph = json.loads(GRAPH_PATH.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"无法读取 graph.json: {e}")

    return [n['id'] for n in graph['nodes'] if n.get('in_degree_wiki', 0) == 0]


def is_archived(path):
    """路径的任一目录段含 ARCHIVE_MARKERS 中任一标记 → 归档区。
    覆盖任意 ARCHIVE_MARKERS 目录下（默认含'归档'）。只看目录部分，不看文件名。"""
    parts = path.split('/')
    return any(marker in seg for marker in ARCHIVE_MARKERS for seg in parts[:-1])


def classify_orphans(orphans):
    """把孤儿分成活跃区 / 归档区两组。"""
    active, archived = [], []
    for p in orphans:
        (archived if is_archived(p) else active).append(p)
    return active, archived


def _group_by_category(paths):
    grouped = defaultdict(list)
    for p in paths:
        cat = p.split('/')[0] if '/' in p else '(根目录)'
        grouped[cat].append(p)
    return grouped


def build_orphans_report(active, archived):
    """生成 .meta/orphans.md 的完整内容（快照型，全量覆盖）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(active) + len(archived)

    lines = [
        "# 孤儿笔记清单",
        "",
        f"> 快照生成于: {now}",
        f"> 总计: **{total}** 篇（活跃 {len(active)} · 归档 {len(archived)}）",
        "",
        "**定义**：`in_degree_wiki == 0` 的节点——未被任何**用户手写**链接引用的笔记。涵盖 `[[wikilink]]` 和 `[text](target.md)` 两种语法。",
        "",
        "**数据来源**：`.meta/graph.json`（由 `build_graph.py` 按 Obsidian 官方算法解析 wikilink 与 markdown 链接，含 frontmatter aliases + basename 最短路径匹配）。",
        "",
        "**注意**：Agent 生成的 semantic 边**不**计入入度判定（宪法 Article II 作者分离原则）。",
        "",
        "**分区说明**：",
        "- **活跃区孤儿**：真正可能需要建立关联的笔记，优先处理。",
        "- **归档区孤儿**：位于任意归档目录下（默认标记含「归档」），无反链属正常状态，仅供审计。",
        "",
        "---",
        "",
        "## 活跃区孤儿",
        "",
    ]

    if active:
        grouped = _group_by_category(active)
        for cat in sorted(grouped.keys()):
            items = sorted(grouped[cat])
            lines.append(f"### `{cat}` — {len(items)} 篇")
            lines.append("")
            for p in items:
                lines.append(f"- `{p}`")
            lines.append("")
    else:
        lines.append("- （无）")
        lines.append("")

    lines.extend([
        "---",
        "",
        "## 归档区孤儿",
        "",
        "> 位于「归档」目录下，通常无需建立关联，仅供审计。",
        "",
    ])

    if archived:
        grouped = _group_by_category(archived)
        for cat in sorted(grouped.keys()):
            items = sorted(grouped[cat])
            lines.append(f"### `{cat}` — {len(items)} 篇")
            lines.append("")
            for p in items:
                lines.append(f"- `{p}`")
            lines.append("")
    else:
        lines.append("- （无）")
        lines.append("")

    return "\n".join(lines)


def find_sparse_categories(by_category, threshold=SPARSE_CATEGORY_MIN):
    return {cat: items for cat, items in by_category.items() if len(items) < threshold}


def load_deviation_signals():
    """从 .meta/knowledge-map.md 提取偏差信号段（## 与分类的偏差信号）。
    返回偏差信号行列表；knowledge-map.md 不存在时返回空列表。"""
    if not KNOWLEDGE_MAP_PATH.exists():
        return []
    try:
        content = KNOWLEDGE_MAP_PATH.read_text(encoding='utf-8')
    except Exception:
        return []

    signals = []
    in_section = False
    for line in content.splitlines():
        if line.strip() == '## 与分类的偏差信号（建议关注）':
            in_section = True
            continue
        if in_section:
            if line.startswith('## ') or (not line.strip()):
                if signals and not line.strip():
                    continue
                if line.startswith('## '):
                    break
                continue
            if line.startswith('- '):
                signals.append(line)
    return signals


def build_report(total, by_category, inbox, orphans, active_orphans, archived_orphans, sparse, deviation_signals=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        "# 仓库健康报告",
        "",
        f"> 快照生成于: {now}",
        "",
        "## 总览指标",
        "",
        f"- 笔记总数: **{total}** 篇",
        f"- 分类覆盖: {len(by_category)} 个活跃分类",
        f"- 孤儿笔记: {len(orphans)} 篇（活跃 {len(active_orphans)} · 归档 {len(archived_orphans)}）",
        f"- 收件箱待归类: {len(inbox)} 项",
        f"- 稀疏分类: {len(sparse)} 个（< {SPARSE_CATEGORY_MIN} 篇）",
        "",
        "## 告警",
        "",
    ]

    alerts = []
    if len(inbox) > INBOX_ALERT_THRESHOLD:
        alerts.append(f"- ⚠️ 收件箱积压 {len(inbox)} 项，建议归类")
    if len(active_orphans) > ORPHAN_ALERT_THRESHOLD:
        alerts.append(f"- ⚠️ 活跃区孤儿 {len(active_orphans)} 篇，建议建立关联（归档区 {len(archived_orphans)} 篇不计入告警）")
    if sparse:
        cats = ', '.join(sorted(sparse.keys()))
        alerts.append(f"- ℹ️ 稀疏分类: {cats}")
    if not alerts:
        alerts.append("- ✅ 无异常")

    lines.extend(alerts)
    lines.append("")

    # 收件箱
    lines.extend([
        "## 收件箱待归类",
        "",
    ])
    if inbox:
        for item in inbox:
            lines.append(f"- `{item['name']}` — {item['lines']} 行")
            lines.append(f"  - **建议**: {item.get('suggestion', '未分析')}")
            lines.append(f"  - **依据**: {item.get('reason', '')}")
            lines.append("")
    else:
        lines.append("- （空）")
        lines.append("")

    # 脚本收集
    lines.extend(_section_script_collection())

    # 孤儿笔记（摘要，全量清单见 .meta/orphans.md）
    lines.extend([
        "## 孤儿笔记（未被引用）",
        "",
        f"- 总计: **{len(orphans)}** 篇",
        f"- 活跃区: **{len(active_orphans)}** 篇（优先关注）",
        f"- 归档区: **{len(archived_orphans)}** 篇（通常可忽略）",
        "",
        "完整清单见 [[.meta/orphans]]。",
        "",
    ])

    # 稀疏分类
    lines.extend([
        "## 稀疏分类（< 3 篇）",
        "",
    ])
    if sparse:
        for cat, items in sorted(sparse.items()):
            lines.append(f"- `{cat}` — {len(items)} 篇")
        lines.append("")
    else:
        lines.append("- （无）")
        lines.append("")

    # 分类详情
    lines.extend([
        "## 分类详情",
        "",
    ])
    for cat in sorted(by_category.keys()):
        count = len(by_category[cat])
        lines.append(f"- `{cat}` — {count} 篇")
    lines.append("")

    # 归档建议占位
    lines.extend([
        "## 归档建议",
        "",
        "_（此节待实现：基于 180 天未动 + 0 反链的自动判定）_",
        "",
    ])

    # 偏差信号（来自 knowledge-map.md）
    deviation_signals = deviation_signals or []
    lines.extend([
        "## 待人工判断",
        "",
        "> 来自 [[.meta/knowledge-map]] 偏差信号。Agent 只观察不裁决。",
        "",
    ])
    if deviation_signals:
        for sig in deviation_signals:
            lines.append(sig)
        lines.append("")
    else:
        lines.append("- （无偏差信号）")
        lines.append("")

    return "\n".join(lines)


def _section_script_collection():
    """扫描脚本收集文件夹，报告缺少说明文档的脚本（纯文件扫描，不调用嵌入 API）。

    目录位置通过 env SCRIPT_COLLECTION_DIR 配置（相对 VAULT_ROOT）。
    默认空（不扫）；目标仓库在 .env 设 SCRIPT_COLLECTION_DIR 启用脚本收集扫描。
    空值或文件夹不存在则静默跳过。"""
    # 默认空；目标仓库在 .env 设 SCRIPT_COLLECTION_DIR 启用脚本收集扫描
    scripts_dir_rel = _os.environ.get('SCRIPT_COLLECTION_DIR', '')
    lines = [
        "## 脚本收集",
        "",
    ]
    if not scripts_dir_rel:
        lines.append("- （未配置 SCRIPT_COLLECTION_DIR，跳过）")
        lines.append("")
        return lines
    scripts_dir = VAULT_ROOT / scripts_dir_rel
    if not scripts_dir.exists() or not scripts_dir.is_dir():
        lines.append("- 文件夹不存在")
        lines.append("")
        return lines

    py_files = sorted(scripts_dir.glob('*.py'))
    if not py_files:
        lines.append("- （无脚本）")
        lines.append("")
        return lines

    missing_docs = []
    ok_count = 0
    for py_file in py_files:
        stem = py_file.stem
        # 匹配 _说明文档.md（如 compress_xlsx_images_说明文档.md）
        doc_candidates = list(scripts_dir.glob(f'{stem}_说明文档*.md'))
        if not doc_candidates:
            missing_docs.append(py_file.name)
        else:
            ok_count += 1

    lines.append(f"- 脚本总数: {len(py_files)}")
    lines.append(f"- 有说明文档: {ok_count}")
    lines.append(f"- 缺说明文档: {len(missing_docs)}")
    lines.append("")

    if missing_docs:
        for name in missing_docs:
            lines.append(f"- ⚠️ `{name}` — 缺少说明文档，建议注册")
        lines.append("")

    return lines


def append_trend(total, orphans):
    now = datetime.now().strftime("%Y-%m-%d")
    row = [now, str(total), str(orphans)]
    header = ["date", "total_notes", "orphans"]

    if not TREND_PATH.exists():
        with open(TREND_PATH, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(header)

    with open(TREND_PATH, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(row)


def main():
    print("  ── 生成健康报告 ──")

    total, by_category = count_notes()
    orphans = load_orphans_from_graph()
    active_orphans, archived_orphans = classify_orphans(orphans)
    sparse = find_sparse_categories(by_category)
    deviation_signals = load_deviation_signals()

    # 收件箱扫描（带 LLM 建议）；API 不可用时降级为空清单，不阻断报告
    try:
        inbox = inbox_scan_main()
    except Exception as e:
        print(f"  ⚠️  收件箱扫描不可用（{e}），本轮跳过 LLM 归类建议")
        inbox = []

    # 写 health-report.md（摘要）
    report = build_report(total, by_category, inbox, orphans, active_orphans, archived_orphans, sparse, deviation_signals)
    REPORT_PATH.write_text(report, encoding='utf-8')

    # 写 orphans.md（全量清单，按活跃/归档分区 + 按分类分组）
    orphans_doc = build_orphans_report(active_orphans, archived_orphans)
    ORPHANS_PATH.write_text(orphans_doc, encoding='utf-8')

    append_trend(total, len(orphans))

    print(f"  ✓ health-report.md ({total} 篇, 孤儿 {len(orphans)}={len(active_orphans)}活+{len(archived_orphans)}档, {len(inbox)} 收件箱)")
    print(f"  ✓ orphans.md")
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
