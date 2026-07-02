#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inbox_scan.py — 收件箱扫描 + LLM 分类建议

扫描根目录未归类文件，用 DeepSeek 推荐分类。
建议缓存到 .meta/inbox-suggestions.json，避免重复调用 API。

用法: python .meta/scripts/inbox_scan.py
"""

import sys
import json
import re
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import VAULT_ROOT, content_hash, DeepSeekClient

CACHE_PATH = VAULT_ROOT / '.meta' / 'inbox-suggestions.json'
TAXONOMY_PATH = VAULT_ROOT / 'docs' / 'TAXONOMY.md'

TOP_EXCLUDES = {
    'AGENTS.md', 'CLAUDE.md', 'GEMINI.md',
    'README.md', '.env', '.env.example', '.gitignore',
}

PROMPT_TEMPLATE = """你是一个个人知识库的分类助手。

请根据笔记的"动作意图"（写这篇笔记时的主要目的）推荐最合适的 1-2 个分类目录。

可选分类及定义：
{taxonomy}

归类原则：
- 按"动作意图最近"判断，不是按关键词字面匹配

只输出分类名，用空格分隔。最多 2 个。如果只有一个最合适，只输出一个。

示例：（从上方可选分类中选取，空格分隔，最多 2 个）

笔记内容：
{content}"""


def load_taxonomy() -> str:
    """从 TAXONOMY.md 读取分类定义（前 60 行）。"""
    if not TAXONOMY_PATH.exists():
        return "（分类定义文件不存在；请在 docs/TAXONOMY.md 定义分类）"
    try:
        lines = TAXONOMY_PATH.read_text(encoding='utf-8').splitlines()
        return '\n'.join(lines[:60])
    except Exception:
        return "（分类定义文件读取失败）"


def load_taxonomy_dirs():
    """从 TAXONOMY.md 提取所有目录名集合（不预设命名格式）。

    提取策略（按优先级）：
    1. NNN【name】格式 → 取整个 'NNN【name】'
    2. 反引号内的目录名（`xxx/` 或 `xxx`）
    3. markdown 表格/列表中以 `/` 结尾或形如目录的 token

    返回目录名列表（去重）；TAXONOMY.md 不存在则返回 []。
    """
    if not TAXONOMY_PATH.exists():
        return []
    try:
        content = TAXONOMY_PATH.read_text(encoding='utf-8')
    except Exception:
        return []
    dirs = []
    seen = set()
    # 1. NNN【name】格式
    for m in re.findall(r'\d{2,3}【[^】]+】', content):
        if m not in seen:
            seen.add(m)
            dirs.append(m)
    # 2. 反引号内的目录名（含/或不含尾斜杠）
    for m in re.findall(r'`([A-Za-z0-9_\-./一-鿿]+)`', content):
        candidate = m.strip().rstrip('/')
        # 只要像目录名（含中文或/或字母数字段）且非纯数字
        if candidate and candidate not in seen and (
            '/' in candidate or re.search(r'[一-鿿]', candidate)
        ):
            seen.add(candidate)
            dirs.append(candidate)
    return dirs


def get_inbox_files():
    """扫描根目录收件箱文件"""
    files = []
    for p in VAULT_ROOT.iterdir():
        if not p.is_file() or p.suffix != '.md':
            continue
        if p.name in TOP_EXCLUDES:
            continue
        files.append(p)
    return files


def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def save_cache(cache: dict):
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')


def suggest_category(filepath: Path, taxonomy: str, client: DeepSeekClient, taxonomy_dirs=None) -> dict:
    """调用 DeepSeek 获取分类建议。

    解析策略（不预设命名格式）：
    - 优先用 taxonomy_dirs（从 TAXONOMY.md 实际目录集合）匹配 LLM 输出
    - taxonomy_dirs 为空时，退回旧正则 \\d{3}【...】 作为 fallback（仅当检测到此格式时）
    - 都失败则取原始输出的第一个 token
    """
    try:
        content = filepath.read_text(encoding='utf-8')
    except Exception as e:
        return {'suggestion': '(无法读取文件)', 'reason': str(e)}

    preview = content[:1500]
    prompt = PROMPT_TEMPLATE.format(taxonomy=taxonomy, content=preview)

    try:
        raw = client.chat([{"role": "user", "content": prompt}], max_tokens=60, temperature=0.3)
    except Exception as e:
        return {'suggestion': '(API 失败)', 'reason': str(e)}

    suggestion = _match_category(raw, taxonomy_dirs)
    return {
        'suggestion': suggestion,
        'reason': f'基于前 {len(preview)} 字内容判断',
    }


def _match_category(raw, taxonomy_dirs):
    """从 LLM 输出提取分类名，用 TAXONOMY.md 实际目录集合做匹配（不预设命名格式）。

    - taxonomy_dirs 非空：匹配 LLM 输出中出现的任一已知目录名
    - taxonomy_dirs 为空：fallback 到旧 \\d{3}【...】 正则（兼容无 TAXONOMY 仓库）
    """
    raw_stripped = raw.strip()
    if taxonomy_dirs:
        # 匹配 LLM 输出中出现的任一已知目录名（不预设 NNN【name】格式）
        hits = [d for d in taxonomy_dirs if d in raw_stripped]
        if hits:
            return ' '.join(hits[:2])
        # fallback：取第一个 token
        tokens = raw_stripped.split()
        return tokens[0] if tokens else raw_stripped[:60]
    # taxonomy_dirs 为空：保留旧正则作 fallback（仅当输出确为 NNN【】格式时生效）
    categories = re.findall(r'\d{3}【[^】]+】', raw_stripped)
    if categories:
        return ' '.join(categories[:2])
    tokens = raw_stripped.split()
    return tokens[0] if tokens else raw_stripped[:60]


def main():
    print("  ── 收件箱扫描 ──")
    files = get_inbox_files()
    if not files:
        print("  ✓ 收件箱为空")
        return []

    taxonomy = load_taxonomy()
    taxonomy_dirs = load_taxonomy_dirs()
    cache = load_cache()
    client = DeepSeekClient()

    results = []
    for fp in files:
        h = content_hash(fp)
        key = str(fp.relative_to(VAULT_ROOT)).replace('\\', '/')

        # 检查缓存
        cached = cache.get(key)
        if cached and cached.get('hash') == h:
            print(f"  [cache] {fp.name} → {cached['suggestion']}")
            results.append({
                'name': fp.name,
                'lines': len(fp.read_text(encoding='utf-8').splitlines()),
                'suggestion': cached['suggestion'],
                'reason': cached.get('reason', ''),
            })
            continue

        print(f"  [LLM] {fp.name}")
        info = suggest_category(fp, taxonomy, client, taxonomy_dirs=taxonomy_dirs)
        cache[key] = {
            'hash': h,
            'suggestion': info['suggestion'],
            'reason': info['reason'],
            'generated_at': datetime.now().isoformat(),
        }
        results.append({
            'name': fp.name,
            'lines': len(fp.read_text(encoding='utf-8').splitlines()),
            'suggestion': info['suggestion'],
            'reason': info['reason'],
        })

    save_cache(cache)
    print(f"  ✓ 收件箱扫描完成（{len(results)} 项）")
    return results


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
    items = main()
    for it in items:
        print(f"\n  {it['name']}")
        print(f"    建议: {it['suggestion']}")
        print(f"    依据: {it['reason']}")
