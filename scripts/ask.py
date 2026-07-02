#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ask.py — 语义查询 / 查重 / 孤儿检测

用法:
    python .meta/scripts/ask.py "问题"                    # 语义检索
    python .meta/scripts/ask.py --check "打算写的主题"     # 写前查重
    python .meta/scripts/ask.py --orphans                  # 孤儿笔记清单
    python .meta/scripts/ask.py --backlinks "目标笔记"      # 反向链接查询
"""

import sys
import re
import sqlite3
import argparse
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    VAULT_ROOT, scan_notes, rel_path, meta_mirror, ensure_parent, ZhipuEmbedClient,
    find_all_wikilinks, find_all_mdlinks, iter_markdown_content_lines,
    WIKILINK_RE, resolve_wikilink, resolve_mdlink, parse_aliases, log_script_run,
    rerank_with_llm,
)
from embed import blob_to_embedding
try:
    from build_graph import get_neighbors, find_shortest_path
except ImportError:
    # build_graph.py 属 Phase 1b bundle；Phase 1a 下 --neighbors/--path 不可用（核心检索不受影响）
    get_neighbors = None
    find_shortest_path = None
try:
    from bm25_index import search as bm25_search
except ImportError:
    # bm25_index.py 属 Phase 2 bundle；Phase 1 下 --bm25/--hybrid 不可用
    bm25_search = None

DB_PATH = VAULT_ROOT / '.meta' / 'embeddings.sqlite'
GRAPH_PATH = VAULT_ROOT / '.meta' / 'graph.json'
SECONDS_PER_YEAR = 365.25 * 86400
# Embeddings 表增长预警阈值（约 10× 当前规模 4,251 行，按 ~860 行/月增速约 3-4 年后触发）
EMBEDDINGS_WARN_THRESHOLD = 40000


@dataclass(frozen=True)
class Backlink:
    source: str
    target: str
    link_type: str
    method: str
    line_no: int | None = None
    raw: str = ""


def slugify(text: str) -> str:
    """把查询字符串转成文件名安全的 slug"""
    # 保留中英文、数字、空格和连字符
    s = re.sub(r'[^\u4e00-\u9fff\w\s\-]', '', text)
    s = re.sub(r'\s+', '-', s.strip())
    return s[:50]  # 限制长度


def fetch_summary(path: str, max_len: int = 200) -> str:
    """从 .meta/summaries/ 读取摘要，失败返回空"""
    sp = meta_mirror(VAULT_ROOT / path, 'summaries')
    if not sp.exists():
        return ""
    try:
        text = sp.read_text(encoding='utf-8')
        parts = text.split('---', 2)
        body = parts[2].strip() if len(parts) >= 3 else text.strip()
        # 取第一行非空内容
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                return line[:max_len]
        return body[:max_len]
    except Exception:
        return ""


def save_query_results(query: str, results: list, is_check: bool):
    """将查询结果保存到 .meta/syntheses/queries/"""
    now = datetime.now()
    timestamp = now.strftime('%Y%m%d-%H%M%S')
    slug = slugify(query) or 'untitled'
    filename = f"query-{timestamp}-{slug}.md"
    path = VAULT_ROOT / '.meta' / 'syntheses' / 'queries' / filename

    lines = [
        '---',
        f"source: ask.py",
        f"query: {query}",
        f"type: query-result",
        f"mode: {'check' if is_check else 'search'}",
        f"generated_at: {now.isoformat()}",
        f"model: zhipu-embedding-3",
        '---',
        '',
        f'# Query: {query}',
        '',
        f'> 模式: {"查重" if is_check else "语义检索"} | 命中 {len(results)} 条',
        '',
        '## 结果',
        '',
    ]

    for i, (p, sim) in enumerate(results, 1):
        summary = fetch_summary(p)
        lines.append(f"{i}. **[{sim:.3f}]** `{p}`")
        if summary:
            lines.append(f"   > {summary}")
        lines.append('')

    lines.extend([
        '---',
        '',
        '## 关联思考',
        '',
        '> 在此追加你的分析、后续行动或新发现...',
        '',
    ])

    ensure_parent(path)
    path.write_text('\n'.join(lines), encoding='utf-8', newline='')
    print(f"\n💾 结果已保存: {rel_path(path)}")
    return path


def path_matches_scope(path: str, scope: str) -> bool:
    if scope == 'notes':
        return not path.startswith('.meta/memory/')
    if scope == 'memory':
        return path.startswith('.meta/memory/')
    if scope == 'all':
        return True
    raise ValueError(f"unknown scope: {scope}")


# 文件日期清单缓存（惰性加载）
_file_dates: dict | None = None


def _load_file_dates() -> dict:
    """加载 .meta/file-dates.json（主机维护时生成，OneDrive 同步到从机）"""
    global _file_dates
    if _file_dates is not None:
        return _file_dates
    dates_path = VAULT_ROOT / '.meta' / 'file-dates.json'
    if dates_path.exists():
        try:
            _file_dates = json.loads(dates_path.read_text(encoding='utf-8'))
            return _file_dates
        except Exception:
            pass
    _file_dates = {}
    return _file_dates


def get_file_age_years(path: str) -> float | None:
    """返回文件最后有意义修改距今的年数。三级回退：
    ① .meta/file-dates.json（主机维护生成，从机可用）
    ② git log（主机上直接可用）
    ③ 文件系统 mtime（最后兜底）"""
    # ① 文件日期清单（主机/从机通用）
    dates = _load_file_dates()
    if path in dates:
        return (datetime.now().timestamp() - dates[path]) / SECONDS_PER_YEAR

    # ② git log
    try:
        result = subprocess.run(
            ['git', 'log', '-1', '--format=%at', '--', path],
            capture_output=True, text=True, cwd=str(VAULT_ROOT),
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            last_ts = float(result.stdout.strip())
            return (datetime.now().timestamp() - last_ts) / SECONDS_PER_YEAR
    except Exception:
        pass

    # ③ 文件系统 mtime
    full_path = VAULT_ROOT / path
    if full_path.exists():
        return (datetime.now().timestamp() - full_path.stat().st_mtime) / SECONDS_PER_YEAR
    return None


def apply_time_decay(path: str, sim: float, decay_params: tuple | None = None) -> float:
    """基于内容年龄的分档时间衰减。decay_params=(<1年乘数, 1-2年乘数, ≥2年乘数)，默认 (1.0, 0.8, 0.5)。"""
    if decay_params is None:
        decay_params = (1.0, 0.8, 0.5)
    age_years = get_file_age_years(path)
    if age_years is None:
        return sim
    if age_years < 1.0:
        return sim * decay_params[0]
    elif age_years < 2.0:
        return sim * decay_params[1]
    else:
        return sim * decay_params[2]


def semantic_search(query: str, top_k=5, scope='notes', decay_params=None):
    if not DB_PATH.exists():
        print("⚠️  embeddings.sqlite 不存在，请先运行维护。")
        return []

    db = sqlite3.connect(str(DB_PATH))
    # 增长预警：SQLite COUNT(*) 无 WHERE 时使用 B-tree 根页 nRec，O(1)
    row_count = db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    if row_count > EMBEDDINGS_WARN_THRESHOLD:
        print(
            f"⚠️  embeddings 表已 {row_count} 行（阈值 {EMBEDDINGS_WARN_THRESHOLD}），"
            f"全表余弦扫描可能变慢。考虑定期清理旧块或引入 ANN 索引。",
            file=sys.stderr,
        )
    client = ZhipuEmbedClient()
    q_vec = np.array(client.embed_batch([query])[0])
    q_norm = np.linalg.norm(q_vec)

    best = {}
    for row in db.execute("SELECT path, embedding FROM embeddings"):
        path, blob = row
        if not path_matches_scope(path, scope):
            continue
        v = np.array(blob_to_embedding(blob))
        sim = float(np.dot(q_vec, v) / (q_norm * np.linalg.norm(v) + 1e-10))
        if path not in best or sim > best[path]:
            best[path] = sim
    db.close()

    # 应用时间衰减后重新排序
    return sorted(
        [(p, apply_time_decay(p, s, decay_params)) for p, s in best.items()],
        key=lambda x: -x[1]
    )[:top_k]


def print_results(results):
    if not results:
        print("\n未找到相关笔记。")
        return
    print(f"\n命中 {len(results)} 条（相似度降序）:\n")
    for i, (path, sim) in enumerate(results, 1):
        summary = ""
        sp = meta_mirror(VAULT_ROOT / path, 'summaries')
        if sp.exists():
            try:
                text = sp.read_text(encoding='utf-8')
                parts = text.split('---', 2)
                summary = parts[2].strip()[:160] if len(parts) >= 3 else ""
            except: pass
        print(f"{i}. [{sim:.3f}]  {path}")
        if summary:
            print(f"     {summary}")
        print()


def find_orphans():
    referenced = set()
    # 扫原文 wikilinks + markdown 链接
    for md in scan_notes():
        try:
            content = md.read_text(encoding='utf-8')
        except:
            continue

        # Wikilinks
        for target in find_all_wikilinks(content):
            t = target.strip()
            if '/' in t:
                referenced.add(Path(t).stem)
            else:
                referenced.add(t)

        # Markdown 链接
        for display, target in find_all_mdlinks(content):
            t = unquote(target).split('#')[0].strip()
            if '/' in t:
                referenced.add(Path(t).stem)
            else:
                stem = t[:-3] if t.endswith('.md') else t
                referenced.add(stem)

    # 扫 .meta/links/（Agent 生成的 [[wikilink]] 格式链接，来源含 [[]] 和 []() 两种语法）
    links_dir = VAULT_ROOT / '.meta' / 'links'
    if links_dir.exists():
        for md in links_dir.rglob('*.md'):
            try:
                content = md.read_text(encoding='utf-8')
            except:
                continue
            for target in find_all_wikilinks(content):
                t = target.strip()
                if '/' in t:
                    referenced.add(Path(t).stem)
                else:
                    referenced.add(t)

    orphans = []
    for md in scan_notes():
        if md.stem not in referenced:
            orphans.append(rel_path(md))

    print(f"\n孤儿笔记（未被任何 wikilink 或 markdown 链接引用）: {len(orphans)} 篇")
    for p in orphans[:30]:
        print(f"  - {p}")
    if len(orphans) > 30:
        print(f"  ... 以及另外 {len(orphans) - 30} 篇")


def _iter_doc_targets():
    docs_dir = VAULT_ROOT / 'docs'
    if not docs_dir.exists():
        return
    for md in docs_dir.rglob('*.md'):
        yield md


def _build_backlink_indexes():
    basename_index = {}
    aliases_index = {}
    all_paths = set()

    for md in list(scan_notes()) + list(_iter_doc_targets() or []):
        rel = rel_path(md)
        all_paths.add(rel)
        basename_index.setdefault(md.stem, []).append(rel)
        try:
            content = md.read_text(encoding='utf-8')
        except Exception:
            content = ''
        for alias in parse_aliases(content):
            if alias and alias not in aliases_index:
                aliases_index[alias] = rel

    return basename_index, aliases_index, all_paths


def _resolve_backlink_target(target: str, basename_index: dict, aliases_index: dict, all_paths: set) -> str:
    t = target.strip().replace('\\', '/')
    if t in all_paths:
        return t
    if not t.endswith('.md') and f'{t}.md' in all_paths:
        return f'{t}.md'
    resolved, _ = resolve_wikilink(t, '', basename_index, aliases_index, all_paths)
    return resolved or t


def _graph_backlinks(target: str) -> list[Backlink]:
    graph_path = VAULT_ROOT / '.meta' / 'graph.json'
    if not graph_path.exists():
        return []
    try:
        graph = json.loads(graph_path.read_text(encoding='utf-8'))
    except Exception:
        return []

    return sorted(
        {
            Backlink(
                source=edge.get('source', ''),
                target=edge.get('target', ''),
                link_type=edge.get('type', ''),
                method='graph',
            )
            for edge in graph.get('edges', [])
            if edge.get('target') == target and edge.get('source')
        },
        key=lambda b: (b.source, b.link_type),
    )


def _fallback_backlinks(target: str, basename_index: dict, aliases_index: dict, all_paths: set) -> list[Backlink]:
    backlinks = []
    seen = set()

    for md in scan_notes():
        source = rel_path(md)
        try:
            content = md.read_text(encoding='utf-8')
        except Exception:
            continue

        for line_no, line in iter_markdown_content_lines(content):
            for match in WIKILINK_RE.finditer(line):
                raw = match.group(1).strip()
                resolved, _ = resolve_wikilink(raw, source, basename_index, aliases_index, all_paths)
                if resolved == target:
                    key = (source, line_no, 'user-wikilink', raw)
                    if key not in seen:
                        seen.add(key)
                        backlinks.append(Backlink(source, target, 'user-wikilink', 'fallback', line_no, raw))

            for _, raw in find_all_mdlinks(line):
                resolved, _ = resolve_mdlink(raw, source, basename_index, aliases_index, all_paths)
                if resolved == target:
                    key = (source, line_no, 'user-mdlink', raw)
                    if key not in seen:
                        seen.add(key)
                        backlinks.append(Backlink(source, target, 'user-mdlink', 'fallback', line_no, raw))

    return sorted(backlinks, key=lambda b: (b.source, b.line_no or 0, b.link_type))


def find_backlinks(target: str) -> list[Backlink]:
    basename_index, aliases_index, all_paths = _build_backlink_indexes()
    resolved_target = _resolve_backlink_target(target, basename_index, aliases_index, all_paths)

    graph_results = _graph_backlinks(resolved_target)
    if graph_results:
        return graph_results

    return _fallback_backlinks(resolved_target, basename_index, aliases_index, all_paths)


def print_backlinks(target: str):
    backlinks = find_backlinks(target)
    if not backlinks:
        print(f"\n未找到反向链接: {target}")
        return

    resolved = backlinks[0].target
    print(f"\n反向链接: {resolved}（{len(backlinks)} 条）\n")
    for b in backlinks:
        line = f":L{b.line_no}" if b.line_no else ""
        raw = f" · [[{b.raw}]]" if b.raw and b.link_type == 'user-wikilink' else (f" · {b.raw}" if b.raw else "")
        print(f"- `{b.source}{line}` · {b.link_type} · {b.method}{raw}")


def _format_results_for_prompt(results, max_items=3):
    """Format top results for LLM prompt context, using summaries"""
    lines = []
    for i, (path, sim) in enumerate(results[:max_items]):
        summary = fetch_summary(path, max_len=500)
        text = summary if summary else "[摘要不可用]"
        lines.append(f"{i+1}. [{path}] (sim={sim:.3f}): {text}")
    return '\n'.join(lines)


def deep_search(query, iterations=3, breadth=5, scope='notes', decay_params=None, search_fn=None):
    """多跳迭代检索：固定三阶段管道（+可选第四轮实体扩展）

    去重策略：当前为 path 级去重（chunk_index 恒为 0），因 semantic_search() 返回时已按路径聚合最高相似度。
    若未来切换为 chunk 级检索，需改为 (path, chunk_index) 键。
    """
    from common import DeepSeekClient

    if search_fn is None:
        search_fn = semantic_search

    if not (2 <= iterations <= 4):
        print(f"错误：--deep-iterations 必须在 [2, 4] 范围内，收到 {iterations}", file=sys.stderr)
        sys.exit(2)

    all_results = {}  # (path, chunk_index) -> max_sim
    round_outputs = []

    # Round 1: standard search
    r1 = search_fn(query, top_k=breadth, scope=scope, decay_params=decay_params)
    for path, sim in r1:
        key = (path, 0)
        if key not in all_results or sim > all_results[key]:
            all_results[key] = sim
    round_outputs.append(('Round 1 · 初始检索', r1, []))

    if iterations >= 2:
        # Round 2: entity extraction + sub-queries
        try:
            client = DeepSeekClient()
            accumulated = _format_results_for_prompt(r1)
            extracted = client.extract_entities(query, accumulated, round_num=2)
            if 'error' in extracted:
                # Degraded: synonym fallback
                synonyms = client.generate_synonyms(query, n=2)
                r2_merged = []
                for syn in synonyms:
                    sub_results = search_fn(syn, top_k=breadth, scope=scope, decay_params=decay_params)
                    r2_merged.extend(sub_results)
                degraded = True
                reason = extracted.get('error', 'unknown')
            else:
                sub_queries = extracted.get('sub_queries', [])
                r2_merged = []
                for sq in sub_queries:
                    sub_results = search_fn(sq, top_k=breadth, scope=scope, decay_params=decay_params)
                    r2_merged.extend(sub_results)
                degraded = False
                reason = None
        except Exception as e:
            # Degraded: try synonym fallback (may also fail if API is down)
            r2_merged = []
            degraded = True
            reason = str(e)
            try:
                synonyms = client.generate_synonyms(query, n=2)
                for syn in synonyms:
                    sub_results = search_fn(syn, top_k=breadth, scope=scope, decay_params=decay_params)
                    r2_merged.extend(sub_results)
            except Exception:
                # Synonym fallback also failed — round produces no results
                pass

        # Dedup and merge — strict: only add truly new keys to per-round output
        new_r2 = []
        for path, sim in r2_merged:
            key = (path, 0)
            if key not in all_results:
                all_results[key] = sim
                new_r2.append((path, sim))
            elif sim > all_results[key]:
                all_results[key] = sim
        label = f'Round 2 · 实体扩展{" [DEGRADED: " + reason + "]" if degraded else ""}'
        round_outputs.append((label, new_r2, r1 if degraded else []))

    if iterations >= 3:
        # Round 3: contradiction/gap detection
        all_so_far = sorted(all_results.items(), key=lambda x: -x[1])[:breadth]
        try:
            client = DeepSeekClient()
            accumulated = _format_results_for_prompt([(p, s) for (p, _), s in all_so_far])
            extracted = client.extract_entities(query, accumulated, round_num=3)
            if 'error' in extracted:
                r3_results = []
                degraded = True
                reason = extracted.get('error', 'unknown')
            else:
                sub_queries = extracted.get('sub_queries', [])
                r3_results = []
                for sq in sub_queries:
                    sub_results = search_fn(sq, top_k=breadth, scope=scope, decay_params=decay_params)
                    r3_results.extend(sub_results)
                degraded = False
                reason = None
        except Exception as e:
            r3_results = []
            degraded = True
            reason = str(e)

        new_r3 = []
        for path, sim in r3_results:
            key = (path, 0)
            if key not in all_results:
                all_results[key] = sim
                new_r3.append((path, sim))
            elif sim > all_results[key]:
                all_results[key] = sim
        label = f'Round 3 · 矛盾验证{" [DEGRADED: " + reason + "]" if degraded else ""}'
        round_outputs.append((label, new_r3, []))

    if iterations >= 4:
        # Round 4: additional entity expansion (reuse Round 2 pattern)
        all_so_far = sorted(all_results.items(), key=lambda x: -x[1])[:breadth]
        try:
            client = DeepSeekClient()
            accumulated = _format_results_for_prompt([(p, s) for (p, _), s in all_so_far])
            extracted = client.extract_entities(query, accumulated, round_num=2)  # reuse R2 pattern
            if 'error' in extracted:
                r4_results = []
                degraded = True
                reason = extracted.get('error', 'unknown')
            else:
                sub_queries = extracted.get('sub_queries', [])
                r4_results = []
                for sq in sub_queries:
                    sub_results = search_fn(sq, top_k=breadth, scope=scope, decay_params=decay_params)
                    r4_results.extend(sub_results)
                degraded = False
                reason = None
        except Exception as e:
            r4_results = []
            degraded = True
            reason = str(e)

        new_r4 = []
        for path, sim in r4_results:
            key = (path, 0)
            if key not in all_results:
                all_results[key] = sim
                new_r4.append((path, sim))
            elif sim > all_results[key]:
                all_results[key] = sim
        label = f'Round 4 · 实体扩展 II{" [DEGRADED: " + reason + "]" if degraded else ""}'
        round_outputs.append((label, new_r4, []))

    return round_outputs


def hybrid_search(query, top_k=5, scope='notes', decay_params=None, weights=(0.6, 0.4)):
    """BM25 + Dense 融合检索"""
    w_dense, w_bm25 = weights

    # Dense search
    dense_results = semantic_search(query, top_k=top_k*2, scope=scope, decay_params=decay_params)

    # BM25 search
    try:
        bm25_raw = bm25_search(query, top_k=top_k*2)
    except Exception as e:
        print(f"⚠️  BM25 检索失败，退化为纯 Dense: {e}", file=sys.stderr)
        return dense_results

    # Build lookup: path -> {dense_score, bm25_score}
    combined = {}
    for path, sim in dense_results:
        combined[path] = {'dense': sim, 'bm25': 0.0}
    for path, chunk_idx, score in bm25_raw:
        if path not in combined:
            combined[path] = {'dense': 0.0, 'bm25': 0.0}
        combined[path]['bm25'] = max(combined[path]['bm25'], score)

    # Normalize scores (min-max)
    dense_vals = [v['dense'] for v in combined.values()]
    bm25_vals = [v['bm25'] for v in combined.values()]

    d_min, d_max = min(dense_vals), max(dense_vals)
    b_min, b_max = min(bm25_vals), max(bm25_vals)

    d_range = d_max - d_min if d_max > d_min else 1.0
    b_range = b_max - b_min if b_max > b_min else 1.0

    # Fuse scores
    fused = {}
    for path, scores in combined.items():
        d_norm = (scores['dense'] - d_min) / d_range
        b_norm = (scores['bm25'] - b_min) / b_range
        fused[path] = w_dense * d_norm + w_bm25 * b_norm

    return sorted(fused.items(), key=lambda x: -x[1])[:top_k]


def rerank_results(query, results, top_m=5):
    """对检索结果做 LLM-as-Judge 精排"""
    import sqlite3

    if not results:
        return results

    # Get chunk texts from embeddings.sqlite
    db = sqlite3.connect(str(DB_PATH))
    candidates = []
    for path, sim in results[:20]:  # Take top-20 for reranking
        rows = db.execute(
            "SELECT chunk_index, chunk_text FROM embeddings WHERE path = ? ORDER BY chunk_index",
            (path,)
        ).fetchall()
        for chunk_idx, text in rows:
            candidates.append((path, chunk_idx, text))
    db.close()

    if not candidates:
        return results[:top_m]

    reranked = rerank_with_llm(query, candidates, top_m=top_m)

    # Map back to (path, score) format
    path_scores = {}
    for path, chunk_idx, score in reranked:
        if path not in path_scores or score > path_scores[path]:
            path_scores[path] = score

    return sorted(path_scores.items(), key=lambda x: -x[1])[:top_m]


def main():
    log_script_run()
    parser = argparse.ArgumentParser(description='语义查询 / 查重 / 孤儿检测')
    parser.add_argument('--check', action='store_true', help='写前查重模式（返回 top-10）')
    parser.add_argument('--orphans', action='store_true', help='列出孤儿笔记')
    parser.add_argument('--backlinks', metavar='TARGET', help='查询指向目标笔记的反向链接')
    parser.add_argument('--save', action='store_true', help='将结果保存到 .meta/syntheses/queries/')
    parser.add_argument('--scope', choices=('notes', 'memory', 'all'), default='notes',
                        help='语义检索范围：notes=用户笔记（默认），memory=.meta/memory/，all=二者')
    parser.add_argument('--decay', type=str, default=None,
                        help='时间衰减参数：逗号分隔三个浮点数，对应 <1年/1-2年/≥2年 乘数（默认 1.0,0.8,0.5）。需单调递减 (a>=b>=c>=0)。例：--decay "1.0,1.0,1.0" 关闭衰减')
    parser.add_argument('--top-k', type=int, default=None, dest='top_k',
                        help='最大返回条数（1-50，默认：普通查询 5，查重 10）。超出范围自动截断。例：--top-k 20')
    parser.add_argument('--neighbors', type=str, default=None, metavar='TARGET',
                        help='查询目标笔记的图邻域（沿 graph.json 边做 N 跳探索）')
    parser.add_argument('--max-hops', type=int, default=2,
                        help='--neighbors 的最大跳数（默认 2）')
    parser.add_argument('--path', nargs=2, metavar=('SOURCE', 'TARGET'),
                        help='查找两篇笔记间的最短路径')
    parser.add_argument('--deep', action='store_true', help='多跳迭代深度检索')
    parser.add_argument('--deep-iterations', type=int, default=3, help='深度检索轮数（2-4，默认 3）')
    parser.add_argument('--deep-breadth', type=int, default=5, help='每轮保留 top-k（默认 5）')
    parser.add_argument('--bm25', action='store_true', help='使用 BM25 稀疏检索（替代 Dense）')
    parser.add_argument('--hybrid', action='store_true', help='BM25 + Dense 混合检索')
    parser.add_argument('--hybrid-weights', type=str, default='0.6,0.4',
                        help='Hybrid 融合权重，逗号分隔两个浮点数（默认 0.6,0.4）')
    parser.add_argument('--rerank', action='store_true', help='LLM-as-Judge 精排（DeepSeek chat API）')
    parser.add_argument('--rerank-top-m', type=int, default=None,
                        help='精排后返回 top-m（默认 min(k, 5)，k 为 --top-k 或 --deep-breadth）')
    parser.add_argument('query', nargs='?', help='查询字符串')
    args = parser.parse_args()

    if args.neighbors:
        if get_neighbors is None:
            print("错误：--neighbors 需要 build_graph.py（Phase 1b 脚本）。当前安装未包含，请升级到 Phase 1b 或检查 .meta/scripts/。", file=sys.stderr)
            sys.exit(1)
        result = get_neighbors(args.neighbors, max_hops=args.max_hops)
        if isinstance(result, dict) and "error" in result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(1)
        is_isolated = len(result) == 0
        output = {"neighbors": result, "is_isolated": is_isolated}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    if args.path:
        if find_shortest_path is None:
            print("错误：--path 需要 build_graph.py（Phase 1b 脚本）。当前安装未包含，请升级到 Phase 1b 或检查 .meta/scripts/。", file=sys.stderr)
            sys.exit(1)
        source, target = args.path
        result = find_shortest_path(source, target)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if "error" in result:
            sys.exit(1)
        return

    if args.orphans:
        find_orphans()
        return

    if args.backlinks:
        print_backlinks(args.backlinks)
        return

    if not args.query:
        parser.print_help()
        return

    query = args.query

    # 解析 --decay 参数（--deep 和普通检索共享）
    decay_params = None
    if args.decay is not None:
        try:
            parts = [float(x.strip()) for x in args.decay.split(",")]
            if len(parts) != 3:
                print(f"⚠️  --decay 需要恰好 3 个逗号分隔值，收到 {len(parts)} 个。回退默认值 (1.0, 0.8, 0.5)。", file=sys.stderr)
            elif not (parts[0] >= parts[1] >= parts[2] >= 0):
                print(f"⚠️  --decay 参数需单调递减 (a>=b>=c>=0)，收到 {parts}。回退默认值 (1.0, 0.8, 0.5)。", file=sys.stderr)
            else:
                decay_params = tuple(parts)
        except ValueError:
            print(f"⚠️  --decay 解析失败 '{args.decay}'，需为三个逗号分隔的数值。回退默认值 (1.0, 0.8, 0.5)。", file=sys.stderr)

    # Determine retrieval mode
    use_hybrid = args.hybrid
    use_bm25 = args.bm25

    if use_hybrid and use_bm25:
        print("错误：--hybrid 与 --bm25 互斥", file=sys.stderr)
        sys.exit(2)

    if args.deep and use_bm25:
        print("错误：--deep 不与 --bm25 组合（BM25 不支持语义查询扩展）。请用 --hybrid --deep", file=sys.stderr)
        sys.exit(2)

    # Parse hybrid weights
    hybrid_weights = (0.6, 0.4)
    if args.hybrid_weights:
        try:
            parts = [float(x.strip()) for x in args.hybrid_weights.split(',')]
            if len(parts) != 2:
                print(f"错误：--hybrid-weights 需要恰好 2 个值", file=sys.stderr)
                sys.exit(2)
            hybrid_weights = tuple(parts)
        except ValueError:
            print(f"错误：--hybrid-weights 格式无效", file=sys.stderr)
            sys.exit(2)

    if args.deep:
        from functools import partial
        if use_hybrid:
            search_fn = partial(hybrid_search, weights=hybrid_weights)
        else:
            search_fn = semantic_search
        round_outputs = deep_search(
            query, iterations=args.deep_iterations,
            breadth=args.deep_breadth, scope=args.scope,
            decay_params=decay_params, search_fn=search_fn
        )
        for label, results, _ in round_outputs:
            if args.rerank:
                # 精排 top-m: min(breadth, 5) 若未显式指定
                rerank_m = args.rerank_top_m if args.rerank_top_m is not None else min(args.deep_breadth, 5)
                results = rerank_results(query, results, top_m=rerank_m)
            print(f"\n{'='*60}")
            print(f"  {label}（{len(results)} 条）")
            print(f"{'='*60}")
            print_results(results)
        if args.save and round_outputs:
            all_items = []
            for _, results, _ in round_outputs:
                all_items.extend(results)
            save_query_results(query, all_items, is_check=False)
        return

    # --top-k 覆盖模式默认值；未提供时保持现有行为（普通 5，查重 10）
    if args.top_k is not None:
        if args.top_k < 1:
            print(f"⚠️  --top-k 最小值为 1，收到 {args.top_k}，已截断为 1。", file=sys.stderr)
            top_k = 1
        elif args.top_k > 50:
            print(f"⚠️  --top-k 最大值为 50，收到 {args.top_k}，已截断为 50。", file=sys.stderr)
            top_k = 50
        else:
            top_k = args.top_k
    else:
        top_k = 10 if args.check else 5
    mode_label = '查重' if args.check else '查询'
    if use_hybrid:
        mode_label = 'Hybrid' if not args.check else '查重(Hybrid)'
    elif use_bm25:
        mode_label = 'BM25' if not args.check else '查重(BM25)'

    print(f"{mode_label}: 「{query}」")
    if args.check:
        print("以下已有笔记可能与你打算写的内容重合:")
    if use_hybrid:
        results = hybrid_search(query, top_k=top_k, scope=args.scope, decay_params=decay_params, weights=hybrid_weights)
    elif use_bm25:
        try:
            results = [(path, score) for path, chunk_idx, score in bm25_search(query, top_k=top_k)]
        except Exception as e:
            print(f"⚠️  BM25 检索失败: {e}", file=sys.stderr)
            results = []
    else:
        results = semantic_search(query, top_k=top_k, scope=args.scope, decay_params=decay_params)
    if args.rerank:
        rerank_m = args.rerank_top_m if args.rerank_top_m is not None else min(top_k, 5)
        results = rerank_results(query, results, top_m=rerank_m)
    print_results(results)

    if args.save and results:
        save_query_results(query, results, is_check=args.check)


if __name__ == "__main__":
    main()
