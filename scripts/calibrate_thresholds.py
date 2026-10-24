#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calibrate_thresholds.py — 用当前嵌入库的相似度分布，建议 links/graph/escalation 阈值

只读：不改 .env、不写索引。把输出贴进 `.env` 即可（或据此微调）。

思路：
    · 相关代理 = 同一顶层目录内的最高相似度（links 实际取的就是 top-k）
    · 不相关代理 = 不同顶层目录的最高相似度（最容易误判的"难负例"）
    · links 下限取两者中值；高相关取相关中位数；graph 收边取相关 p25；
      escalation 取相关 p10（低于它视为低置信）

用法:
    python .meta/scripts/calibrate_thresholds.py
    python .meta/scripts/calibrate_thresholds.py --limit 4000 --json
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import VAULT_ROOT, threshold_defaults

DB_PATH = VAULT_ROOT / '.meta' / 'embeddings.sqlite'


def load_vectors(limit: int):
    con = sqlite3.connect(str(DB_PATH))
    try:
        by_path = {}
        for path, blob in con.execute("SELECT path, embedding FROM embeddings"):
            if path.startswith('.meta/') and not path.startswith('.meta/office-extracts/'):
                continue
            by_path.setdefault(path, []).append(
                np.frombuffer(blob, dtype='<f4').astype(np.float32))
    finally:
        con.close()
    paths = sorted(by_path)[:limit]
    if not paths:
        return [], None
    mat = np.stack([np.mean(by_path[p], axis=0) for p in paths])
    mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12)
    return paths, mat


def _top_dir(path: str) -> str:
    return path.split('/', 1)[0] if '/' in path else ''


def collect(paths, mat):
    related, unrelated = [], []
    n = len(paths)
    dirs = np.array([_top_dir(p) for p in paths])
    for start in range(0, n, 256):
        stop = min(start + 256, n)
        sims = mat[start:stop] @ mat.T          # (chunk, n)
        for i in range(stop - start):
            row = sims[i]
            same = dirs == dirs[start + i]
            same[start + i] = False             # 排除自己
            if same.any():
                related.append(float(row[same].max()))
            diff = ~same
            diff[start + i] = False
            if diff.any():
                unrelated.append(float(row[diff].max()))
    return np.array(related), np.array(unrelated)


def suggest(related, unrelated):
    d = threshold_defaults()
    defaults = {'links': d['links'], 'links_high': d['links_high'],
                'graph': d['graph'], 'escalation': d['escalation']}
    # 样本太少时不做推断：目录代理不可靠，容易给出 0.3x 这种会灌水的阈值
    if len(related) < 20 or len(unrelated) < 20:
        return defaults, {'insufficient': True,
                          'samples': {'related': int(len(related)),
                                      'unrelated': int(len(unrelated))}}
    rel = np.percentile(related, [10, 25, 50, 90])
    unrel = np.percentile(unrelated, [50, 90, 99])
    floor = (float(unrel[1]) + float(rel[0])) / 2
    links = round(min(max(floor, 0.30), 0.90), 2)
    return {
        'links': links,
        'links_high': round(max(float(rel[2]), links + 0.05), 2),
        'graph': round(max(float(rel[1]), links + 0.02), 2),
        'escalation': round(max(float(rel[0]), links - 0.05), 2),
    }, {
        'related_p10_p25_p50_p90': [round(float(x), 3) for x in rel],
        'unrelated_p50_p90_p99': [round(float(x), 3) for x in unrel],
        'samples': {'related': int(len(related)), 'unrelated': int(len(unrelated))},
    }


def main():
    parser = argparse.ArgumentParser(description='相似度阈值校准（只读）')
    parser.add_argument('--limit', type=int, default=4000, help='最多采样多少篇（默认 4000）')
    parser.add_argument('--json', action='store_true', help='输出 JSON')
    args = parser.parse_args()

    if not DB_PATH.exists():
        print("语义层未建立（.meta/embeddings.sqlite 不存在）", file=sys.stderr)
        return 1
    paths, mat = load_vectors(max(2, args.limit))
    if not paths:
        print("嵌入库为空", file=sys.stderr)
        return 1

    related, unrelated = collect(paths, mat)
    values, stats = suggest(related, unrelated)
    if args.json:
        print(json.dumps({'suggested': values, 'distribution': stats}, ensure_ascii=False, indent=2))
        return 0

    print(f"样本：{len(paths)} 篇（{stats.get('samples', {}).get('related', 0)} 个相关对，"
          f"{stats.get('samples', {}).get('unrelated', 0)} 个难负例）")
    if stats.get('insufficient'):
        print("⚠️  样本不足（<20 对），沿用当前后端默认值；语料积累后再校准。")
    if stats.get('related_p10_p25_p50_p90'):
        print(f"相关 top-1 分位 p10/p25/p50/p90: {stats['related_p10_p25_p50_p90']}")
        print(f"不同目录 top-1 分位 p50/p90/p99: {stats['unrelated_p50_p90_p99']}")
    print("\n建议写入 .env（按当前嵌入模型与语料分布）：")
    print(f"LINKS_SIM_THRESHOLD={values['links']}")
    print(f"LINKS_HIGH_THRESHOLD={values['links_high']}")
    print(f"SEMANTIC_THRESHOLD={values['graph']}")
    print(f"ESCALATION_THRESHOLD={values['escalation']}")
    print("\n说明：相关/不相关用目录结构代理，校准后建议抽查 .meta/links/ 采样确认。")
    return 0


if __name__ == '__main__':
    sys.exit(main())