#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skill-sync-check.py — 检测 init-kb-harness bundle 脚本与本库 .meta/scripts/ 的 drift。

用法:
    python skill-sync-check.py
    python skill-sync-check.py --vault /path/to/source/vault
    HARNESS_SOURCE_VAULT=/other/vault python skill-sync-check.py

退出码:
    0 = 无 drift
    1 = 有 drift（供 CI / pre-commit hook 用）

比对策略：SHA256 哈希逐脚本比对。
- MATCH：两边都存在且哈希一致
- DRIFT：两边都存在但哈希不一致（重点告警）
- MISSING_IN_SOURCE：bundle 有，source 也有，但 source 文件读不到（异常）
- BUNDLED_ONLY：bundle 独有（maintain-lite.py / skill-sync-check.py 等）——预期，不算 drift
- MISSING_IN_BUNDLE：source 有但 bundle 没有（仅作信息，不算 drift）
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path

# Windows UTF-8 输出保障（本脚本独立、不 import common，需自备；与 common.py 一致）
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# bundle-only 文件（无 source 对应，预期存在，不算 drift）
BUNDLED_ONLY = {
    'maintain-lite.py',
    'skill-sync-check.py',
}

# 无默认源仓库——drift 检测是 bundle 维护者的动作，须显式指定源
# （--vault 或 HARNESS_SOURCE_VAULT env），避免假定/泄露他人本机路径。
DEFAULT_SOURCE_VAULT = None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def resolve_source_vault(cli_arg):
    """源仓库须显式指定：--vault 或 HARNESS_SOURCE_VAULT env。无则返回 None。"""
    if cli_arg:
        return Path(cli_arg).resolve()
    env_val = os.environ.get('HARNESS_SOURCE_VAULT', '').strip()
    if env_val:
        return Path(env_val).resolve()
    return None


def main():
    parser = argparse.ArgumentParser(description='init-kb-harness bundle 与本库 .meta/scripts 的 drift 检测')
    parser.add_argument('--vault', help='source vault 根路径（或 HARNESS_SOURCE_VAULT env；无默认，须显式指定）')
    args = parser.parse_args()

    bundle_dir = Path(__file__).resolve().parent
    source_vault = resolve_source_vault(args.vault)
    if source_vault is None:
        print("⚠️  未指定源仓库。drift 检测须显式指定源：")
        print("    python skill-sync-check.py --vault /path/to/source/vault")
        print("    或 HARNESS_SOURCE_VAULT=/path/to/vault python skill-sync-check.py")
        return 2
    source_dir = source_vault / '.meta' / 'scripts'

    print(f"bundle  目录: {bundle_dir}")
    print(f"source  目录: {source_dir}")
    print()

    if not source_dir.exists():
        print(f"⚠️  source 目录不存在或不可访问：{source_dir}")
        print("    无法进行 drift 检测。")
        return 2

    # 收集 bundle .py 文件（排除 __pycache__）
    bundle_files = {p.name: p for p in bundle_dir.glob('*.py') if p.is_file()}

    # 收集 source .py 文件（仅顶层的 .py，不递归；排除 tests）
    source_files = {}
    for p in source_dir.glob('*.py'):
        if p.is_file():
            source_files[p.name] = p

    drift_count = 0
    match_count = 0
    bundled_only_count = 0
    missing_in_bundle = []

    print(f"{'脚本':<32} {'状态':<20}")
    print('-' * 54)

    # 1. bundle 中每个 .py
    for name in sorted(bundle_files):
        bundle_path = bundle_files[name]
        try:
            bundle_hash = sha256(bundle_path)
        except OSError as e:
            print(f"  ⚠️  {name:<30} READ_FAIL ({e})")
            drift_count += 1
            continue

        if name in BUNDLED_ONLY:
            print(f"  {name:<30} BUNDLED_ONLY")
            bundled_only_count += 1
            continue

        if name not in source_files:
            print(f"  {name:<30} MISSING_IN_SOURCE  (bundle 哈希 {bundle_hash[:8]})")
            drift_count += 1
            continue

        source_path = source_files[name]
        try:
            source_hash = sha256(source_path)
        except OSError:
            print(f"  {name:<30} MISSING_IN_SOURCE")
            drift_count += 1
            continue

        if bundle_hash == source_hash:
            print(f"  {name:<30} MATCH")
            match_count += 1
        else:
            print(f"  {name:<30} DRIFT")
            print(f"      bundle:  {bundle_hash}  ({bundle_path.stat().st_size} bytes)")
            print(f"      source:  {source_hash}  ({source_path.stat().st_size} bytes)")
            drift_count += 1

    # 2. source 中存在但 bundle 缺失（信息性）
    for name in sorted(source_files):
        if name in bundle_files:
            continue
        # 跳过本库独有脚本（dream / synthesis_index / bm25_index / gc / knowledge_map /
        # semantic_lint / maintain / search_sessions / synthesize / commit_meta / check_plan_status /
        # check_supersession / review_queue / create_text_pptx / html_to_pptx / find_orphan_images /
        # archive_orphan_images 等）——这些不在 Phase 1 bundle 内，预期缺失
        missing_in_bundle.append(name)

    print()
    print('── 摘要 ──')
    print(f"  MATCH          : {match_count}")
    print(f"  DRIFT          : {drift_count}")
    print(f"  BUNDLED_ONLY   : {bundled_only_count}")
    print(f"  SOURCE-ONLY    : {len(missing_in_bundle)}（非 drift；这些是 Phase 2/3 source 独有脚本）")
    if missing_in_bundle and len(missing_in_bundle) <= 30:
        for n in missing_in_bundle:
            print(f"      - {n}")

    print()
    if drift_count > 0:
        print(f"✗ 检测到 {drift_count} 项 drift —— bundle 与 source 不同步")
        print("  维护建议：从 source 重新拷贝 DRIFT 项到 bundle（必要时重新做 de-coupling）")
        return 1
    else:
        print("✓ 无 drift —— bundle 与 source 同步")
        return 0


if __name__ == "__main__":
    sys.exit(main())
