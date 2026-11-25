#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_links.py — 生成 .meta/links/ 语义关联 sidecar（纯余弦，无 LLM）

属语义层：只要有 embeddings.sqlite 就能跑，与 HARNESS_MODE / LLM 无关。
build_graph 从这里读 semantic 边；ask.py --orphans 不再把它当"用户引用"。

- 新鲜度：content_hash + 生成模型 + 两个阈值，任一变化都会重建该篇
- 清理：源文件消失、或笔记已无向量的 sidecar 会被删除
- 相似度：一次性矩阵乘（numpy），而不是 Python 双层循环

用法:
    python .meta/scripts/build_links.py           # 增量
    python .meta/scripts/build_links.py --full    # 全量重写
"""

import sys
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    VAULT_ROOT, scan_notes, scan_office_extracts, rel_path,
    meta_mirror, ensure_parent, find_all_wikilinks, find_all_mdlinks,
    ENV, threshold_defaults, log_script_run, ScanCache,
)
from embed import blob_to_embedding, read_meta

DB_PATH = VAULT_ROOT / '.meta' / 'embeddings.sqlite'
LINKS_DIR = VAULT_ROOT / '.meta' / 'links'
TOP_K = 8
# 关联阈值：这里的线写 links；build_graph 再用 SEMANTIC_THRESHOLD 收边。
# 换嵌入模型后可用 .env 校准（生效条件：本脚本会因 model/阈值变化自动重建）。
_td = threshold_defaults()
RELATED_THRESHOLD = float(ENV.get('LINKS_SIM_THRESHOLD') or _td['links'])
HIGH_TIER = float(ENV.get('LINKS_HIGH_THRESHOLD') or _td['links_high'])


def _stamp_params():
    return f'{RELATED_THRESHOLD:.2f}|{HIGH_TIER:.2f}'


def _read_stamp(path: Path) -> dict:
    """读 sidecar frontmatter 的溯源字段；失败返回空 dict。"""
    out = {}
    try:
        for line in path.read_text(encoding='utf-8').splitlines()[:12]:
            if ':' in line:
                k, v = line.split(':', 1)
                out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


def already_up_to_date(rel: str, h: str, model: str) -> bool:
    p = meta_mirror(VAULT_ROOT / rel, 'links')
    if not p.exists():
        return False
    fm = _read_stamp(p)
    return (fm.get('content_hash') == h
            and fm.get('model') == model
            and fm.get('thresholds') == _stamp_params())


def extract_link_targets(content: str) -> list:
    """提取 wikilink 与 markdown 链接的 target；输出统一用 [[target]]。"""
    targets = set()
    for t in find_all_wikilinks(content):
        targets.add(t.strip())
    for _display, t in find_all_mdlinks(content):
        targets.add(unquote(t).split('#')[0].strip())
    return sorted(t for t in targets if t)


def _link_target(rel: str) -> str:
    """vault 相对路径（去 .md）；'#' 是 wikilink 锚点分隔符，转成 %23 由解析侧还原。"""
    return Path(rel).with_suffix('').as_posix().replace('#', '%23')


def write_links(rel, h, related, wikilinks, model):
    p = meta_mirror(VAULT_ROOT / rel, 'links')
    ensure_parent(p)
    lines = [
        "---",
        f"source: {rel}",
        f"content_hash: {h}",
        f"generated_at: {datetime.now().isoformat()}",
        f"model: {model}",
        f"thresholds: {_stamp_params()}",
        "---",
        "",
        "<!-- Obsidian 语法 [[]]：改名时自动维护。本文件由脚本生成，请勿手动编辑。 -->",
        "",
    ]

    high = [(r, s) for r, s in related if s >= HIGH_TIER]
    mid = [(r, s) for r, s in related if RELATED_THRESHOLD <= s < HIGH_TIER]

    if high:
        lines.append(f"## 高度相关（sim ≥ {HIGH_TIER:.2f}）")
        for r, s in high:
            lines.append(f"- [[{_link_target(r)}|{Path(r).stem}]] — sim {s:.2f}")
        lines.append("")
    if mid:
        lines.append(f"## 中度相关（{RELATED_THRESHOLD:.2f}–{HIGH_TIER:.2f}）")
        for r, s in mid:
            lines.append(f"- [[{_link_target(r)}|{Path(r).stem}]] — sim {s:.2f}")
        lines.append("")
    if not high and not mid:
        lines.append("## 相关笔记")
        lines.append("- （无）")
        lines.append("")

    if wikilinks:
        lines.append("## 用户手写双链")
        for link in wikilinks:
            lines.append(f"- [[{link}]]")
        lines.append("")

    p.write_text("\n".join(lines), encoding='utf-8')


def cleanup_orphans(valid_rels: set) -> int:
    """删除源笔记已不存在、或该笔记已无向量的 sidecar。"""
    removed = 0
    if not LINKS_DIR.exists():
        return 0
    for p in LINKS_DIR.rglob('*.md'):
        src = _read_stamp(p).get('source', '')
        if src not in valid_rels:
            try:
                p.unlink()
                removed += 1
            except Exception:
                pass
    return removed


SIM_BLOCK_SIZE = 512


def compute_top_k_neighbors(
    mat: np.ndarray,
    query_indices: list,
    paths: list,
    top_k: int = TOP_K,
    threshold: float = RELATED_THRESHOLD,
    block_size: int = SIM_BLOCK_SIZE,
) -> list:
    """向量化分块矩阵乘 + np.argpartition 计算每个 query_index 的 top-K 语义近邻。

    mat: (N, D) L2 归一化矩阵
    query_indices: 待计算的行索引列表
    返回与 query_indices 等长的列表，每项为 [(neighbor_path, score), ...]，
    排除自身，按 score 降序，仅保留 score >= threshold，最多 top_k 条。
    """
    n = mat.shape[0]
    if not query_indices or n <= 1:
        return [[] for _ in query_indices]

    results = []
    k_sel = min(top_k, n - 1)
    mat_t = mat.T

    for start in range(0, len(query_indices), block_size):
        chunk_idx = query_indices[start:start + block_size]
        sim_block = mat[chunk_idx] @ mat_t
        for row_i, src_idx in enumerate(chunk_idx):
            row = sim_block[row_i]
            row[src_idx] = -np.inf
            if k_sel <= 0:
                results.append([])
                continue
            cand = np.argpartition(-row, k_sel - 1)[:k_sel]
            cand_scores = row[cand]
            order = np.lexsort((cand, -cand_scores))
            related = []
            for pos in order:
                j = int(cand[pos])
                if j == src_idx:
                    continue
                s = float(cand_scores[pos])
                if s >= threshold:
                    related.append((paths[j], s))
            results.append(related)
    return results


def main(full=False):
    if not DB_PATH.exists():
        print("  语义层未建立（无 embeddings.sqlite），跳过。")
        return 0

    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA busy_timeout=30000")
    try:
        meta = read_meta(db)
        if meta is None:
            print("  嵌入库缺少元数据（首跑未完成？），本轮跳过关联计算。")
            return 0
        by_path = {}
        for path, blob in db.execute("SELECT path, embedding FROM embeddings"):
            by_path.setdefault(path, []).append(np.asarray(blob_to_embedding(blob), dtype=np.float32))
    finally:
        db.close()
    if not by_path:
        print("  嵌入库为空，本轮跳过关联计算。")
        return 0

    model = f"cosine({meta['model']})"
    paths = list(by_path)
    index = {p: i for i, p in enumerate(paths)}
    mat = np.stack([np.mean(by_path[p], axis=0) for p in paths])
    mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12)

    scan_cache = ScanCache()
    notes = list(scan_notes()) + list(scan_office_extracts())
    todo = []
    for md in notes:
        rel = rel_path(md)
        if rel not in index:
            # 该笔记没有向量（被大文件闸门排除/尚未嵌入）：旧 sidecar 不能继续用
            p = meta_mirror(VAULT_ROOT / rel, 'links')
            if p.exists():
                p.unlink()
            continue
        try:
            h = scan_cache.get_content_hash(md, rel)
        except Exception as e:
            print(f"  [skip] 无法读取: {rel} ({e})")
            continue
        if not full and already_up_to_date(rel, h, model):
            continue
        try:
            content = md.read_text(encoding='utf-8')
        except Exception as e:
            print(f"  [skip] 无法读取: {rel} ({e})")
            continue
        todo.append((md, rel, h, content))
    scan_cache.save()

    if todo:
        query_indices = [index[rel] for _, rel, _, _ in todo]
        all_related = compute_top_k_neighbors(
            mat, query_indices, paths, top_k=TOP_K, threshold=RELATED_THRESHOLD
        )
        for i, ((md, rel, h, content), related) in enumerate(zip(todo, all_related), 1):
            write_links(rel, h, related, extract_link_targets(content), model)
            if i % 50 == 0 or i == len(todo):
                print(f"  [{i}/{len(todo)}] ...")

    current = {rel_path(md) for md in notes}
    removed = cleanup_orphans(current)
    print(f"  ✓ 完成 .meta/links/（{len(todo)} 篇更新，阈值 {RELATED_THRESHOLD}，"
          f"{removed} 个孤儿清理）")
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
    log_script_run()
    sys.exit(main(full='--full' in sys.argv))