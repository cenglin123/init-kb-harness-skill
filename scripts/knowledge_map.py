#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
knowledge_map.py — 消费 graph.json，渲染 .meta/knowledge-map.md

派生型快照：纯计算，零 API 调用。每次 build_graph.py 运行后触发覆盖。
职责：god nodes、跨域意外连接、社区一览、分类偏差信号。
不修改 graph.json 本身，只读不写图谱。

用法：python .meta/scripts/knowledge_map.py
"""

import sys
import json
from datetime import datetime
from pathlib import Path
from collections import defaultdict, Counter

sys.path.insert(0, str(Path(__file__).parent))
from common import VAULT_ROOT, ENV

GRAPH_PATH = VAULT_ROOT / '.meta' / 'graph.json'
KNOWLEDGE_MAP_PATH = VAULT_ROOT / '.meta' / 'knowledge-map.md'

SCHEMA_VERSION = 1

# 日记/日志类目录标记（用于"日记社区凝结新主题"信号；按仓库分类命名自定义）
JOURNAL_MARKERS = [
    m.strip() for m in ENV.get('JOURNAL_MARKERS', '日记,journal,diary,log').split(',')
    if m.strip()
]


# ─── 工具 ─────────────────────────────────────────────────────────────────

def load_graph():
    if not GRAPH_PATH.exists():
        raise RuntimeError(
            f"{GRAPH_PATH.relative_to(VAULT_ROOT)} 不存在。"
            "请先运行 `python .meta/scripts/build_graph.py`。"
        )
    return json.loads(GRAPH_PATH.read_text(encoding='utf-8'))


def _category_of(path: str) -> str:
    parts = path.split('/')
    return parts[0] if len(parts) > 1 else '(收件箱)'


def _extract_tags_from_graph(nodes_by_id: dict, node_id: str) -> list:
    """从 node category 推导简单分类标签（不读原文，零 IO）。"""
    return []


# ─── 核心计算 ─────────────────────────────────────────────────────────────

def compute_god_nodes(graph: dict, top_n: int = 15) -> list:
    """按总入度 (wiki + semantic) 排序的 top-N 节点。"""
    nodes = graph['nodes']
    scored = []
    for n in nodes:
        if n.get('archived'):
            continue
        in_wiki = n.get('in_degree_wiki', 0)
        in_sem = n.get('in_degree_semantic', 0)
        total_in = in_wiki + in_sem
        if total_in == 0:
            continue
        scored.append({
            'id': n['id'],
            'basename': n['basename'],
            'category': n.get('category', ''),
            'in_wiki': in_wiki,
            'in_sem': in_sem,
            'total_in': total_in,
            'community_id': n.get('community_id', -1),
        })
    scored.sort(key=lambda x: x['total_in'], reverse=True)
    return scored[:top_n]


def compute_cross_category_surprises(graph: dict, top_n: int = 10) -> list:
    """跨分类的 INFERRED 边，按 weight 降序 top-N。"""
    edges = graph['edges']
    nodes_by_id = {n['id']: n for n in graph['nodes']}

    cross = []
    seen_pairs = set()
    for e in edges:
        if e.get('confidence') != 'INFERRED':
            continue
        s_id, t_id = e['source'], e['target']
        s_node = nodes_by_id.get(s_id)
        t_node = nodes_by_id.get(t_id)
        if not s_node or not t_node:
            continue
        s_cat = s_node.get('category', '')
        t_cat = t_node.get('category', '')
        if s_cat == t_cat:
            continue
        # Deduplicate undirected pairs
        pair = tuple(sorted([s_id, t_id]))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        cross.append({
            'source': s_id,
            'source_cat': s_cat,
            'target': t_id,
            'target_cat': t_cat,
            'weight': e.get('weight', 0),
        })
    cross.sort(key=lambda x: x['weight'], reverse=True)
    return cross[:top_n]


def compute_community_stats(graph: dict, top_n: int = 15) -> list:
    """社区统计：size、cohesion、主导分类、top tags、枢纽节点。"""
    nodes = graph['nodes']
    nodes_by_id = {n['id']: n for n in nodes}
    edges = graph['edges']

    # 按社区 ID 分组活跃节点
    communities = defaultdict(list)
    for n in nodes:
        cid = n.get('community_id', -1)
        if cid >= 0:
            communities[cid].append(n)

    # 构建邻接用于计算 cohesion
    adj = defaultdict(set)
    for e in edges:
        s, t = e['source'], e['target']
        adj[s].add(t)
        adj[t].add(s)

    stats = []
    for cid in sorted(communities.keys()):
        members = communities[cid]
        if not members:
            continue
        size = len(members)

        # Cohesion: intra-community edges / max possible
        member_ids = {n['id'] for n in members}
        intra_edges = 0
        for mid in member_ids:
            for neighbor in adj.get(mid, set()):
                if neighbor in member_ids:
                    intra_edges += 1
        intra_edges //= 2  # undirected double-count
        possible = size * (size - 1) / 2
        cohesion = round(intra_edges / possible, 2) if possible > 0 else 0.0

        # 主导分类
        cat_counts = Counter(n.get('category', '') for n in members)
        dominant = cat_counts.most_common()

        # 枢纽节点：按 in_degree_wiki + in_degree_semantic 在社区内排序
        by_in = sorted(members,
                       key=lambda n: n.get('in_degree_wiki', 0) + n.get('in_degree_semantic', 0),
                       reverse=True)
        hubs = by_in[:5]

        stats.append({
            'id': cid,
            'size': size,
            'cohesion': cohesion,
            'categories': dominant,
            'hubs': hubs,
        })

    stats.sort(key=lambda x: x['size'], reverse=True)
    return stats[:top_n]


def compute_deviation_signals(community_stats: list) -> list:
    """检测社区内跨分类混合信号，生成偏差建议。"""
    signals = []
    for cs in community_stats:
        cats = cs['categories']
        if len(cats) < 2:
            continue
        total = cs['size']
        top_pct = round(cats[0][1] / total * 100)
        second_pct = round(cats[1][1] / total * 100)
        # 偏差信号：前两分类都 > 30%
        if second_pct >= 30:
            signals.append({
                'community_id': cs['id'],
                'size': total,
                'top_cat': cats[0][0],
                'top_pct': top_pct,
                'second_cat': cats[1][0],
                'second_pct': second_pct,
                'cohesion': cs['cohesion'],
                'type': 'cross-category',
            })
        # 日记社区 cohesion > 0.25 → 可能凝结出新主题
        elif any(any(mk.lower() in c[0].lower() for mk in JOURNAL_MARKERS)
                 for c in cats[:1]) and cs['cohesion'] > 0.25:
            signals.append({
                'community_id': cs['id'],
                'size': total,
                'top_cat': cats[0][0],
                'cohesion': cs['cohesion'],
                'type': 'diary-theme',
            })
    return signals


# ─── 渲染 ────────────────────────────────────────────────────────────────

def render_knowledge_map(graph: dict) -> str:
    now = datetime.now().replace(microsecond=0).isoformat()
    nodes = graph['nodes']
    edges = graph['edges']

    # 概览统计
    total_nodes = len(nodes)
    total_edges = len(edges)
    wiki_edges = sum(1 for e in edges if e.get('type') == 'user-wikilink')
    sem_edges = total_edges - wiki_edges

    conf_counts = Counter(e.get('confidence', 'UNKNOWN') for e in edges)
    total_e = total_edges or 1
    ext_pct = round(conf_counts.get('EXTRACTED', 0) / total_e * 100)
    inf_pct = round(conf_counts.get('INFERRED', 0) / total_e * 100)
    amb_pct = round(conf_counts.get('AMBIGUOUS', 0) / total_e * 100)

    active_communities = set(
        n.get('community_id', -1) for n in nodes
        if n.get('community_id', -1) >= 0
    )
    n_communities = len(active_communities)

    lines = [
        '---',
        'type: knowledge-map',
        f'source: build_graph.py + knowledge_map.py',
        f'generated_at: {now}',
        f'schema_version: {SCHEMA_VERSION}',
        '---',
        '',
        '# 知识地图',
        '',
        f'> 派生型快照。基于 .meta/graph.json 计算，每次维护覆盖。',
        f'> 这是"知识视角"，与 health-report.md 的"维护视角"互补。',
        '',
        '## 概览',
        f'- 节点 {total_nodes} · 边 {total_edges}（wiki {wiki_edges} + semantic {sem_edges}）· 社区 {n_communities}',
        f'- 置信度构成：EXTRACTED {ext_pct}% · INFERRED {inf_pct}% · AMBIGUOUS {amb_pct}%',
        '',
    ]

    # 核心枢纽 (God Nodes)
    god_nodes = compute_god_nodes(graph)
    lines.append('## 核心枢纽（God Nodes · 按总入度排序 top-15）')
    lines.append('')
    lines.append('| 笔记 | 分类 | 入度 (wiki+sem) | 所在社区 |')
    lines.append('|---|---|---|---|')
    for gn in god_nodes:
        lines.append(
            f"| [[{gn['id']}]] | {gn['category']} | "
            f"{gn['total_in']} ({gn['in_wiki']}+{gn['in_sem']}) | "
            f"C{gn['community_id']} |"
        )
    lines.append('')

    # 跨域意外连接
    surprises = compute_cross_category_surprises(graph)
    lines.append('## 跨域意外连接（Top-10 · 跨分类的 INFERRED 边）')
    lines.append('')
    if surprises:
        for s in surprises:
            lines.append(
                f"- [[{s['source']}]] ⟷ [[{s['target']}]] "
                f"(sim {s['weight']:.2f}) · {s['source_cat']} ↔ {s['target_cat']}"
            )
    else:
        lines.append('- （无跨分类 INFERRED 边）')
    lines.append('')

    # 社区一览
    community_stats = compute_community_stats(graph)
    lines.append('## 社区一览（活跃节点 · top-15 by size）')
    lines.append('')
    for cs in community_stats:
        cat_str = ' + '.join(
            f"{cat}({round(cnt / cs['size'] * 100)}%)"
            for cat, cnt in cs['categories'][:3]
        )
        lines.append(f"### Community {cs['id']} ({cs['size']} nodes · cohesion {cs['cohesion']})")
        lines.append(f"**主导分类**：{cat_str}")
        hub_names = ', '.join(f"[[{h['id']}]]" for h in cs['hubs'][:3])
        lines.append(f"**枢纽节点**：{hub_names}")
        lines.append('（无 LLM 命名，仅事实统计）')
        lines.append('')

    # 偏差信号
    signals = compute_deviation_signals(community_stats)
    lines.append('## 与分类的偏差信号（建议关注）')
    lines.append('')
    if signals:
        for sig in signals:
            if sig['type'] == 'cross-category':
                lines.append(
                    f"- Community {sig['community_id']} 同时跨 "
                    f"{sig['top_cat']}({sig['top_pct']}%) 和 "
                    f"{sig['second_cat']}({sig['second_pct']}%)"
                )
                lines.append('  → 可能存在跨分类主题，或某一篇放错位置')
            elif sig['type'] == 'diary-theme':
                lines.append(
                    f"- Community {sig['community_id']} 全部来自 {sig['top_cat']}，"
                    f"但 cohesion {sig['cohesion']}"
                )
                lines.append('  → 日记中可能凝结出新主题，候选 P3 合成')
    else:
        lines.append('- （无显著偏差信号）')
    lines.append('')

    return '\n'.join(lines)


# ─── 主流程 ──────────────────────────────────────────────────────────────

def main():
    print('  ── 生成知识地图 ──')

    graph = load_graph()

    content = render_knowledge_map(graph)
    KNOWLEDGE_MAP_PATH.write_text(content, encoding='utf-8')

    # Stats
    nodes = graph['nodes']
    edges = graph['edges']
    god_nodes = compute_god_nodes(graph)
    surprises = compute_cross_category_surprises(graph)
    community_stats = compute_community_stats(graph)
    signals = compute_deviation_signals(community_stats)

    print(f'  ✓ knowledge-map.md ({len(nodes)} nodes, {len(edges)} edges)')
    print(f'  ✓ god nodes: {len(god_nodes)}, surprises: {len(surprises)}')
    print(f'  ✓ communities: {len(community_stats)}, deviation signals: {len(signals)}')
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
