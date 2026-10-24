#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
common.py — 所有维护脚本的共用工具

提供：
- 环境变量加载（.env）
- API 客户端（DeepSeek）+ 嵌入客户端（通用 OpenAI 兼容 /embeddings）
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
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional
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
# semantic 边保留阈值（build_graph 构造 + get_neighbors 遍历共享）；换嵌入模型后可校准
SEMANTIC_THRESHOLD = float(ENV.get('SEMANTIC_THRESHOLD') or 0.75)

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
    'AGENTS.md',
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

# ─── 大文件闸门（硬约束，嵌入/LLM 管线共用）─────────────────────────────────
# 单文件超过阈值（默认 1MB）不直接进入嵌入/LLM 管线（embed / summarize），
# 由脚本硬性挡下并登记挂起；维护完成后由 agent 向用户询问处理方式，决定写入
# .meta/rules/large-file-policy.md 后生效。检查编码在脚本内执行，不依赖 agent 自觉。
# BM25 / 图谱 / manifest 索引为本地直扫语料，不受本闸门影响（保留检索覆盖）。

LARGE_FILE_THRESHOLD_MB = float(ENV.get('LARGE_FILE_THRESHOLD_MB', '1'))
LARGE_FILE_THRESHOLD_BYTES = int(LARGE_FILE_THRESHOLD_MB * 1024 * 1024)
LARGE_FILE_POLICY_PATH = VAULT_ROOT / '.meta' / 'rules' / 'large-file-policy.md'
LARGE_FILE_PENDING_JSON = VAULT_ROOT / '.meta' / 'large-files-pending.json'
LARGE_FILE_PENDING_MD = VAULT_ROOT / '.meta' / 'large-files-pending.md'

# 用户决定记录文件的模板（首次发生挂起时由脚本自动创建）
LARGE_FILE_POLICY_TEMPLATE = """# 大文件处理策略（用户决定记录）

> **本文件只能记录用户的明确决定，禁止 agent 未经询问自行填写。**
> 挂起清单见 `.meta/large-files-pending.md`；维护完成后 agent 必须逐个向用户
> 询问处理方式（1 全套正常处理 / 2 仅排除嵌入与LLM / 3 本次跳过 / 4 切分 / 5 删除），
> 然后按下方格式把决定追加到「已决定」段。
>
> - `相对路径` → allow      （选项 1：全套正常处理，进嵌入/LLM 管线）
> - `相对路径` → llm_skip   （选项 2：仅排除嵌入/LLM，BM25/图谱/关键词照常）
> - `相对路径` → skip       （选项 3：本次跳过，下次维护再询问）
> - 选项 4（切分，需 agent 先给切分建议、用户确认后执行）与选项 5（删除）
>   完成后原文件已不存在/已变小，删除对应行即可，挂起清单自动消失。

## 已决定

<!-- 每行一条，格式：- `相对路径` → allow|llm_skip|skip -->
"""

LARGE_FILE_ACTION_RE = re.compile(
    r'^\s*[-*]\s+`?([^`\n]+?)`?\s*(?:→|->)\s*(allow|llm_skip|skip)\s*$', re.MULTILINE
)


def load_large_file_policy() -> dict:
    """解析用户决定记录：{相对路径: allow|llm_skip|skip}。文件不存在返回空。"""
    if not LARGE_FILE_POLICY_PATH.exists():
        return {}
    text = LARGE_FILE_POLICY_PATH.read_text(encoding='utf-8', errors='ignore')
    return {m.group(1).strip(): m.group(2) for m in LARGE_FILE_ACTION_RE.finditer(text)}


def file_oversize(path: Path, size: Optional[int] = None) -> bool:
    """单文件是否超过大文件阈值。"""
    if size is None:
        try:
            size = path.stat().st_size
        except OSError:
            return False
    return size > LARGE_FILE_THRESHOLD_BYTES


def llm_pipeline_check(path: Path, size: Optional[int] = None):
    """大文件闸门判定（嵌入/LLM 管线入口统一调用）。

    返回 (allowed: bool, reason: str)：
    - (True, '')                —— 未超阈值，或用户已决定 allow
    - (False, 'llm_skip')       —— 用户决定仅排除嵌入/LLM
    - (False, 'skip')           —— 用户决定本次跳过
    - (False, 'pending')        —— 超阈值且尚无用户决定（挂起）
    """
    if not file_oversize(path, size):
        return True, ''
    action = load_large_file_policy().get(rel_path(path), '')
    if action == 'allow':
        return True, ''
    if action in ('llm_skip', 'skip'):
        return False, action
    return False, 'pending'


def refresh_large_file_report() -> int:
    """重扫语料生成挂起清单（.meta/large-files-pending.{json,md}）。

    仅维护主入口（maintain.py）在 embed/summarize 结束后调用——单写者，避免
    并行子进程竞争。已决定 allow / llm_skip 的文件不出现在挂起清单；文件删除
    或缩回阈值内后条目自动消失。返回挂起文件数。"""
    policy = load_large_file_policy()
    oversize = []
    for md in scan_indexable_notes(scope='all'):
        try:
            size = md.stat().st_size
        except OSError:
            continue
        if size > LARGE_FILE_THRESHOLD_BYTES:
            oversize.append((rel_path(md), size))

    # 合并旧账目的 first_detected
    old_first = {}
    try:
        if LARGE_FILE_PENDING_JSON.exists():
            old = json.loads(LARGE_FILE_PENDING_JSON.read_text(encoding='utf-8'))
            old_first = {f['path']: f.get('first_detected', '') for f in old.get('files', [])}
    except Exception:
        pass

    today = datetime.now().strftime('%Y-%m-%d')
    pending = [
        {'path': rel, 'size': size,
         'first_detected': old_first.get(rel) or today, 'last_seen': today}
        for rel, size in sorted(oversize)
        if policy.get(rel) not in ('allow', 'llm_skip')
    ]

    LARGE_FILE_PENDING_JSON.parent.mkdir(parents=True, exist_ok=True)
    LARGE_FILE_PENDING_JSON.write_text(json.dumps({
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'threshold_mb': LARGE_FILE_THRESHOLD_MB,
        'files': pending,
    }, ensure_ascii=False, indent=2), encoding='utf-8')

    if pending and not LARGE_FILE_POLICY_PATH.exists():
        LARGE_FILE_POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
        LARGE_FILE_POLICY_PATH.write_text(LARGE_FILE_POLICY_TEMPLATE, encoding='utf-8')

    lines = [
        '# 大文件挂起清单',
        '',
        f'> 生成时间：{datetime.now().isoformat(timespec="seconds")} · '
        f'阈值：{LARGE_FILE_THRESHOLD_MB:g} MB · 挂起数：{len(pending)}',
        '',
        '以下文件**本轮维护未进入嵌入/LLM 管线**（embed / summarize 被脚本硬性挡下）；',
        'BM25 / 图谱 / manifest 索引不受影响，检索覆盖正常。',
        '',
        '维护完成后，**agent 必须逐个向用户询问处理方式**：',
        '',
        '1. **全套正常处理**——嵌入/LLM + BM25 + 图谱 + 关键词全部照常（决定写 `allow`）',
        '2. **仅排除嵌入/LLM**——BM25 / 图谱 / 关键词照做（决定写 `llm_skip`）',
        '3. **本次跳过该文件**——下轮维护再询问（决定写 `skip`）',
        '4. **切分文件**——agent 先提出具体切分建议（按 H2 章节 / 主题边界等），用户确认后执行',
        '5. **删除文件**——用户确认后删除',
        '',
        '决定写入 `.meta/rules/large-file-policy.md`（格式见该文件头注释）。',
        '',
        '| 文件 | 大小 | 首次发现 | 最近发现 |',
        '|---|---|---|---|',
    ]
    for f in pending:
        lines.append(f"| `{f['path']}` | {f['size'] / 1048576:.2f} MB | {f['first_detected']} | {f['last_seen']} |")
    lines.append('')
    LARGE_FILE_PENDING_MD.write_text('\n'.join(lines), encoding='utf-8')
    return len(pending)

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


# ─── 嵌入层（基础能力，必需；HTTP 端点优先，内置模型兜底）──────────────────
# 语义层是本套件的基础能力（office/非结构化仓库要靠它召回），默认启用：
#   auto  → 配了 base_url+model 走 HTTP；否则用内置 fastembed 模型
#   http  → 必须 base_url + model
#   builtin → 必须装有 fastembed
#   off   → 显式关闭（仅排障用；maintain/ask 会明确告警）
# 只有 LLM 层（摘要/精排）是可选增强。
EMBED_PROVIDERS = ('auto', 'http', 'builtin', 'off')
BUILTIN_EMBED_DEFAULT_MODEL = 'BAAI/bge-small-zh-v1.5'
# 分块默认值跟传输方式走：本地 bge-small 是 512 token（≈450 中文字符），
# HTTP 端点默认按 8k 上下文给 3000 字符。
HTTP_CHUNK_DEFAULT = (3000, 300)
BUILTIN_CHUNK_DEFAULT = (450, 50)

_warned_provider = False

# reason 码 → 可执行提示（ask/embed/maintain 共用同一套文案）
EMBED_REASON_HINTS = {
    'off': "语义层被显式关闭（EMBED_PROVIDER=off）；它是本套件的基础能力，"
           "除非排障否则请改回 auto",
    'missing_http': "EMBED_BASE_URL 与 EMBED_MODEL 必须成对填写（当前只填了一个）",
    'fastembed_missing': "内置嵌入模型不可用：请 pip install fastembed"
                         "（离线套装在 assets/packages/kb-harness/wheels）",
    'unknown_provider': "无法识别的 EMBED_PROVIDER（应为 auto|http|builtin|off）",
}


def _fastembed_available() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec('fastembed') is not None
    except Exception:
        return False


def embed_settings() -> dict:
    """解析语义层配置：provider ∈ {http, builtin, None} + 失败原因码。

    解析顺序（纯配置，不读数据库，避免把派生状态当真相）：
        EMBED_PROVIDER=off      → 关闭（reason=off）
        EMBED_PROVIDER=http     → 必须 base_url + model（否则 missing_http）
        EMBED_PROVIDER=builtin  → 必须装有 fastembed（否则 fastembed_missing）
        auto（默认）            → base_url+model 走 http；
                                  只填了一个 → missing_http（不静默降级）；
                                  都没填且装有 fastembed → builtin
    只填了旧版 ZHIPU_* 的仓库自动沿用为 http，避免既有安装静默降级。
    """
    global _warned_provider
    raw = (ENV.get('EMBED_PROVIDER') or 'auto').strip().lower()
    reason = ''
    if raw not in EMBED_PROVIDERS:
        reason = 'unknown_provider'
        if not _warned_provider:
            print(f"⚠️  EMBED_PROVIDER='{raw}' 无法识别，按 auto 处理", file=sys.stderr)
            _warned_provider = True
        raw = 'auto'

    base = (ENV.get('EMBED_BASE_URL') or '').strip().rstrip('/')
    model = (ENV.get('EMBED_MODEL') or '').strip()
    key = (ENV.get('EMBED_API_KEY') or '').strip()
    dim_hint = (ENV.get('EMBED_DIM') or '').strip()
    if not base and (ENV.get('ZHIPU_API_KEY') or '').strip():
        base = (ENV.get('ZHIPU_BASE_URL') or 'https://open.bigmodel.cn/api/paas/v4').strip().rstrip('/')
        model = model or (ENV.get('ZHIPU_EMBED_MODEL') or 'embedding-3').strip()
        key = key or (ENV.get('ZHIPU_API_KEY') or '').strip()
        dim_hint = dim_hint or (ENV.get('ZHIPU_EMBED_DIM') or '').strip()

    has_base, has_model = bool(base), bool(model)
    provider = None
    if raw == 'off':
        reason = 'off'
    elif raw == 'http':
        if has_base and has_model:
            provider = 'http'
        else:
            reason = 'missing_http'
    elif raw == 'builtin':
        if _fastembed_available():
            provider = 'builtin'
        else:
            reason = 'fastembed_missing'
    else:  # auto
        if has_base and has_model:
            provider = 'http'
        elif has_base or has_model:
            # 半配置：不猜测、不静默掉到内置模型
            reason = 'missing_http'
        elif _fastembed_available():
            provider = 'builtin'
        else:
            reason = 'fastembed_missing'

    if provider == 'builtin' and not model:
        model = BUILTIN_EMBED_DEFAULT_MODEL

    return {
        'enabled': provider is not None,
        'provider': provider,
        'base': base,
        'model': model,
        'key': key,
        'dim_hint': int(dim_hint) if dim_hint.isdigit() and int(dim_hint) > 0 else None,
        'reason': '' if provider else reason,
    }


def embed_unavailable_hint(settings=None) -> str:
    cfg = settings or embed_settings()
    return EMBED_REASON_HINTS.get(cfg.get('reason') or '', '语义层不可用')


def embedding_enabled() -> bool:
    return embed_settings()['enabled']


def llm_enabled() -> bool:
    """LLM 层（摘要/精排）可用性单一闸门：有 key 且未被显式 lite 关掉。

    未写 HARNESS_MODE 的旧仓库（有 key）视为启用，避免既有安装静默失去精排。
    """
    if not (ENV.get('DEEPSEEK_API_KEY') or '').strip():
        return False
    mode = (ENV.get('HARNESS_MODE') or '').strip().lower()
    return mode != 'lite'


def embed_chunk_defaults() -> tuple:
    """(字符数, 重叠) 默认值：内置模型 450/50，HTTP 端点 3000/300。"""
    return BUILTIN_CHUNK_DEFAULT if embed_settings()['provider'] == 'builtin' else HTTP_CHUNK_DEFAULT


class HttpEmbeddingsClient:
    """OpenAI 兼容 /embeddings 客户端——本地与云端同一份代码。

    覆盖 Ollama（`http://127.0.0.1:11434/v1`）、vLLM、LM Studio、智谱、
    OpenAI、各类兼容网关。维度以首次响应实测为准，不依赖 .env 里的声明。
    """

    provider = 'http'

    def __init__(self, settings=None, batch_size=32):
        cfg = settings or embed_settings()
        if not cfg['enabled']:
            raise RuntimeError(
                "语义层未启用：请在 .env 配置 EMBED_BASE_URL + EMBED_MODEL，"
                "或安装 fastembed 使用内置模型")
        self.base, self.model, self.key = cfg['base'], cfg['model'], cfg['key']
        self.dim_hint = cfg['dim_hint']
        self.batch_size = int(ENV.get('EMBED_BATCH') or batch_size)
        self.rate_ms = int(ENV.get('API_RATE_LIMIT_MS') or 200)
        self.dim = None

    def _headers(self) -> dict:
        h = {'Content-Type': 'application/json'}
        if self.key:
            h['Authorization'] = f'Bearer {self.key}'
        return h

    def _post(self, texts: list) -> list:
        _global_rate_limit(self.rate_ms)
        payload = {'model': self.model, 'input': texts}
        if self.dim_hint:
            payload['dimensions'] = self.dim_hint
        r = requests.post(f'{self.base}/embeddings', headers=self._headers(),
                          json=payload, timeout=120)
        if r.status_code == 404:
            raise RuntimeError(
                f"{self.base}/embeddings 返回 404：请确认 base_url 指向 OpenAI 兼容根"
                f"（Ollama 用 http://127.0.0.1:11434/v1）")
        r.raise_for_status()
        data = r.json()['data']
        if data and isinstance(data[0], dict) and 'index' in data[0]:
            data = sorted(data, key=lambda d: d['index'])
        return [d['embedding'] for d in data]

    def probe(self) -> int:
        """探针：验证端点可用并锁定真实维度。写库前调用，失败即抛错。"""
        vecs = self._post(['probe'])
        if not vecs:
            raise RuntimeError('探针返回空结果')
        self.dim = len(vecs[0])
        return self.dim

    def embed_batch(self, texts: list) -> list:
        """批量嵌入。返回同长度列表；失败项为 None，调用方应过滤。"""
        if not texts:
            return []
        results = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            try:
                vecs = self._post(batch)
                if len(vecs) != len(batch):
                    raise RuntimeError(f'返回条数不符：请求 {len(batch)} 实得 {len(vecs)}')
                results.extend(vecs)
            except Exception as e:
                print(f"      ✗ batch 失败，降级逐条: {e}")
                for text in batch:
                    try:
                        results.extend(self._post([text]))
                    except Exception as e1:
                        print(f"      ✗ 跳过 chunk (len={len(text)}): {e1}")
                        results.append(None)
        return results


class BuiltinEmbeddingsClient:
    """内置本地模型（fastembed + onnxruntime）：没有可用的 HTTP 端点时的降级解。

    零 API key、装完即离线可用；首次运行从 HuggingFace 拉模型（可用 HF_ENDPOINT
    指向镜像），缓存在 EMBED_CACHE_DIR（默认 ~/.cache/fastembed）。
    """

    provider = 'builtin'

    def __init__(self, settings=None, batch_size=32):
        cfg = settings or embed_settings()
        if not cfg['enabled']:
            raise RuntimeError("语义层未启用：请配置 EMBED_BASE_URL/EMBED_MODEL 或安装 fastembed")
        self.model = cfg['model'] or BUILTIN_EMBED_DEFAULT_MODEL
        self.batch_size = int(ENV.get('EMBED_BATCH') or batch_size)
        self.rate_ms = 0
        self.dim = None
        try:
            from fastembed import TextEmbedding
        except ImportError:
            raise RuntimeError("内置嵌入需要 fastembed：pip install fastembed")
        cache_dir = (ENV.get('EMBED_CACHE_DIR')
                     or str(Path.home() / '.cache' / 'fastembed'))
        try:
            self._embedder = TextEmbedding(model_name=self.model, cache_dir=cache_dir)
        except Exception as e:
            raise RuntimeError(f"内置模型 {self.model} 加载失败：{e}")

    def probe(self) -> int:
        vecs = self.embed_batch(['probe'])
        if not vecs or vecs[0] is None:
            raise RuntimeError('内置模型探针失败')
        self.dim = len(vecs[0])
        return self.dim

    def embed_batch(self, texts: list) -> list:
        if not texts:
            return []
        # fastembed 不接受空串，用单空格占位（只出现在极短文件）
        prepared = [t if t.strip() else ' ' for t in texts]
        try:
            return [v.tolist() for v in
                    self._embedder.embed(prepared, batch_size=self.batch_size)]
        except Exception as e:
            print(f"      ✗ 内置模型批量推理失败，降级逐条: {e}")
        results = []
        for text in prepared:
            try:
                results.append(next(iter(self._embedder.embed([text]))).tolist())
            except Exception as e:
                print(f"      ✗ 跳过 chunk (len={len(text)}): {e}")
                results.append(None)
        return results


def get_embeddings_client(settings=None):
    """按配置返回客户端：HTTP 端点优先，其次内置模型（fastembed）。"""
    cfg = settings or embed_settings()
    if not cfg['enabled']:
        raise RuntimeError("语义层未启用（EMBED_PROVIDER=off 或无可用的嵌入来源）")
    if cfg['provider'] == 'builtin':
        return BuiltinEmbeddingsClient(cfg)
    return HttpEmbeddingsClient(cfg)


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
