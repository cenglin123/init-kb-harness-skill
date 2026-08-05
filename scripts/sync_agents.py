#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""同步 AGENTS.md 到 CLAUDE.md / GEMINI.md，并校验 MD5。
--check-gov-consistency 子命令校验治理文档三方一致性（SSOT / AGENTS 明线 / hook GOV_PATTERNS）。
"""

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "AGENTS.md"
TARGETS = [ROOT / "CLAUDE.md", ROOT / "GEMINI.md"]


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def normalize_token(token: str) -> str:
    """Normalize: strip backticks, quotes, leading ./, trailing /, glob wildcards"""
    t = token.strip()
    t = t.strip('`').strip("'").strip('"').strip()
    if t.startswith('./'):
        t = t[2:]
    t = t.lstrip('*')
    t = t.rstrip('/')
    return t


def is_dir_token(token: str) -> bool:
    """True if the original token ended with /, indicating directory"""
    return token.strip().endswith('/')


def match_tokens(ssot_token: str, candidate_tokens: set) -> bool:
    """
    Check if ssot_token has a match in candidate_tokens using segment matching.
    - Directories (end with /): segment-by-segment equality
    - Files (no trailing /): full relative path equality
    - SSOT file vs hook dir: file-under-dir prefix match (hook regex covers subpaths)
    """
    norm_ssot = normalize_token(ssot_token)
    ssot_is_dir = is_dir_token(ssot_token)
    ssot_segs = norm_ssot.replace('\\', '/').split('/')

    for ct in candidate_tokens:
        norm_ct = normalize_token(ct)
        ct_is_dir = is_dir_token(ct)
        ct_segs = norm_ct.replace('\\', '/').split('/')

        if ssot_is_dir and ct_is_dir:
            # Directory: segment-by-segment
            if ct_segs == ssot_segs:
                return True
        elif not ssot_is_dir and not ct_is_dir:
            # File: full relative path equality (not basename — would cause false
            # positive when two files share the same name in different subdirs)
            if norm_ct.replace('\\', '/') == norm_ssot.replace('\\', '/'):
                return True
        elif not ssot_is_dir and ct_is_dir:
            # SSOT file token vs hook dir token: file under directory → match
            # (hook GOV_PATTERNS uses dir-prefix regex, which catches subpaths)
            if norm_ssot.replace('\\', '/').startswith(norm_ct.replace('\\', '/') + '/'):
                return True
        # else: ssot dir vs hook file → no match

    return False


def parse_ssot(path: Path) -> list[str]:
    """Parse .meta/governed-files.txt, return list of path tokens.
    Skips: blank lines, comments (#), YAML frontmatter (--- / key: value)."""
    tokens = []
    in_frontmatter = False
    for line in path.read_text(encoding='utf-8').splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == '---':
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if stripped.startswith('#'):
            continue
        tokens.append(stripped)
    return tokens


def parse_agents_paragraph(path: Path) -> list[str]:
    """Extract backtick-enclosed paths from the 明线 paragraph in AGENTS.md."""
    content = path.read_text(encoding='utf-8')
    for line in content.splitlines():
        if '**明线规则**' in line:
            return re.findall(r'`([^`]+)`', line)
    return []


def parse_hook_gov_patterns(path: Path) -> list[str]:
    """Extract GOV_PATTERNS tokens from pre-commit hook."""
    content = path.read_text(encoding='utf-8')
    m = re.search(r'GOV_PATTERNS="([^"]+)"', content)
    if not m:
        return []
    return m.group(1).split('|')


def check_bootstrap_exception() -> bool:
    """Bootstrap: if staged include ALL of (a) .meta/governed-files.txt,
    (b) any new governance file, (c) AGENTS.md and/or .githooks/pre-commit,
    skip the consistency check."""
    result = subprocess.run(
        ['git', 'diff', '--cached', '--name-only'],
        capture_output=True, text=True, cwd=ROOT
    )
    staged = set(result.stdout.splitlines())

    has_ssot = '.meta/governed-files.txt' in staged
    has_agents_hook = 'AGENTS.md' in staged or '.githooks/pre-commit' in staged
    return has_ssot and has_agents_hook


def print_diff(side: str, missing: list, extra: list):
    """Print diff between two sides."""
    if missing:
        print(f"  [{side}] 缺少以下项目 (SSOT 有但 {side} 无):")
        for t in missing:
            print(f"    - `{t}`")
    if extra:
        print(f"  [{side}] 多出以下项目 ({side} 有但 SSOT 无):")
        for t in extra:
            print(f"    + `{t}`")


def check_gov_consistency() -> int:
    """--check-gov-consistency: verify T_agents == T_ssot and T_hook ⊇ T_ssot."""
    ssot_file = ROOT / ".meta" / "governed-files.txt"
    agents_file = ROOT / "AGENTS.md"
    hook_file = ROOT / ".githooks" / "pre-commit"

    # Bootstrap exception
    if check_bootstrap_exception():
        print("✓ bootstrap exception: SSOT + new file + AGENTS/hook staged together, skipping consistency check")
        return 0

    # Parse all three sources
    t_ssot = parse_ssot(ssot_file)
    t_agents_raw = set(parse_agents_paragraph(agents_file))
    t_hook_raw = set(parse_hook_gov_patterns(hook_file))

    # hook 委托判据：无 GOV_PATTERNS 字面 AND 引用 governed-files.txt
    # （排除混合 hook：vault 自身 hook 同时含 GOV_PATTERNS 字面 + governed-files.txt 引用，
    #   那不是委托——必须走 parse+compare 才能检出 vault 的 GOV_PATTERNS⇔SSOT drift）
    hook_content = hook_file.read_text(encoding='utf-8')
    hook_delegates = (not t_hook_raw) and ('governed-files.txt' in hook_content)
    if hook_delegates:
        # hook 读 SSOT → 一致由构造保证
        t_hook_effective = set(t_ssot)
    else:
        t_hook_effective = t_hook_raw

    errors = []

    # Determine if AGENTS delegates to SSOT (contains .meta/governed-files.txt reference)
    agents_delegates = any(
        'governed-files.txt' in normalize_token(t) for t in t_agents_raw
    )

    if agents_delegates:
        # AGENTS references SSOT as authoritative → effective match trivially
        t_agents_effective = set(t_ssot)
    else:
        t_agents_effective = t_agents_raw
        # Check T_agents == T_ssot (bidirectional)
        missing_in_agents = [t for t in t_ssot if not match_tokens(t, t_agents_effective)]
        extra_in_agents = [t for t in t_agents_effective if not match_tokens(t, set(t_ssot))]
        if missing_in_agents or extra_in_agents:
            errors.append("AGENTS.md ⇔ SSOT 不一致:")
            if missing_in_agents:
                errors.append(f"  AGENTS.md 缺少: {missing_in_agents}")
            if extra_in_agents:
                errors.append(f"  AGENTS.md 多出: {extra_in_agents}")

    # Check T_hook ⊇ T_ssot
    missing_in_hook = [t for t in t_ssot if not match_tokens(t, t_hook_effective)]
    if missing_in_hook:
        errors.append(f"pre-commit GOV_PATTERNS 缺少 (SSOT 有但 hook 无): {missing_in_hook}")

    if errors:
        print("❌ 治理文档一致性校验失败:")
        for e in errors:
            print(f"  {e}")
        return 1

    print(f"✓ 治理文档一致性校验通过 (SSOT: {len(t_ssot)} 项, hook: {len(t_hook_raw)} 项)")
    return 0


def sync() -> int:
    """Normal sync: copy AGENTS.md to CLAUDE.md and GEMINI.md."""
    if not SRC.exists():
        print(f"missing: {SRC}", file=sys.stderr)
        return 1

    for target in TARGETS:
        shutil.copyfile(SRC, target)

    src_hash = md5(SRC)
    mismatched = [target for target in TARGETS if md5(target) != src_hash]
    if mismatched:
        print(f"MD5 mismatch: {[str(path) for path in mismatched]}", file=sys.stderr)
        return 1

    print(f"synced: {src_hash}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync AGENTS.md → CLAUDE.md / GEMINI.md")
    parser.add_argument(
        '--check-gov-consistency', action='store_true',
        help='校验治理文档三方一致性 (SSOT / AGENTS 明线 / hook GOV_PATTERNS)'
    )
    args = parser.parse_args()

    if args.check_gov_consistency:
        return check_gov_consistency()
    return sync()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    from common import is_primary_host, log_script_run
    if not is_primary_host():
        print("FATAL: this script must run on PRIMARY_HOST", file=sys.stderr)
        sys.exit(1)
    log_script_run()
    sys.exit(main())
