#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_graph.py — 全量重建 .meta/graph.json（L3 关联与发现层）

派生型产物：每次全量重建，无增量（无 API 调用，耗时 < 5 秒）。

边类型：
  - user-wikilink (weight=1.0) — 扫原文 [[xxx]] 与 [text](target.md)，按 Obsidian 算法解析
  - semantic      (weight>0.75) — 读 .meta/links/，抽 sim 分数

节点字段：
  id, basename, category, archived, size_lines, mtime, aliases,
  in_degree_wiki, in_degree_semantic, out_degree_wiki, out_degree_semantic

副产物：
  .meta/broken-links.md — 解析失败的链接清单（定位到源文件 + 行号，区分 wiki/md 语法）

设计原则：
  - 复刻 Obsidian 官方 wikilink 解析算法（basename 最短路径优先）
  - 尊重 frontmatter aliases
  - 不存 stats 块（派生数据，由消费方自算；仅打印到 stdout）
  - 稀疏化：semantic 边 weight > 0.75 才保留（宪法 Article 6）

用法：python .meta/scripts/build_graph.py
"""

import sys
import re
import json
import inspect
from datetime import datetime
from pathlib import Path
from collections import defaultdict, deque
from urllib.parse import unquote
import posixpath

import networkx as nx

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    VAULT_ROOT, scan_notes, rel_path,
    WIKILINK_RE, MDLINK_RE,
    find_all_wikilinks, find_all_mdlinks,
    iter_markdown_content_lines, INTERNAL_ATTACHMENT_EXTS,
    extract_frontmatter, parse_aliases,
    pick_closest_by_prefix, resolve_wikilink, resolve_mdlink,
    is_primary_host, SEMANTIC_THRESHOLD,
)

GRAPH_PATH = VAULT_ROOT / '.meta' / 'graph.json'
BROKEN_LINKS_PATH = VAULT_ROOT / '.meta' / 'broken-links.md'
BROKEN_LINKS_IGNORE_PATH = VAULT_ROOT / '.meta' / 'broken-links-ignore.md'
LINKS_DIR = VAULT_ROOT / '.meta' / 'links'

SCHEMA_VERSION = 2
# 归档目录关键词（与 health_report.py ARCHIVE_MARKERS 同源），env 可覆盖
import os as _os
ARCHIVE_KEYWORDS = tuple(
    m.strip() for m in _os.environ.get('ARCHIVE_MARKERS', '归档').split(',') if m.strip()
) or ('归档',)
CLIP_JUNK_TARGETS = {'↩', 'link', 'PDF', 'HTML'}
META_DOC_FILES = {'AGENTS.md', 'CLAUDE.md', 'GEMINI.md', 'README.md', 'CHANGELOG.md'}

# ─── 正则 ─────────────────────────────────────────────────────────────────────

# Semantic 边匹配：`- [[target]] — sim 0.83 [— reason]`
# 兼容 em-dash / hyphen，兼容 `相似度` / `sim`
# group(1)=target, group(2)=score, group(3)=reason (可能 None)
SEMANTIC_LINE_RE = re.compile(
    r'^\s*-\s+\[\[([^\[\]|#\n]+?)(?:[|#][^\]\n]*)?\]\]'      # - [[target]]
    r'\s*[—\-]\s*(?:sim|相似度)\s*([0-9.]+)'                    # — sim 0.83
    r'(?:\s*[—,，]\s*(.+?))?\s*$',                              # — reason（可选）
    re.MULTILINE
)


# ─── 工具 ─────────────────────────────────────────────────────────────────────

def is_archived(path: str) -> bool:
    """路径任一目录段含 ARCHIVE_KEYWORDS 中任一标记 → 归档区。与 health_report.py 一致。"""
    parts = path.split('/')
    return any(kw in seg for kw in ARCHIVE_KEYWORDS for seg in parts[:-1])


def _clean_link_target(raw: str) -> str:
    """解码并剥离锚点/查询参数，保留 posix 风格路径。"""
    t = unquote(raw.strip()).split('#')[0].split('?', 1)[0].strip()
    return t.replace('\\', '/')


def _target_basename(raw: str) -> str:
    return posixpath.basename(_clean_link_target(raw)).strip()


def build_attachment_index() -> dict:
    """建立附件文件名/唯一 stem 索引，仅用于断链归类，不生成 graph edge。"""
    by_key = defaultdict(list)
    for path in VAULT_ROOT.rglob('*'):
        if not path.is_file():
            continue
        if path.suffix.lower() not in INTERNAL_ATTACHMENT_EXTS:
            continue
        rel = rel_path(path)
        if rel.startswith('000【备忘录'):
            continue
        by_key[path.name.lower()].append(rel)
        by_key[path.stem.lower()].append(rel)

    index = {}
    for key, values in by_key.items():
        unique = sorted(set(values))
        if len(unique) == 1:
            index[key] = unique
    return index


def classify_broken_link(display: str, raw: str, source: str, reason: str,
                         syntax: str, attachment_index: dict) -> dict:
    """将 unresolved link 归入报告分类。分类只影响报告，不改变 graph edge。"""
    clean = _clean_link_target(raw)
    base = _target_basename(raw)
    result = {
        'category': 'REAL-BROKEN',
        'display': display,
        'raw': raw,
        'source': source,
        'reason': reason,
        'syntax': syntax,
    }

    if syntax == 'wiki' and raw.strip() in CLIP_JUNK_TARGETS:
        result['category'] = 'CLIP-JUNK'
        return result

    clean_no_dot = clean.lstrip('./')
    if clean_no_dot.startswith('docs/') or base in META_DOC_FILES:
        result['category'] = 'META-DOC'
        return result

    if is_archived(source) or '/archived/' in source:
        result['category'] = 'ARCHIVED'
        return result

    attachment = attachment_index.get(base.lower())
    if attachment:
        result['category'] = 'ATTACHMENT-EXISTS'
        result['attachment'] = attachment[0]
        return result

    if syntax == 'md' and (raw.strip().startswith('[') or '](' in raw):
        result['category'] = 'SYNTAX-NOISE'
        return result

    if (
        syntax == 'wiki'
        and '/' not in clean
        and not Path(clean).suffix
        and len(clean) <= 30
    ):
        result['category'] = 'CONCEPT-CANDIDATE'
        return result

    return result


# ─── 索引构建 ─────────────────────────────────────────────────────────────────

def build_indexes():
    """遍历所有笔记，返回：
      - notes_meta: {path: {basename, size_lines, mtime, aliases, content}}
      - basename_index: {basename: [paths...]}
      - aliases_index: {alias: path}  # 别名冲突时后者覆盖前者（罕见）
      - all_paths: set of path
    """
    notes_meta = {}
    basename_index = defaultdict(list)
    aliases_index = {}
    all_paths = set()

    for md in scan_notes():
        rel = rel_path(md)
        try:
            content = md.read_text(encoding='utf-8')
        except Exception:
            content = ''
        try:
            mtime_ts = md.stat().st_mtime
            mtime = datetime.fromtimestamp(mtime_ts).replace(microsecond=0).isoformat()
        except Exception:
            mtime = ''

        aliases = parse_aliases(content)

        notes_meta[rel] = {
            'basename': md.stem,
            'size_lines': len(content.splitlines()),
            'mtime': mtime,
            'aliases': aliases,
            'content': content,  # 扫链接时复用，避免二次 IO
        }
        basename_index[md.stem].append(rel)
        for a in aliases:
            if a and a not in aliases_index:
                aliases_index[a] = rel
        all_paths.add(rel)

    return notes_meta, basename_index, aliases_index, all_paths


# ─── 边扫描 ──────────────────────────────────────────────────────────────────

def scan_wikilink_edges(notes_meta, basename_index, aliases_index, all_paths):
    """扫所有原文 [[xxx]]，产出 user-wikilink 边 + 断链清单。
    同一 (source, target) 合并，count 累加。自引用跳过。
    断链条目为 5 元组：(display, raw_target, line_no, reason, 'wiki')"""
    edge_map = {}   # (source, target) → {weight, count, raw_target}
    broken = defaultdict(list)  # source → [(display, raw_target, line_no, reason, syntax)]

    for source, meta in notes_meta.items():
        content = meta['content']
        for line_no, line in iter_markdown_content_lines(content):
            for m in WIKILINK_RE.finditer(line):
                raw = m.group(1).strip()
                if not raw:
                    continue

                target, reason = resolve_wikilink(
                    raw, source, basename_index, aliases_index, all_paths
                )
                if target is None:
                    broken[source].append(('', raw, line_no, reason, 'wiki'))
                    continue
                if target == source:
                    continue  # 自引用不建边

                key = (source, target)
                if key in edge_map:
                    edge_map[key]['count'] += 1
                else:
                    edge_map[key] = {
                        'source': source,
                        'target': target,
                        'type': 'user-wikilink',
                        'weight': 1.0,
                        'count': 1,
                        'raw_target': raw,
                        'link_syntax': 'wiki',
                        'confidence': 'EXTRACTED',
                    }
    return list(edge_map.values()), broken


def scan_mdlink_edges(notes_meta, basename_index, aliases_index, all_paths):
    """扫所有原文 [text](target.md)，产出 user-wikilink 边 + 断链清单。
    与 scan_wikilink_edges 并行，边带 link_syntax: "md" 字段。
    断链条目为 5 元组：(display, raw_target, line_no, reason, 'md')"""
    edge_map = {}   # (source, target) → {weight, count, raw_target}
    broken = defaultdict(list)  # source → [(display, raw_target, line_no, reason, syntax)]

    for source, meta in notes_meta.items():
        content = meta['content']
        for line_no, line in iter_markdown_content_lines(content):
            for display, raw in find_all_mdlinks(line):

                target, reason = resolve_mdlink(
                    raw, source, basename_index, aliases_index, all_paths
                )
                if target is None:
                    broken[source].append((display, raw, line_no, reason, 'md'))
                    continue
                if target == source:
                    continue  # 自引用不建边

                key = (source, target)
                if key in edge_map:
                    edge_map[key]['count'] += 1
                else:
                    edge_map[key] = {
                        'source': source,
                        'target': target,
                        'type': 'user-wikilink',
                        'weight': 1.0,
                        'count': 1,
                        'raw_target': raw,
                        'link_syntax': 'md',
                        'confidence': 'EXTRACTED',
                    }
    return list(edge_map.values()), broken


def scan_semantic_edges(all_paths, basename_index, aliases_index):
    """读 .meta/links/*.md，抽 sim > SEMANTIC_THRESHOLD 的边。
    links 文件的 source 在 frontmatter 里声明，不依赖镜像路径推断。"""
    edges = []
    if not LINKS_DIR.exists():
        return edges

    for link_md in LINKS_DIR.rglob('*.md'):
        try:
            content = link_md.read_text(encoding='utf-8')
        except Exception:
            continue

        # 从 frontmatter 里拿 source
        fm = extract_frontmatter(content)
        if not fm:
            continue
        m = re.search(r'^source\s*:\s*(.+)\s*$', fm, re.MULTILINE)
        if not m:
            continue
        source = m.group(1).strip()
        if source not in all_paths:
            continue  # source 已被删除或改名

        # 抽 sim 行
        for m2 in SEMANTIC_LINE_RE.finditer(content):
            raw_target = m2.group(1).strip()
            try:
                weight = float(m2.group(2))
            except ValueError:
                continue
            if weight <= SEMANTIC_THRESHOLD:
                continue
            reason = m2.group(3).strip() if m2.group(3) else None

            target, _ = resolve_wikilink(
                raw_target, source, basename_index, aliases_index, all_paths
            )
            if target is None or target == source:
                continue  # semantic 断链静默（links 里的目标可能已删）

            confidence = 'INFERRED' if weight >= 0.85 else 'AMBIGUOUS'
            edge = {
                'source': source,
                'target': target,
                'type': 'semantic',
                'weight': weight,
                'confidence': confidence,
            }
            if reason:
                edge['reason'] = reason
            edges.append(edge)

    # semantic 去重：同一 (source, target) 取最高 weight
    dedup = {}
    for e in edges:
        k = (e['source'], e['target'])
        if k not in dedup or e['weight'] > dedup[k]['weight']:
            dedup[k] = e
    return list(dedup.values())


# ─── 节点构建 ────────────────────────────────────────────────────────────────

def build_nodes(notes_meta, edges):
    """生成节点列表，带 in/out degree 拆分（wiki vs semantic）。
    注意：in_degree_wiki / out_degree_wiki 涵盖两种用户链接语法（wiki + md）。"""
    in_wiki = defaultdict(int)
    in_sem = defaultdict(int)
    out_wiki = defaultdict(int)
    out_sem = defaultdict(int)

    for e in edges:
        s, t, typ = e['source'], e['target'], e['type']
        if typ == 'user-wikilink':
            out_wiki[s] += 1
            in_wiki[t] += 1
        elif typ == 'semantic':
            out_sem[s] += 1
            in_sem[t] += 1

    nodes = []
    for path, meta in notes_meta.items():
        parts = path.split('/')
        category = parts[0] if len(parts) > 1 else '(收件箱)'
        node = {
            'id': path,
            'basename': meta['basename'],
            'category': category,
            'archived': is_archived(path),
            'size_lines': meta['size_lines'],
            'mtime': meta['mtime'],
            'in_degree_wiki': in_wiki[path],
            'in_degree_semantic': in_sem[path],
            'out_degree_wiki': out_wiki[path],
            'out_degree_semantic': out_sem[path],
        }
        if meta['aliases']:
            node['aliases'] = meta['aliases']
        nodes.append(node)

    return nodes


# ─── 社区检测 ───────────────────────────────────────────────────────────────

def compute_communities(nodes: list, edges: list) -> dict:
    """Louvain 社区检测。返回 {node_id: community_id}。

    规则：
      - 仅对活跃节点（archived=False）且有边的节点聚类
      - 归档节点 → community_id = -2
      - 孤立活跃节点（degree=0）→ community_id = -1
      - 社区编号按 size 降序重分配（0 = 最大社区）
      - 聚类失败时不中断主流程，所有节点 community_id = -1

    兼容性：用 inspect.signature 探测 max_level 参数是否存在（旧版 NX 无）。
    """
    node_ids = {n['id'] for n in nodes}
    archived_ids = {n['id'] for n in nodes if n.get('archived')}

    # 建无向图（仅活跃节点）
    G = nx.Graph()
    for n in nodes:
        if n['id'] not in archived_ids:
            G.add_node(n['id'])
    for e in edges:
        s, t = e['source'], e['target']
        if s not in archived_ids and t not in archived_ids:
            if s in node_ids and t in node_ids:
                G.add_edge(s, t, weight=e.get('weight', 1.0))

    # 隔离归档和孤立节点
    result = {}
    for nid in archived_ids:
        result[nid] = -2

    isolates = [n for n in G.nodes() if G.degree(n) == 0]
    for nid in isolates:
        result[nid] = -1

    # 活跃 + 有连接的子图
    connected = [n for n in G.nodes() if G.degree(n) > 0]
    if not connected:
        # 填充剩余节点
        for n in nodes:
            if n['id'] not in result:
                result[n['id']] = -1
        return result

    subG = G.subgraph(connected)
    try:
        kwargs = {'seed': 42, 'threshold': 1e-4}
        sig = inspect.signature(nx.community.louvain_communities)
        if 'max_level' in sig.parameters:
            kwargs['max_level'] = 10
        communities_raw = nx.community.louvain_communities(subG, **kwargs)

        # 按社区 size 降序排列，分配新 ID
        communities_sorted = sorted(communities_raw, key=len, reverse=True)
        for cid, node_set in enumerate(communities_sorted):
            for nid in node_set:
                result[nid] = cid
    except Exception as e:
        print(f'  ⚠️ 聚类失败（降级为 -1）: {e}')
        for n in nodes:
            if n['id'] not in result:
                result[n['id']] = -1
        return result

    # 填充剩余（活跃但无连接——理论上已由 isolates 覆盖，保险再补一次）
    for n in nodes:
        if n['id'] not in result:
            result[n['id']] = -1

    return result


# ─── 副产物：broken-links.md ────────────────────────────────────────────────

def _merge_broken(broken_a: dict, broken_b: dict) -> dict:
    """合并两组 broken dict（均为 source → [(5-tuple)]），按 source 聚合。"""
    merged = defaultdict(list)
    for d in (broken_a, broken_b):
        for source, items in d.items():
            merged[source].extend(items)
    # 按行号排序
    for source in merged:
        merged[source].sort(key=lambda x: x[2])  # x[2] = line_no
    return dict(merged)


def _classified_broken(broken: dict, attachment_index: dict) -> list:
    rows = []
    for source in sorted(broken.keys()):
        for display, raw, line_no, reason, syntax in broken[source]:
            row = classify_broken_link(
                display, raw, source, reason, syntax, attachment_index
            )
            row['line_no'] = line_no
            rows.append(row)
    return rows


def _format_broken_item(row: dict) -> str:
    line_no = row['line_no']
    display = row['display']
    raw = row['raw']
    reason = row['reason']
    syntax = row['syntax']
    suffix = ''
    if row.get('attachment'):
        suffix = f" → attachment: `{row['attachment']}`"
    if syntax == 'md':
        return f"- L{line_no}: `[{display}]({raw})` — {reason}{suffix}"
    return f"- L{line_no}: `[[{raw}]]` — {reason}{suffix}"


def write_broken_links(broken: dict, attachment_index: dict):
    """生成 broken-links.md。broken 值为 5 元组列表：
    (display, raw_target, line_no, reason, syntax) — syntax ∈ {'wiki', 'md'}"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    rows = _classified_broken(broken, attachment_index)
    total = len(rows)
    by_category = defaultdict(list)
    for row in rows:
        by_category[row['category']].append(row)
    real_count = len(by_category.get('REAL-BROKEN', []))
    affected_sources = {row['source'] for row in rows}

    lines = [
        '# 断链清单（Broken Links）',
        '',
        f'> 快照生成于: {now}',
        f'> 共 **{total}** 条解析异常，涉及 **{len(affected_sources)}** 篇笔记',
        f'> 待修复真实断链（REAL-BROKEN）：**{real_count}** 条',
        '',
        '**说明**：以下链接无法解析为库内笔记节点。报告按治理分类分区，支持 `[[]]` 和 `[]()` 两种语法。可能原因：',
        '- 目标笔记被删除或改名（未同步更新链接）',
        '- 拼写错误',
        '- 带 `/` 的路径错写',
        '- 附件、概念候选、Agent 文档引用或剪藏噪声',
        '',
        '**修正建议**：在 Obsidian 里按 `Ctrl+P` → "Rename" 可触发全库自动替换。',
        '',
        '---',
        '',
    ]

    if not rows:
        lines.append('✅ 无断链。')
        BROKEN_LINKS_PATH.write_text('\n'.join(lines), encoding='utf-8')
        write_broken_links_ignore([], now)
        return

    order = [
        'REAL-BROKEN',
        'CONCEPT-CANDIDATE',
        'META-DOC',
        'ARCHIVED',
        'ATTACHMENT-EXISTS',
        'CLIP-JUNK',
        'SYNTAX-NOISE',
    ]
    titles = {
        'REAL-BROKEN': '待修复真实断链',
        'CONCEPT-CANDIDATE': '概念候选',
        'META-DOC': 'Agent / Meta 文档引用',
        'ARCHIVED': '归档区历史链接',
        'ATTACHMENT-EXISTS': '已存在附件',
        'CLIP-JUNK': '剪藏垃圾',
        'SYNTAX-NOISE': '语法噪声',
    }

    for category in order:
        items = by_category.get(category, [])
        if not items:
            continue
        lines.append(f"## {titles[category]}（{category} · {len(items)} 条）")
        lines.append('')
        current_source = None
        for row in sorted(items, key=lambda r: (r['source'], r['line_no'], r['raw'])):
            if row['source'] != current_source:
                current_source = row['source']
                lines.append(f"### `{current_source}`")
                lines.append('')
            lines.append(_format_broken_item(row))
        lines.append('')

    BROKEN_LINKS_PATH.write_text('\n'.join(lines), encoding='utf-8')
    write_broken_links_ignore(
        [row for row in rows if row['category'] != 'REAL-BROKEN'],
        now,
    )


def write_broken_links_ignore(rows: list, generated_at: str):
    """写派生型 ignore 快照。该文件不是人工长期黑名单。"""
    lines = [
        '---',
        'type: broken-links-ignore-snapshot',
        'source: .meta/scripts/build_graph.py',
        'model: wikilink-scan + mdlink-scan',
        f'generated_at: {generated_at}',
        '---',
        '',
        '# 断链忽略快照',
        '',
        '> 派生型快照：每次 build_graph.py 运行可重写。不是长期人工黑名单。',
        '',
    ]
    if not rows:
        lines.append('✅ 本次无非待修复分类。')
        BROKEN_LINKS_IGNORE_PATH.write_text('\n'.join(lines), encoding='utf-8')
        return

    by_category = defaultdict(list)
    for row in rows:
        by_category[row['category']].append(row)

    for category in sorted(by_category):
        items = by_category[category]
        lines.append(f'## {category}（{len(items)} 条）')
        lines.append('')
        for row in sorted(items, key=lambda r: (r['source'], r['line_no'], r['raw'])):
            lines.append(f"- `{row['source']}` L{row['line_no']}: `{row['raw']}`")
        lines.append('')

    BROKEN_LINKS_IGNORE_PATH.write_text('\n'.join(lines), encoding='utf-8')


# ─── 主流程 ──────────────────────────────────────────────────────────────────

def main():
    print('  ── 构建全局图谱 ──')

    # 1. 建索引
    notes_meta, basename_index, aliases_index, all_paths = build_indexes()
    attachment_index = build_attachment_index()
    total_notes = len(notes_meta)
    alias_count = len(aliases_index)

    # 2. 扫 wikilink 边
    wiki_edges, broken_wiki = scan_wikilink_edges(
        notes_meta, basename_index, aliases_index, all_paths
    )

    # 2.5. 扫 markdown 链接边（与 wiki 并行，产出同型边 + 断链）
    md_edges, broken_md = scan_mdlink_edges(
        notes_meta, basename_index, aliases_index, all_paths
    )

    # 释放 content（大仓库省内存）
    for m in notes_meta.values():
        m.pop('content', None)

    # 3. 扫 semantic 边
    sem_edges = scan_semantic_edges(all_paths, basename_index, aliases_index)

    # 4. 合并 + 构建节点
    all_edges = wiki_edges + md_edges + sem_edges
    nodes = build_nodes(notes_meta, all_edges)

    # 4.5. 社区检测（Louvain）
    community_map = compute_communities(nodes, all_edges)
    for n in nodes:
        n['community_id'] = community_map.get(n['id'], -1)

    # 5. 写 graph.json
    graph = {
        'schema_version': SCHEMA_VERSION,
        'generated_at': datetime.now().replace(microsecond=0).isoformat(),
        'model': 'wikilink-scan + mdlink-scan + embedding-3 + deepseek-v4-flash',
        'config': {
            'semantic_threshold': SEMANTIC_THRESHOLD,
            'archive_keywords': list(ARCHIVE_KEYWORDS),
        },
        'nodes': nodes,
        'edges': all_edges,
    }
    GRAPH_PATH.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )

    # 6. 合并断链 + 写 broken-links.md
    broken = _merge_broken(broken_wiki, broken_md)
    write_broken_links(broken, attachment_index)

    # 7. 打印 stats（不存 json，由消费方自算；这里仅 stdout）
    archived = sum(1 for n in nodes if n['archived'])
    active = total_notes - archived
    orphans_wiki = sum(1 for n in nodes if n['in_degree_wiki'] == 0)
    orphans_active_wiki = sum(
        1 for n in nodes if n['in_degree_wiki'] == 0 and not n['archived']
    )
    broken_count = sum(len(v) for v in broken.values())

    print(f'  ✓ 节点: {total_notes} (活跃 {active} · 归档 {archived})')
    print(f'  ✓ 别名: {alias_count}')
    print(f'  ✓ 边: wiki {len(wiki_edges)} · md {len(md_edges)} · semantic {len(sem_edges)}')
    print(f'  ✓ 断链: {broken_count}')
    print(f'  ✓ 孤儿 (in_degree_wiki==0): {orphans_wiki} (活跃 {orphans_active_wiki})')

    # 社区统计
    active_communities = set(
        n['community_id'] for n in nodes
        if n['community_id'] >= 0
    )
    n_communities = len(active_communities)
    isolates = sum(1 for n in nodes if n['community_id'] == -1)
    if n_communities > 0:
        largest = max(
            sum(1 for n in nodes if n['community_id'] == cid)
            for cid in active_communities
        )
        print(f'  ✓ communities: {n_communities} detected (largest: {largest} nodes, isolates: {isolates})')
    else:
        print(f'  ✓ communities: 0 detected (isolates: {isolates})')

    print(f'  ✓ graph.json + broken-links.md')
    return 0


# ─── 图遍历 API（供 ask.py --neighbors / --path 调用）─────────────────────────

_graph_cache = None  # 模块级惰性缓存 (nodes_dict, edges_list)


def _load_graph():
    """惰性加载 graph.json，返回 (nodes_dict, edges_list)。
    nodes_dict: {node_id: node_dict}，edges_list: [edge_dict, ...]
    模块级缓存，同一进程内仅加载一次。"""
    global _graph_cache
    if _graph_cache is not None:
        return _graph_cache
    if not GRAPH_PATH.exists():
        _graph_cache = ({}, [])
        return _graph_cache
    try:
        graph = json.loads(GRAPH_PATH.read_text(encoding='utf-8'))
        nodes = {n['id']: n for n in graph.get('nodes', [])}
        edges = graph.get('edges', [])
        _graph_cache = (nodes, edges)
        return _graph_cache
    except Exception:
        _graph_cache = ({}, [])
        return _graph_cache


def get_neighbors(node_id, max_hops=2, edge_types=None, min_weight=SEMANTIC_THRESHOLD):
    """查询图邻域：BFS 从 node_id 出发 up to max_hops 跳（有向出边）。

    Args:
        node_id: 目标节点路径（如 "category/sub/xxx.md"，相对 vault root 的 posix 路径）
        max_hops: 最大跳数（默认 2）
        edge_types: 边类型过滤列表，None 使用默认 ['user-wikilink', 'semantic']。
                    匹配逻辑：子串包含（'wiki' 匹配 'user-wikilink'）。
        min_weight: 最小边权重阈值（默认来自 common.SEMANTIC_THRESHOLD，与图构建阈值共享同一来源）

    Returns:
        成功时返回邻域列表 [{path, edge_type, weight, hop}, ...]，按 hop 升序、weight 降序排列。
        孤立节点返回空列表 []。
        图文件缺失时返回 {"error": "graph_not_found", "message": "..."}。
        节点不存在时返回 {"error": "node_not_found", "message": "..."}。
    """
    if edge_types is None:
        edge_types = ['user-wikilink', 'semantic']

    nodes, edges = _load_graph()
    if not nodes:
        if not GRAPH_PATH.exists():
            return {"error": "graph_not_found", "message": "graph.json 不存在"}
        return {"error": "graph_not_found", "message": "graph.json 为空或无节点"}

    if node_id not in nodes:
        return {"error": "node_not_found", "message": f"节点 '{node_id}' 不在图谱中"}

    # 建有向邻接表（出边，按 edge_types 和 min_weight 过滤）
    adj = defaultdict(list)
    for e in edges:
        src = e.get('source', '')
        tgt = e.get('target', '')
        etype = e.get('type', '')
        w = e.get('weight', 1.0)
        # 子串匹配：'wiki' 匹配 'user-wikilink'
        if not any(et in etype for et in edge_types):
            continue
        if w < min_weight:
            continue
        adj[src].append((tgt, etype, w))

    # BFS
    visited = set()
    result = []
    q = deque([(node_id, 0)])  # (node, current_hop)
    visited.add(node_id)

    while q:
        cur, hop = q.popleft()
        if hop >= max_hops:
            continue
        for nxt, etype, w in adj.get(cur, []):
            entry = {"path": nxt, "edge_type": etype, "weight": w, "hop": hop + 1}
            result.append(entry)
            if nxt not in visited:
                visited.add(nxt)
                if hop + 1 < max_hops:
                    q.append((nxt, hop + 1))

    # 按 hop 升序、weight 降序排列
    result.sort(key=lambda x: (x['hop'], -x['weight']))
    return result


def find_shortest_path(source_id, target_id):
    """查找两篇笔记之间的最短路径（无向 BFS——所有边视为无向）。

    Args:
        source_id: 起始节点路径
        target_id: 目标节点路径

    Returns:
        成功时返回路径列表 [{path, edge_type}, ...]，从 source 到 target。
        无路径时返回 {"error": "no_path"}。
        图文件缺失时返回 {"error": "graph_not_found", "message": "..."}。
        源/目标节点不存在时返回对应 error dict。
    """
    nodes, edges = _load_graph()
    if not nodes:
        if not GRAPH_PATH.exists():
            return {"error": "graph_not_found", "message": "graph.json 不存在"}
        return {"error": "graph_not_found", "message": "graph.json 为空或无节点"}

    if source_id not in nodes:
        return {"error": "source_not_found", "message": f"源节点 '{source_id}' 不在图谱中"}
    if target_id not in nodes:
        return {"error": "target_not_found", "message": f"目标节点 '{target_id}' 不在图谱中"}

    if source_id == target_id:
        return [{"path": source_id, "edge_type": "self"}]

    # 建无向邻接表
    adj = defaultdict(list)
    for e in edges:
        src = e.get('source', '')
        tgt = e.get('target', '')
        etype = e.get('type', '')
        adj[src].append((tgt, etype))
        adj[tgt].append((src, etype))  # 无向

    # BFS 找最短路径（记录前驱用于回溯）
    q = deque([source_id])
    predecessor = {source_id: None}  # node_id → (prev_node_id, edge_type)

    while q:
        cur = q.popleft()
        if cur == target_id:
            break
        for nxt, etype in adj.get(cur, []):
            if nxt not in predecessor:
                predecessor[nxt] = (cur, etype)
                q.append(nxt)

    if target_id not in predecessor:
        return {"error": "no_path"}

    # 回溯路径
    path = []
    cur = target_id
    while cur != source_id:
        prev, etype = predecessor[cur]
        path.append({"path": cur, "edge_type": etype})
        cur = prev
    path.append({"path": source_id, "edge_type": "start"})
    path.reverse()
    return path


if __name__ == '__main__':
    # --- host guard ---
    if not is_primary_host():
        print("FATAL: this script must run on PRIMARY_HOST", file=sys.stderr)
        sys.exit(1)
    # --- /host guard ---
    sys.exit(main())
