#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
common.py — 所有维护脚本的共用工具

提供：
- 环境变量加载（.env）
- API 客户端（DeepSeek、智谱）
- 路径/哈希/扫描工具
- Git 包装
- 主从检测
"""

import os
import sys
import re
import json
import posixpath
import hashlib
import subprocess
import time
import socket
from pathlib import Path
from typing import Iterator
from collections import defaultdict
from urllib.parse import unquote

import requests

# ─── Windows UTF-8 输出保障 ────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ─── 加载 .env ─────────────────────────────────────────────────────────────
def find_vault_root() -> Path:
    """从脚本位置向上找 vault 根（含 AGENTS.md 和 .meta/）"""
    p = Path(__file__).resolve()
    while p != p.parent:
        if (p / 'AGENTS.md').exists() and (p / '.meta').is_dir():
            return p
        p = p.parent
    raise RuntimeError("无法定位 vault root")

def load_env() -> dict:
    vault = find_vault_root()
    env = {}
    env_file = vault / '.env'
    if env_file.exists():
        for line in env_file.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    for k in list(env.keys()):
        if k in os.environ:
            env[k] = os.environ[k]
    return env

ENV = load_env()
VAULT_ROOT = find_vault_root()

# ─── 图与向量公共常量 ──────────────────────────────────────────────────────────
SEMANTIC_THRESHOLD = 0.75   # semantic 边保留阈值（build_graph 构造 + get_neighbors 遍历共享）

# ─── 排除规则 ──────────────────────────────────────────────────────────────
def _load_privacy_dirs() -> set:
    """隐私目录：env PRIVACY_DIRS 优先；否则从 category-privacy.md 的专用段落
    （## 隐私目录 / ## 受保护目录 等）的列表项提取**裸目录名**（不含路径分隔符）；
    否则空。注：只解析专用段的 `- ` 列表项，不扫全文任意【】token——避免把
    示例路径里的归档/分类目录名误当隐私目录（e2e 发现的回归）。"""
    env_val = os.environ.get('PRIVACY_DIRS', '').strip()
    if env_val:
        return {d.strip() for d in env_val.split(',') if d.strip()}
    cp = VAULT_ROOT / '.meta' / 'rules' / 'category-privacy.md'
    if cp.exists():
        text = cp.read_text(encoding='utf-8', errors='ignore')
        dirs = set()
        in_section = False
        for line in text.splitlines():
            if re.match(r'^#+\s*(隐私目录|受保护目录|受保护|隐私|Protected|Privacy)', line, re.IGNORECASE):
                in_section = True
                continue
            if in_section and re.match(r'^#+\s', line):
                in_section = False
                continue
            if not in_section:
                continue
            # 段内列表项：仅匹配整行恰为「NNN【name】」裸目录名（容忍尾斜杠），杜绝路径误匹配
            m = re.match(r'^\s*[-*]\s+`?([^\s`/]+【[^】]+】)`?/?`?\s*$', line)
            if m:
                dirs.add(m.group(1))
            # 裸英文目录名（无【】）：- `private/` 或 - private
            m2 = re.match(r'^\s*[-*]\s+`?([A-Za-z0-9_.\-]+)/?`?\s*$', line)
            if m2:
                dirs.add(m2.group(1))
        return dirs
    return set()

EXCLUDE_DIRS = {
    '.git', '.meta', '.index', '.obsidian', '.claude',
    '__pycache__', 'node_modules', 'venv', '.venv',
} | _load_privacy_dirs()

EXCLUDE_TOP_FILES = {
    'AGENTS.md', 'CLAUDE.md', 'GEMINI.md',
    'README.md', '.env', '.env.example', '.gitignore',
}

def is_excluded(path: Path) -> bool:
    try:
        rel = path.relative_to(VAULT_ROOT)
    except ValueError:
        return True
    parts = rel.parts
    for p in parts[:-1]:
        if p in EXCLUDE_DIRS:
            return True
    if len(parts) == 1 and parts[0] in EXCLUDE_TOP_FILES:
        return True
    return False

def scan_notes() -> Iterator[Path]:
    """扫描所有应被处理的 .md 文件"""
    for md in VAULT_ROOT.rglob('*.md'):
        if not is_excluded(md):
            yield md

# ─── Office 文档提取（sidecar 机制）────────────────────────────────────────
# 源 office 文件由 extract_office.py 提取纯文本到 .meta/office-extracts/ 镜像
# sidecar（.md），随后进入 embed / bm25 / build_index 检索链路。
OFFICE_EXTRACT_EXTS = {
    e.strip().lower()
    for e in ENV.get('OFFICE_EXTRACT_EXTS', '.docx,.xlsx,.pptx,.pdf').split(',')
    if e.strip()
}
OFFICE_LEGACY_EXTS = {'.doc', '.xls', '.ppt'}  # 老二进制格式：不提取，登记提示转存
OFFICE_EXTRACTS_DIR = VAULT_ROOT / '.meta' / 'office-extracts'

# 维护管线批量任务（API 调用/文件提取）的并发数
MAINTAIN_CONCURRENCY = max(1, int(ENV.get('MAINTAIN_CONCURRENCY', '6')))


def scan_office_docs() -> Iterator[Path]:
    """扫描待提取的 office 源文件（排除目录/隐私目录以外）。"""
    for f in VAULT_ROOT.rglob('*'):
        if not f.is_file():
            continue
        if f.suffix.lower() not in OFFICE_EXTRACT_EXTS:
            continue
        if not is_excluded(f):
            yield f


def scan_office_legacy_docs() -> Iterator[Path]:
    """扫描无法提取的老格式 office 文件（.doc/.xls/.ppt），供登记提示。"""
    for f in VAULT_ROOT.rglob('*'):
        if not f.is_file():
            continue
        if f.suffix.lower() not in OFFICE_LEGACY_EXTS:
            continue
        if not is_excluded(f):
            yield f


def scan_office_extracts() -> Iterator[Path]:
    """扫描 office 提取 sidecar（`_` 开头的报告文件除外）。"""
    if not OFFICE_EXTRACTS_DIR.exists():
        return
    for md in OFFICE_EXTRACTS_DIR.rglob('*.md'):
        if md.name.startswith('_'):
            continue
        yield md


def office_extract_source(extract_rel: str) -> str:
    """由 sidecar 相对路径推回源 office 文件的 vault 相对路径。
    '.meta/office-extracts/a/b.xlsx.md' → 'a/b.xlsx'；非 sidecar 路径原样返回。"""
    prefix = '.meta/office-extracts/'
    if extract_rel.startswith(prefix) and extract_rel.endswith('.md'):
        return extract_rel[len(prefix):-3]
    return extract_rel

def scan_memory_notes() -> Iterator[Path]:
    """扫描活跃 memory 记忆文件，不包含归档层。"""
    memory_root = VAULT_ROOT / '.meta' / 'memory'
    archive_root = memory_root / '.archive'
    if not memory_root.exists():
        return
    for md in memory_root.rglob('*.md'):
        try:
            md.relative_to(archive_root)
            continue
        except ValueError:
            yield md

def scan_indexable_notes(scope: str = 'all') -> Iterator[Path]:
    """扫描可进入 embeddings 的语料：用户笔记（含 office 提取 sidecar）、memory 或并集。"""
    if scope == 'notes':
        yield from scan_notes()
        yield from scan_office_extracts()
        return
    if scope == 'memory':
        yield from scan_memory_notes()
        return
    if scope != 'all':
        raise ValueError(f"unknown scope: {scope}")

    seen = set()
    for scanner in (scan_notes, scan_office_extracts, scan_memory_notes):
        for md in scanner():
            rel = rel_path(md)
            if rel in seen:
                continue
            seen.add(rel)
            yield md

def rel_path(p: Path) -> str:
    """统一用正斜杠的相对路径"""
    return str(p.relative_to(VAULT_ROOT)).replace('\\', '/')

# ─── 哈希 ──────────────────────────────────────────────────────────────────
def content_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()[:16]

# ─── 路径镜像 ──────────────────────────────────────────────────────────────
def meta_mirror(note_path: Path, subdir: str) -> Path:
    """笔记原文路径 → .meta/<subdir>/<mirrored-path>"""
    rel = note_path.relative_to(VAULT_ROOT)
    return VAULT_ROOT / '.meta' / subdir / rel

def ensure_parent(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)

# ─── API 客户端 ────────────────────────────────────────────────────────────

# 跨线程全局限速：ThreadPool 并发时各线程独立 sleep 会把有效速率放大 N 倍，
# 这里用共享时间戳 + 锁保证进程内请求间隔 ≥ rate_ms
import threading
_rate_lock = threading.Lock()
_rate_last_ts = 0.0


def _global_rate_limit(rate_ms: int):
    global _rate_last_ts
    if rate_ms <= 0:
        return
    interval = rate_ms / 1000
    with _rate_lock:
        now = time.monotonic()
        wait = _rate_last_ts + interval - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _rate_last_ts = now


class DeepSeekClient:
    def __init__(self):
        self.key = ENV.get('DEEPSEEK_API_KEY')
        self.base = ENV.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/v1')
        # 默认型号取最新代 DeepSeek 次等型号（批量任务成本控制；新代发布时随之升级）
        self.model = ENV.get('DEEPSEEK_MODEL', 'deepseek-v4-flash')
        self.rate_ms = int(ENV.get('API_RATE_LIMIT_MS', '200'))
        if not self.key:
            raise RuntimeError("DEEPSEEK_API_KEY 未设置")

    def chat(self, messages, temperature=0.3, max_tokens=512) -> str:
        for attempt in range(3):
            try:
                _global_rate_limit(self.rate_ms)
                r = requests.post(
                    f"{self.base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                    timeout=60,
                )
                r.raise_for_status()
                return r.json()['choices'][0]['message']['content'].strip()
            except requests.exceptions.RequestException as e:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)

    def extract_entities(self, original_query, accumulated_results, round_num):
        """从已检索结果中提取实体/子查询，供多跳迭代检索使用。

        round_num=2,4: 提取实体 + 构造子查询
        round_num=3:    提取矛盾/空白 + 构造验证查询

        返回 dict: {entities, sub_queries, knowledge_gaps} 或 {"error": "..."}
        """
        # 上下文截断：若累积文本过长则卡到 3000 字符
        truncated = accumulated_results
        if len(truncated) > 3000:
            truncated = truncated[:3000]

        # 按轮次选择 prompt 模板
        if round_num == 3:
            prompt = f"""你是一个研究助手。以下是从知识库中检索到的与问题「{original_query}」相关的笔记片段：
{truncated}

请完成两个任务：
1. 提取其中 1-2 个矛盾、空白或尚未充分覆盖的方向
2. 为每个空白构造一个验证查询，用于发现相反证据或补充信息

只输出 JSON（不要 markdown 代码块）：
{{
  "entities": [],
  "sub_queries": ["验证查询1", "验证查询2"],
  "knowledge_gaps": ["尚未覆盖的方向"]
}}"""
        else:
            prompt = f"""你是一个研究助手。以下是从知识库中检索到的与问题「{original_query}」相关的笔记片段：
{truncated}

请完成两个任务：
1. 提取其中 2-3 个最关键的实体/术语/概念（用于进一步检索）
2. 为每个实体构造一个子查询，用于发现相关但尚未覆盖的信息

只输出 JSON（不要 markdown 代码块）：
{{
  "entities": ["实体1", "实体2"],
  "sub_queries": ["子查询1", "子查询2"],
  "knowledge_gaps": ["尚未覆盖的方向"]
}}"""

        try:
            response = self.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=512,
            )
            # 清理可能的 markdown 代码块包裹
            cleaned = response.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                # 去掉首行 ```json 和末行 ```
                if len(lines) >= 3:
                    cleaned = "\n".join(lines[1:-1])
                else:
                    cleaned = response
            result = json.loads(cleaned)
            return result
        except (json.JSONDecodeError, KeyError) as e:
            return {"error": f"JSON parse failed: {e}"}
        except Exception as e:
            return {"error": f"API call failed: {e}"}

    def generate_synonyms(self, query, n=2):
        """生成查询的同义改写变体，用于退化回退。

        返回 list[str] 或空列表（失败时）。
        """
        prompt = (
            f'Generate {n} synonym/paraphrase variants of the following query. '
            f'Return ONLY a JSON object (no markdown code block):\n\n'
            f'{{"variants": ["variant 1", "variant 2"]}}\n\n'
            f'Query: {query}'
        )

        try:
            response = self.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=256,
            )
            cleaned = response.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                if len(lines) >= 3:
                    cleaned = "\n".join(lines[1:-1])
            result = json.loads(cleaned)
            return result.get("variants", [])
        except (json.JSONDecodeError, KeyError):
            return []
        except Exception:
            return []


class ZhipuEmbedClient:
    def __init__(self):
        self.key = ENV.get('ZHIPU_API_KEY')
        self.base = ENV.get('ZHIPU_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4')
        self.model = ENV.get('ZHIPU_EMBED_MODEL', 'embedding-3')
        self.dim = int(ENV.get('ZHIPU_EMBED_DIM', '2048'))
        self.rate_ms = int(ENV.get('API_RATE_LIMIT_MS', '200'))
        if not self.key:
            raise RuntimeError("ZHIPU_API_KEY 未设置")

    def _post_embed(self, batch: list):
        _global_rate_limit(self.rate_ms)
        return requests.post(
            f"{self.base}/embeddings",
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
            json={"model": self.model, "input": batch, "dimensions": self.dim},
            timeout=60,
        )

    def _embed_one(self, text: str):
        """单条嵌入。成功返回 vec，400/其它失败返回 None（含日志）。"""
        for attempt in range(2):
            try:
                r = self._post_embed([text])
                if r.status_code == 400:
                    body = r.text[:150].replace('\n', ' ')
                    print(f"        ✗ 跳过 chunk (len={len(text)}): 400 {body}")
                    return None
                r.raise_for_status()
                return r.json()['data'][0]['embedding']
            except requests.exceptions.RequestException as e:
                if attempt == 1:
                    print(f"        ✗ 跳过 chunk (len={len(text)}): {e}")
                    return None
                time.sleep(2 ** attempt)
        return None

    def embed_batch(self, texts: list) -> list:
        """批量嵌入。返回同长度列表；失败项为 None，调用方应过滤。"""
        results = []
        for i in range(0, len(texts), 64):
            batch = texts[i:i + 64]
            ok = False
            for attempt in range(3):
                try:
                    r = self._post_embed(batch)
                    if r.status_code == 400:
                        break  # 直接进入逐条降级
                    r.raise_for_status()
                    data = r.json()['data']
                    results.extend([d['embedding'] for d in data])
                    ok = True
                    break
                except requests.exceptions.RequestException as e:
                    if attempt == 2:
                        print(f"      ✗ batch 失败，降级逐条: {e}")
                        break
                    time.sleep(2 ** attempt)
            if not ok:
                print(f"      ↓ batch 400 或全重试失败，逐条重试 {len(batch)} 条")
                for text in batch:
                    results.append(self._embed_one(text))
        return results


# ─── Git 包装 ──────────────────────────────────────────────────────────────

def git(*args, check=True):
    env = os.environ.copy()
    env['GIT_PAGER'] = 'cat'
    return subprocess.run(
        ['git'] + list(args),
        cwd=str(VAULT_ROOT),
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        env=env,
        check=check,
    )

def git_available() -> bool:
    r = git('rev-parse', '--git-dir', check=False)
    return r.returncode == 0

def is_primary_host() -> bool:
    primary = ENV.get('PRIMARY_HOST', '').strip()
    if not primary:
        return True
    return socket.gethostname() == primary


# ═══════════════════════════════════════════════════════════════════════════════
#  Link Resolution Utilities (shared across build_graph / semantic_lint / ask / summarize)
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Frontmatter regexes ──────────────────────────────────────────────────────

FM_ALIASES_INLINE_RE = re.compile(r'^aliases\s*:\s*\[([^\]]*)\]\s*$', re.MULTILINE)
FM_ALIASES_BLOCK_RE = re.compile(
    r'^aliases\s*:\s*\n((?:\s+-\s+.+\n?)+)', re.MULTILINE
)
FM_ALIAS_SINGLE_RE = re.compile(r'^alias\s*:\s*(.+)\s*$', re.MULTILINE)

# ─── Link regexes ─────────────────────────────────────────────────────────────
# WIKILINK_RE: [[target]], [[target|alias]], [[target#heading]], mixed
# group(1) = bare target (no #, no |)
WIKILINK_RE = re.compile(
    r'\[\[([^\[\]|#\n]+?)(?:#[^\]|\n]*)?(?:\|[^\]\n]*)?\]\]'
)
# MDLINK_RE: [text](target) — negative lookbehind excludes ![]() image syntax
MDLINK_RE = re.compile(r'(?<!\!)\[([^\]]*)\]\(([^)\s]+)\)')

INTERNAL_ATTACHMENT_EXTS = {
    '.pdf', '.pptx', '.png', '.jpg', '.jpeg', '.gif',
    '.doc', '.docx', '.xlsx', '.csv', '.html', '.mp4', '.zip',
    '.py',
}
INTERNAL_LINK_EXTS = INTERNAL_ATTACHMENT_EXTS | {'.md'}
INLINE_CODE_RE = re.compile(r'`[^`\n]*`')
SCHEME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*:')
BARE_DOMAIN_RE = re.compile(r'^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/.*)?$')


# ─── Frontmatter parsing ──────────────────────────────────────────────────────

def extract_frontmatter(content: str) -> str:
    """返回 frontmatter 文本（不含 --- 分隔符）；没有则返回空串。"""
    if not content.startswith('---'):
        return ''
    end = content.find('\n---', 3)
    if end < 0:
        return ''
    return content[3:end]


def parse_aliases(content: str) -> list:
    """从 frontmatter 提取 aliases 列表，支持三种写法：
    - aliases: [a, b, c]
    - aliases:\n  - a\n  - b
    - alias: single
    """
    fm = extract_frontmatter(content)
    if not fm:
        return []

    def _clean(s: str) -> str:
        return s.strip().strip('"').strip("'").strip()

    # inline list
    m = FM_ALIASES_INLINE_RE.search(fm)
    if m:
        return [_clean(a) for a in m.group(1).split(',') if a.strip()]

    # block list
    m = FM_ALIASES_BLOCK_RE.search(fm)
    if m:
        out = []
        for line in m.group(1).splitlines():
            m2 = re.match(r'^\s+-\s+(.+)$', line)
            if m2:
                out.append(_clean(m2.group(1)))
        return out

    # single alias
    m = FM_ALIAS_SINGLE_RE.search(fm)
    if m:
        return [_clean(m.group(1))]

    return []


# ─── Link extraction ──────────────────────────────────────────────────────────

def strip_inline_code(line: str) -> str:
    """移除单行 inline code，避免代码样例被当作链接。"""
    return INLINE_CODE_RE.sub('', line)


def iter_markdown_content_lines(text: str):
    """逐行产出非 fenced-code 的 Markdown 内容，保留原行号。"""
    in_fence = False
    fence_marker = ''
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith(('```', '~~~')):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ''
            continue
        if in_fence:
            continue
        yield line_no, strip_inline_code(line)


def find_all_wikilinks(text: str) -> list:
    """提取所有 [[...]] 中的链接目标（已由正则去掉 # 和 | 部分）。"""
    out = []
    for _, line in iter_markdown_content_lines(text):
        out.extend(m.group(1).strip() for m in WIKILINK_RE.finditer(line))
    return out


def find_all_mdlinks(text: str) -> list:
    """提取所有 [text](target) 中的 (display, target) 对。
    自动跳过外部 URL（http/https/app）和纯锚点链接（#...）。"""
    results = []
    for _, line in iter_markdown_content_lines(text):
        for display, target in _scan_mdlinks_in_line(line):
            if is_internal_mdlink_candidate(target):
                results.append((display, target))
    return results


def _find_unescaped(text: str, char: str, start: int) -> int:
    escaped = False
    for i in range(start, len(text)):
        c = text[i]
        if escaped:
            escaped = False
            continue
        if c == '\\':
            escaped = True
            continue
        if c == char:
            return i
    return -1


def _scan_mdlinks_in_line(line: str) -> list:
    """扫描单行 Markdown 链接，支持 target 中出现括号。"""
    results = []
    i = 0
    while i < len(line):
        if line[i] != '[' or (i > 0 and line[i - 1] == '!'):
            i += 1
            continue
        close = _find_unescaped(line, ']', i + 1)
        if close < 0 or close + 1 >= len(line) or line[close + 1] != '(':
            i += 1
            continue
        target_start = close + 2
        depth = 1
        j = target_start
        escaped = False
        while j < len(line):
            c = line[j]
            if escaped:
                escaped = False
            elif c == '\\':
                escaped = True
            elif c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    display = line[i + 1:close].strip()
                    target = line[target_start:j].strip()
                    if target.startswith('<') and target.endswith('>'):
                        target = target[1:-1].strip()
                    results.append((display, target))
                    i = j + 1
                    break
            j += 1
        else:
            i += 1
            continue
    return results


def is_internal_mdlink_candidate(target: str) -> bool:
    """判断 markdown link target 是否值得按库内链接解析。"""
    t = unquote(target.strip()).split('#')[0].split('?', 1)[0].strip()
    if not t or t.startswith('#'):
        return False
    if '@' in t:
        return False
    lower = t.lower()
    if lower.startswith(('http://', 'https://', 'www.', 'mailto:', 'app://')):
        return False
    if SCHEME_RE.match(t):
        return False

    ext = Path(t).suffix.lower()
    if ext:
        return ext in INTERNAL_LINK_EXTS
    if BARE_DOMAIN_RE.match(t):
        return False
    return '/' in t


# ─── Link resolution ──────────────────────────────────────────────────────────

def pick_closest_by_prefix(source: str, candidates: list) -> str:
    """复刻 Obsidian：多个同名候选时，选『与 source 共享目录前缀最长』的那个，
    平手时按字典序取最小（稳定排序）。"""
    source_dir = source.rsplit('/', 1)[0] if '/' in source else ''
    source_parts = source_dir.split('/') if source_dir else []

    def score(cand: str):
        cand_dir = cand.rsplit('/', 1)[0] if '/' in cand else ''
        cand_parts = cand_dir.split('/') if cand_dir else []
        common = 0
        for a, b in zip(source_parts, cand_parts):
            if a == b:
                common += 1
            else:
                break
        return (-common, cand)

    return min(candidates, key=score)


def resolve_wikilink(target: str, source_path: str,
                     basename_index: dict, aliases_index: dict,
                     all_paths: set):
    """复刻 Obsidian wikilink 解析算法。
    支持 [[target]]、[[target|alias]]、[[target#heading]]、[[../path/note]]。
    返回 (resolved_path, None) 成功，(None, reason) 失败。"""
    t = target.strip()
    if not t:
        return None, 'empty target'

    # 规则一：带 / —— vault-absolute 或相对路径（如 ../）
    if '/' in t:
        if t.startswith('..') or t.startswith('.'):
            source_dir = source_path.rsplit('/', 1)[0] if '/' in source_path else ''
            cand = posixpath.normpath(posixpath.join(source_dir, t))
        else:
            cand = t
        if not cand.endswith('.md'):
            cand += '.md'
        if cand in all_paths:
            return cand, None
        return None, f'path not found: {t}'

    # 规则二：frontmatter aliases
    if t in aliases_index:
        return aliases_index[t], None

    # 规则三：basename（可能带 .md 后缀）
    key = t[:-3] if t.endswith('.md') else t
    candidates = basename_index.get(key, [])
    if not candidates:
        return None, f'basename not found: {t}'
    if len(candidates) == 1:
        return candidates[0], None
    return pick_closest_by_prefix(source_path, candidates), None


def resolve_mdlink(target: str, source_path: str,
                   basename_index: dict, aliases_index: dict,
                   all_paths: set):
    """解析 markdown [text](target) 链接。
    预处理：URL decode + 剥除锚点。
    Rule 1 相对于 source dir 解析（而非 vault root）。
    返回 (resolved_path, None) 成功，(None, reason) 失败。"""
    t = unquote(target.strip())
    # 剥除锚点
    t = t.split('#')[0].strip()
    if not t:
        return None, 'empty target'

    # 规则一：带 / —— 相对路径，相对于源文件所在目录解析
    if '/' in t:
        source_dir = source_path.rsplit('/', 1)[0] if '/' in source_path else ''
        cand = posixpath.normpath(posixpath.join(source_dir, t))
        if not cand.endswith('.md'):
            cand_md = cand + '.md'
            if cand_md in all_paths:
                return cand_md, None
        if cand in all_paths:
            return cand, None
        return None, f'path not found: {t}'

    # 规则二：frontmatter aliases
    if t in aliases_index:
        return aliases_index[t], None

    # 规则三：basename（可能带 .md 后缀）
    key = t[:-3] if t.endswith('.md') else t
    candidates = basename_index.get(key, [])
    if not candidates:
        return None, f'basename not found: {t}'
    if len(candidates) == 1:
        return candidates[0], None
    return pick_closest_by_prefix(source_path, candidates), None


# ═══════════════════════════════════════════════════════════════════════════════
#  LLM-as-Judge Re-rank
# ═══════════════════════════════════════════════════════════════════════════════

# Session-level rerank cache
_rerank_cache = {}

def rerank_with_llm(query, candidates, top_m=5):
    """LLM-as-Judge 精排：对候选 chunk 做相关性打分（1-10）

    Args:
        query: 原始查询字符串
        candidates: [(path, chunk_index, text), ...] 候选列表
        top_m: 返回 top-m 结果

    Returns:
        [(path, chunk_index, score), ...] 精排后的结果，score 为归一化后的 LLM 打分（0-1）
    """
    import hashlib, json

    if not candidates:
        return []

    # Check cache
    q_hash = hashlib.md5(query.encode()).hexdigest()[:8]
    cache_hits = []
    uncached = []
    for item in candidates:
        path, chunk_idx, text = item
        c_hash = hashlib.md5(text[:200].encode()).hexdigest()[:8]
        cache_key = (q_hash, c_hash)
        if cache_key in _rerank_cache:
            cache_hits.append((*item, _rerank_cache[cache_key]))
        else:
            uncached.append(item)

    if uncached:
        # Batch process uncached (max 20 per call)
        batch = uncached[:20]
        prompt_lines = [
            f'查询：「{query}」',
            '',
            '对以下候选段落与查询的相关性打分（1-10 分，10=最相关），只返回 JSON 数组：',
            '',
        ]
        for i, (path, chunk_idx, text) in enumerate(batch):
            # Truncate each candidate to 300 chars for prompt
            snippet = text[:300].replace('\n', ' ')
            prompt_lines.append(f'[{i}] {snippet}')

        prompt = '\n'.join(prompt_lines)

        try:
            client = DeepSeekClient()
            response = client.chat([
                {'role': 'system', 'content': '你是一个搜索相关性评估器。只返回 JSON 数组，不要其他内容。'},
                {'role': 'user', 'content': prompt},
            ], temperature=0, max_tokens=256)

            # Parse JSON array
            # Strip markdown code fences if present
            response = response.strip()
            if response.startswith('```'):
                response = response.split('\n', 1)[1].rsplit('\n```', 1)[0]

            scores = json.loads(response)
            if not isinstance(scores, list):
                scores = []

            # Cache and collect
            for s_entry in scores:
                idx = s_entry.get('index', -1)
                score = s_entry.get('score', 5)
                if 0 <= idx < len(batch):
                    path, chunk_idx, text = batch[idx]
                    c_hash = hashlib.md5(text[:200].encode()).hexdigest()[:8]
                    cache_key = (q_hash, c_hash)
                    _rerank_cache[cache_key] = score
                    cache_hits.append((path, chunk_idx, text, score))
        except Exception as e:
            print(f"⚠️  Re-rank API 不可用，跳过精排: {e}", file=sys.stderr)
            print("rerank_degraded", file=sys.stderr)
            # Fallback: return original order with dummy scores
            for item in uncached:
                cache_hits.append((*item, 5.0))

    # Normalize scores to 0-1 and sort
    if cache_hits:
        scores = [s for *_, s in cache_hits]
        s_min, s_max = min(scores), max(scores)
        s_range = s_max - s_min if s_max > s_min else 1.0
        normalized = [(*item[:-1], (item[-1] - s_min) / s_range) for item in cache_hits]
        normalized.sort(key=lambda x: -x[-1])
        return [(path, chunk_idx, score) for path, chunk_idx, _, score in normalized[:top_m]]

    return [(path, chunk_idx, 0.5) for path, chunk_idx, _ in candidates[:top_m]]


# ─── Usage Tracking ──────────────────────────────────────────────────
def log_script_run():
    """追加脚本调用记录到 .meta/usage-log.jsonl（静默失败）。"""
    try:
        import json as _json
        from datetime import datetime as _dt
        entry = _json.dumps({
            "ts": _dt.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": "script",
            "name": Path(sys.argv[0]).name,
            "argv": sys.argv[1:],
        }, ensure_ascii=False)
        log_path = VAULT_ROOT / ".meta" / "usage-log.jsonl"
        with open(log_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(entry + "\n")
    except Exception:
        pass  # 追踪不能中断脚本正常运行
