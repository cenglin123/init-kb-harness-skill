#!/usr/bin/env python3
"""模型自检：识别当前 Agent 实际调用的 provider 与 model。

为什么需要：harness（如 Claude Code、Codex CLI）可能通过环境变量重定向到非
官方后端（Kimi / DeepSeek / Zhipu / Qwen / 自建网关 ……）。harness 注入的
commit 模板或 UI 文案中的"Claude"字样不代表实际模型——准确自识对：
  - commit 署名诚实
  - 评估自身口癖、推理风格
  - token 成本与速率限制估计
都很重要。

用法：
    python .meta/scripts/whoami.py                  # 自检（人读）
    python .meta/scripts/whoami.py --frontmatter    # 输出 agent 溯源 YAML（created_by/model/generated_at）

Provider 识别分两层：
  Layer 1 · Harness 路由（detect_harness）：
    优先级（强 → 弱）：
      1. CODEX_THREAD_ID env → 'codex'（Codex 当前线程专有运行时信号）
      2. CLAUDECODE=1 env → 'claude-code'（Claude Code 运行时专有信号）
      3. OPENCODE_BIN_PATH env → 'opencode'
      4. cc-switch.db 存在 或 ~/.claude/settings.json 存在 → 'claude-code'（弱信号）
      5. 兜底 → 'unknown'（按 Claude Code 流程处理）
    设计：CODEX_THREAD_ID / CLAUDECODE=1 是当前 harness 注入的专有运行时信号，
         优先于可能从父 shell 残留的 OPENCODE_BIN_PATH。ANTHROPIC_* env 不作为
         harness 检测信号——可能从 shell
         profile/.env 泄漏到 opencode 子进程造成假阳性（详见 detect_harness() 设计说明）。
         opencode 命中后**独占**——一旦判定为 opencode，main() 不再读 cc-switch /
         .claude/settings.json / ANTHROPIC_* env，避免 Claude Code 残留状态在 opencode
         会话内假阳性。

  Layer 2 · 各 harness 内部 Provider 识别：
    codex：
      - provider 固定报告为 OpenAI / Codex；当前进程环境未暴露精确模型 ID，
        因而只输出稳定身份 `openai/codex`，不从其他 harness 状态猜测模型。
    claude-code（原逻辑，高 → 低）：
      1. 显式 BASE_URL（env 或 settings）→ 按 URL 匹配（字节实际流向，最强信号）
      2. cc-switch 激活 provider（~/.cc-switch/cc-switch.db 中 app_type=claude & is_current=1）
      3. 默认 Anthropic 直连
    opencode：
      1. opencode.db session 表最新行的 model 列（权威——随会话切换实时更新）；
         兜底：~/.local/state/opencode/model.json 的 recent[0]（去重导航历史，可能过时）
      2. providerID → OPENCODE_PROVIDER_LABELS 映射 / 原始 ID + (未映射) 兜底
      3. baseURL 优先从 opencode.json provider.options.baseURL 取（自定义 provider）；
         内置 provider 无显式 baseURL（仅 auth.json 有凭据）

  注：不用 MODEL 名字符串子串猜 provider——那是装饰标签，按名猜会假阳性
      （例：通用配置里写死 "deepseek-v4-pro" 但实际跑 Claude Official）。

settings/env 读取来源（用于 BASE_URL / MODEL 标签，优先级高 → 低）：
  1. process env：ANTHROPIC_BASE_URL / ANTHROPIC_MODEL / ANTHROPIC_DEFAULT_*_MODEL
  2. 项目 .claude/settings.local.json
  3. 项目 .claude/settings.json
  4. 用户 ~/.claude/settings.local.json
  5. 用户 ~/.claude/settings.json
  settings 支持顶层 "model" 字段，或 "env" 段下的 ANTHROPIC_* 变量。

cc-switch DB 路径可用环境变量 CC_SWITCH_DB 覆盖。只读非敏感列（name/category/
settings_config.model），绝不读取 token/key。

不进行：网络请求、token 消耗、settings 写入。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import NamedTuple, Optional


PROVIDER_HINTS: dict[str, str] = {
    'anthropic.com': 'Anthropic（官方）',
    'kimi.com': 'Moonshot / Kimi',
    'moonshot': 'Moonshot / Kimi',
    'deepseek': 'DeepSeek',
    'openai.com': 'OpenAI',
    'azure.com': 'Azure OpenAI',
    'bigmodel.cn': 'Zhipu / GLM',
    'zhipu': 'Zhipu / GLM',
    'dashscope.aliyuncs.com': 'Alibaba / Qwen',
    'qwen': 'Alibaba / Qwen',
    'siliconflow': 'SiliconFlow',
    'volces.com': 'Volcengine / 字节',
    'doubao': 'Doubao / 字节',
    'minimax': 'MiniMax',
    'baichuan': 'Baichuan',
    'yi.01.ai': '01.AI / Yi',
    'lingyiwanwu': '01.AI / Yi',
    'mistral.ai': 'Mistral',
    'cohere.ai': 'Cohere',
    'localhost': '本地（自建）',
    '127.0.0.1': '本地（自建）',
    '0.0.0.0': '本地（自建）',
}


# 字段 → 对应环境变量名
ENV_KEYS: dict[str, str] = {
    'base_url': 'ANTHROPIC_BASE_URL',
    'model': 'ANTHROPIC_MODEL',
    'sonnet': 'ANTHROPIC_DEFAULT_SONNET_MODEL',
    'opus': 'ANTHROPIC_DEFAULT_OPUS_MODEL',
    'haiku': 'ANTHROPIC_DEFAULT_HAIKU_MODEL',
}


class FieldSource(NamedTuple):
    """每个字段的最终值与来源标记。

    origin 取值：
      - 'env'           ：来自 process environment
      - 'settings:<p>'  ：来自指定 settings.json
      - ''              ：未设置
    """
    value: str
    origin: str


def read_ccswitch_active(app_type: str = 'claude') -> Optional[dict]:
    """读取 cc-switch 当前激活 provider（权威源）。

    cc-switch 是 provider 切换器，其 SQLite DB 按 app_type 记录"当前 provider"
    （is_current=1）。这是"实际激活后端"的权威来源——优于 settings.json 里可能被
    cc-switch「通用配置」写死、与真实后端解耦的 model 标签。

    安全：只读 name/category 与 settings_config 中的 model 子键；绝不读取或返回
    token/key 等敏感字段。以只读模式打开，DB 缺失/加锁/schema 不符时静默返回 None。
    路径可用环境变量 CC_SWITCH_DB 覆盖，默认 ~/.cc-switch/cc-switch.db。
    """
    db_path = os.environ.get('CC_SWITCH_DB', '').strip() or str(
        Path.home() / '.cc-switch' / 'cc-switch.db')
    if not Path(db_path).exists():
        return None
    try:
        con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=1.0)
        try:
            row = con.execute(
                'SELECT name, category, settings_config FROM providers '
                'WHERE is_current=1 AND app_type=? LIMIT 1', (app_type,)
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row:
        return None
    name, category, scfg = row
    model_intent = ''
    try:
        data = json.loads(scfg) if isinstance(scfg, str) else {}
        m = data.get('model')
        if isinstance(m, str):
            model_intent = m.strip()
    except Exception:
        pass
    return {'name': name or '', 'category': category or '', 'model': model_intent}


def provider_from_ccswitch(cc: dict) -> str:
    """把 cc-switch 激活 provider 映射成 provider 描述（用其权威『选择』而非装饰标签）。"""
    name = cc.get('name', '')
    category = cc.get('category', '')
    low = name.lower()
    if category == 'official' and 'claude' in low:
        return 'Anthropic（官方 Claude Code · cc-switch active）'
    if category == 'official':
        return f'{name}（官方 · cc-switch active）'
    # cn_official / third_party：对 provider 名（权威选择）做 hint 匹配
    for hint, label in PROVIDER_HINTS.items():
        if hint in low:
            return f'{label}（cc-switch active: {name}）'
    return f'{name}（cc-switch active · category={category}）'


def ccswitch_identity(cc: dict) -> str:
    """由 cc-switch 激活 provider 推导可写入 frontmatter 的诚实模型标识（如 claude-opus）。"""
    name = cc.get('name', '')
    category = cc.get('category', '')
    mid = (cc.get('model') or '').strip().lower()
    low = name.lower()
    if category == 'official' and 'claude' in low:
        fam = 'claude'
    elif category == 'official' and ('openai' in low or 'gpt' in low or 'codex' in low):
        fam = 'gpt'
    elif category == 'official' and 'gemini' in low:
        fam = 'gemini'
    else:
        fam = re.sub(r'[^a-z0-9]+', '-', low).strip('-') or 'unknown'
    if mid and mid not in fam:
        return f'{fam}-{mid}'
    return fam


def detect_provider(base_url: str, model: str, ccswitch: Optional[dict] = None) -> str:
    """识别 provider，按可信度排序：显式 BASE_URL > cc-switch 激活源 > 默认 Anthropic。

    重要：不再用 MODEL 名字符串子串猜 provider——model 字段可能是与真实后端解耦的
    装饰标签（如 cc-switch 通用配置写死的别名），按名猜测会产生假阳性。
    """
    # 1) 显式 BASE_URL：字节实际流向，最强信号
    if base_url:
        base_lower = base_url.lower()
        for hint, name in PROVIDER_HINTS.items():
            if hint in base_lower:
                return name
        return f'未知（请人工识别 BASE_URL: {base_url}）'

    # 2) cc-switch 激活 provider：用户实际选择的后端（权威）
    if ccswitch:
        return provider_from_ccswitch(ccswitch)

    # 3) 兜底：默认 Anthropic（不再按 MODEL 名猜测）
    if model:
        return 'Anthropic（默认直连；未检测到 cc-switch/显式 BASE_URL，MODEL 仅为标签）'
    return 'Anthropic（默认直连，BASE_URL 与 MODEL 均未设）'


# ============================================================
# opencode 框架支持
# ============================================================

# opencode 在用户主目录下的状态/配置/共享目录（XDG 风格，Windows 上 opencode 仍用此布局）
OPENCODE_STATE_DIR = Path.home() / '.local' / 'state' / 'opencode'
OPENCODE_CONFIG_DIR = Path.home() / '.config' / 'opencode'
OPENCODE_SHARE_DIR = Path.home() / '.local' / 'share' / 'opencode'

# opencode 内置 + 自定义 provider ID → 人类可读名映射
# 未命中时调用方应用原始 ID + "(未映射)" 标注，便于发现新 provider 待收录
OPENCODE_PROVIDER_LABELS: dict[str, str] = {
    'zhipuai-coding-plan': 'Zhipu / GLM（coding plan）',
    'zhipuai': 'Zhipu / GLM',
    'kimi-for-coding': 'Moonshot / Kimi（coding）',
    'moonshotai-cn': 'Moonshot / Kimi',
    'minimax-cn-coding-plan': 'MiniMax（coding plan）',
    'minimax-cn': 'MiniMax',
    'deepseek': 'DeepSeek',
    'openai': 'OpenAI',
    'anthropic': 'Anthropic',
    'alibaba-cn': 'Alibaba / Qwen',
    'xiaomi': 'Xiaomi / MiMo',
    'openrouter': 'OpenRouter',
    'zai-coding-plan': 'Zhipu / GLM（ZAI coding plan）',
    'zai': 'Zhipu / GLM（ZAI）',
}


def detect_harness() -> str:
    """识别当前运行的 harness 框架。

    优先级（强 → 弱）：
      1. CODEX_THREAD_ID env → 'codex'（Codex 当前线程专有运行时信号）
      2. CLAUDECODE=1 env → 'claude-code'（Claude Code 运行时专有信号）
      3. OPENCODE_BIN_PATH env 存在 → 'opencode'
      4. cc-switch.db 存在 或 ~/.claude/settings.json 存在 → 'claude-code'（弱信号）
      5. 兜底 → 'unknown'

    设计：CODEX_THREAD_ID / CLAUDECODE=1 是当前 harness 的专有运行时信号，优先于
    可能从父 shell 泄漏的 OPENCODE_BIN_PATH。ANTHROPIC_* 环境变量不作为 harness
    检测信号——它们可能从 shell profile /.env 泄漏到 opencode 子进程，造成假阳性。
    opencode 命中后**独占**——一旦判定为 opencode，main() 不再读 cc-switch /
    .claude/settings.json / ANTHROPIC_* env。
    """
    # Codex 当前线程专有信号；优先于可能残留的 OPENCODE_BIN_PATH。
    if os.environ.get('CODEX_THREAD_ID', '').strip():
        return 'codex'
    # Claude Code 专有运行时信号
    if os.environ.get('CLAUDECODE', '').strip() == '1':
        return 'claude-code'
    if os.environ.get('OPENCODE_BIN_PATH', '').strip():
        return 'opencode'
    home = Path.home()
    if (home / '.cc-switch' / 'cc-switch.db').exists():
        return 'claude-code'
    if (home / '.claude' / 'settings.json').exists():
        return 'claude-code'
    return 'unknown'


def _normalize_dir(p: str) -> str:
    """把目录路径归一化为可比较的形式（POSIX + 小写）。

    背景问题（S2）：session.directory 列存储正斜杠 POSIX 路径（如
    `C:/path/to/vault`），而 Windows 上 `os.getcwd()` 返回反斜杠
    （`C:\\path\\to\\vault`）。直接字符串比较会失配，导致正向定位漏判。

    采用字符串级归一化（非 `Path.resolve`）以保持纯函数性质——无文件系统访问、
    无符号链接解析、确定性。Windows 路径大小写不敏感，故 lower。
    """
    if not p:
        return ''
    return p.replace('\\', '/').lower()


def select_session_model(
    rows: list[dict],
    cwd: str,
    now_ms: int,
    window_ms: int = 120000,
) -> tuple[Optional[str], bool, str]:
    """从 session 表的原始行中选择"调用者本会话"的 model。

    **纯函数**——无 IO，确定性，便于单测。

    检测阶梯（honest best-effort，**非正向鉴定**）：

      1. directory 过滤：保留 directory == cwd 的行（跨目录并发隔离——
         这是**可靠正向定位的部分**，消除跨项目并发污染）。
      2. 活跃窗口筛选：在同目录行中保留 `now_ms - time_updated <= window_ms`
         的"近期活跃"行。`window_ms` 默认 120000（2min），偏向"宁可过宽误降级
         也不漏检调用者"——过宽只会多降级（安全方向），过窄才会误判。
      3. 判定：
         - 恰好 **1** 行近期活跃 → **正向定位**：返回该行 model，
           `degraded=False`（单会话常态 + 跨目录并发下调用者是同目录唯一活跃
           会话均落此分支）。
         - **≥2** 行近期活跃（同目录并发征兆）→ **无法正向定位**：返回
           `degraded=True` + 最新行 model 作 best-guess +
           reason=`"same-dir concurrency"`。
         - **0** 行近期活跃（whoami 在会话久闲后运行）→ 返回同目录最新行
           model + `degraded=True` + reason=`"stale, no active session in window"`。
      4. 无同目录行（cwd 不匹配任何 session）→ 返回 `degraded=True` + 全局最新行
         best-guess + reason=`"no session in cwd"`；连全局都为空则返回
         `(None, True, "no session in cwd")`。

    **诚实边界（best-effort 正向定位 + 稳健降级）**：

    本函数的可靠价值是**消除跨目录并发污染**。对**同目录并发**——当本机 vault
    是用户主工作目录时属常态而非低概率边缘——directory 信号无法消歧，本函数
    只能**检测到并诚实告警降级**，**不是**正向解出调用者本会话。彻底正向定位
    同目录并发需上游暴露 session id（如 `OPENCODE_SESSION_ID` env），本地兜底
    无法替代。

    Args:
      rows: session 表原始行字典列表，键含 {id, directory, time_updated, model}。
            `model` 字段被透传（opaque），由调用方解析。
      cwd: 调用者工作目录（与各行 directory 比较前归一化）。
      now_ms: 当前时间（毫秒），由调用方传入以保持函数纯度。
      window_ms: 活跃窗口宽度（毫秒，默认 2 分钟）。

    Returns:
      `(model | None, degraded: bool, reason: str)`。
      `model` 是被选中行的 `model` 字段原值；调用方负责 JSON 解析。
    """
    cwd_n = _normalize_dir(cwd)
    # 1. directory 过滤
    same_dir = [r for r in rows if _normalize_dir(r.get('directory', '')) == cwd_n]
    if not same_dir:
        # 4. 无同目录行：回退全局最新行；连全局也空则返回 None
        if rows:
            glob_latest = max(rows, key=lambda r: r.get('time_updated', 0))
            return glob_latest.get('model'), True, 'no session in cwd'
        return None, True, 'no session in cwd'
    # 2. 活跃窗口筛选
    active = [r for r in same_dir if (now_ms - r.get('time_updated', 0)) <= window_ms]
    # 3. 判定
    if len(active) == 1:
        return active[0].get('model'), False, ''
    if len(active) >= 2:
        latest = max(active, key=lambda r: r.get('time_updated', 0))
        return latest.get('model'), True, 'same-dir concurrency'
    # 0 行近期活跃
    latest_same = max(same_dir, key=lambda r: r.get('time_updated', 0))
    return latest_same.get('model'), True, 'stale, no active session in window'


def read_opencode_model_from_db() -> Optional[dict]:
    """从 opencode.db session 表读取当前会话实际使用的模型（权威源）。

    数据源：~/.local/share/opencode/opencode.db 的 session 表。

    定位逻辑（修复 2026-07-05 并发竞态）：原实现取 `ORDER BY time_updated DESC
    LIMIT 1`——即**全局最新行**。多 opencode 会话并发时，任一会话的 time_updated
    更新都会抢占"最新行"，导致跨目录/同目录并发下抓到错误会话。

    现实现把读到的所有相关行（id, directory, time_updated, model）喂给纯函数
    `select_session_model(rows, cwd, now_ms, window_ms)`，由它按 directory 过滤 +
    活跃窗口 + 并发检测的阶梯选定调用者本会话的 model，并给出 degraded/reason
    信号。详见 `select_session_model` docstring 的诚实边界声明——可靠价值是
    **消除跨目录并发污染**；同目录并发只能 best-effort 检测并降级告警，**不是**
    正向解出调用者本会话。

    返回字典结构：
      {'provider_id': str, 'model_id': str,
       'degraded': bool, 'reason': str}
    `degraded=True` 时 `reason` 标注降级原因（"same-dir concurrency" /
    "stale, no active session in window" / "no session in cwd"）。

    为什么这是权威源：session.model 是 opencode 运行时为每个会话记录的实际模型，
    随会话切换实时更新。相比之下，model.json 的 recent 数组是去重的导航历史，
    切回已存在模型时不会重排到 [0]，导致 recent[0] ≠ 当前模型（2026-06-29 实测误报）。

    DB 缺失/加锁/schema 不符/无 session 行时静默返回 None。
    """
    db_path = OPENCODE_SHARE_DIR / 'opencode.db'
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=1.0)
        try:
            rows_raw = con.execute(
                'SELECT id, directory, time_updated, model FROM session '
                'WHERE model IS NOT NULL AND model != "" '
                'ORDER BY time_updated DESC'
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return None
    if not rows_raw:
        return None
    rows = [
        {'id': r[0], 'directory': r[1] or '', 'time_updated': r[2] or 0, 'model': r[3]}
        for r in rows_raw
    ]
    model_str, degraded, reason = select_session_model(
        rows, os.getcwd(), int(time.time() * 1000)
    )
    if not model_str:
        return None
    try:
        data = json.loads(model_str)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # session.model 的 JSON 用 "id"（非 "modelID"）和 "providerID"
    pid = str(data.get('providerID', '')).strip()
    mid = str(data.get('id', '')).strip()
    if not pid or not mid:
        return None
    return {
        'provider_id': pid,
        'model_id': mid,
        'degraded': degraded,
        'reason': reason,
    }


def read_opencode_model_state() -> Optional[dict]:
    """读取 opencode 当前选中模型。

    数据源优先级：
      1. opencode.db session 表最新行的 model 列（权威——随会话切换实时更新）
      2. ~/.local/state/opencode/model.json 的 recent[0]（兜底——去重历史，可能过时）

    返回 {'provider_id': str, 'model_id': str} 或 None。

    注意：model.json 的 recent[0] 曾是唯一数据源，但实测发现它是去重导航历史，
    切回已存在模型时不重排，导致 recent[0] ≠ 当前模型。已降级为兜底。
    """
    # 权威源：opencode.db（含 directory 过滤 + 并发检测的 degraded 信号）
    state = read_opencode_model_from_db()
    if state:
        return state

    # 兜底：model.json recent[0]（旧逻辑，DB 不可用时使用）
    # 已知可靠性缺口：recent 是去重导航历史，切回已存在模型时不重排到 [0]，
    # 可能 ≠ 当前模型——标记 degraded=True 诚实告警。
    path = OPENCODE_STATE_DIR / 'model.json'
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    recent = data.get('recent')
    if not isinstance(recent, list) or not recent:
        return None
    first = recent[0]
    if not isinstance(first, dict):
        return None
    pid = str(first.get('providerID', '')).strip()
    mid = str(first.get('modelID', '')).strip()
    if not pid or not mid:
        return None
    return {
        'provider_id': pid,
        'model_id': mid,
        'degraded': True,
        'reason': 'model.json fallback (DB unavailable; recent[0] may be stale)',
    }


def read_opencode_provider_config() -> dict:
    """读取 ~/.config/opencode/opencode.json 的 provider 段（白名单模式）。

    返回 {provider_id: {'baseURL': str, 'name': str, 'models': {model_id: name}}}
    仅解析用户自定义 provider；内置 provider（不在 config 中）返回空。

    安全（D4 硬约束）：白名单模式——只访问 options.baseURL / 顶层 name /
    models.<id>.name；options 下的 apiKey 等敏感字段绝不读取。
    """
    path = OPENCODE_CONFIG_DIR / 'opencode.json'
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    providers = data.get('provider')
    if not isinstance(providers, dict):
        return {}
    result: dict[str, dict] = {}
    for pid, pinfo in providers.items():
        if not isinstance(pinfo, dict):
            continue
        entry: dict = {}
        # 白名单字段 1：顶层 name
        name = pinfo.get('name')
        if isinstance(name, str) and name.strip():
            entry['name'] = name.strip()
        # 白名单字段 2：models.<id>.name
        models_in = pinfo.get('models')
        models_out: dict[str, str] = {}
        if isinstance(models_in, dict):
            for mid, minfo in models_in.items():
                if isinstance(minfo, dict):
                    mname = minfo.get('name')
                    if isinstance(mname, str) and mname.strip():
                        models_out[mid] = mname.strip()
        if models_out:
            entry['models'] = models_out
        # 白名单字段 3：options.baseURL（绝不取 options.apiKey）
        opts = pinfo.get('options')
        if isinstance(opts, dict):
            bu = opts.get('baseURL')
            if isinstance(bu, str) and bu.strip():
                entry['baseURL'] = bu.strip()
        if entry:
            result[pid] = entry
    return result


def detect_provider_opencode(state: dict, cfg: dict) -> tuple[str, str]:
    """返回 (provider_description, base_url)。

    provider_description 来源：OPENCODE_PROVIDER_LABELS > 原始 ID + (未映射)
    base_url 来源：opencode.json provider.options.baseURL（自定义 provider）；
                 内置 provider 返回 ''（不在 opencode.json 中声明）
    """
    pid = (state.get('provider_id') or '').strip()
    base_url = ''
    if pid and pid in cfg:
        base_url = cfg[pid].get('baseURL', '')
    if pid in OPENCODE_PROVIDER_LABELS:
        desc = OPENCODE_PROVIDER_LABELS[pid]
    else:
        desc = f'{pid}（未映射）'
    return desc, base_url


def settings_paths_low_to_high() -> list[Path]:
    """settings.json 候选路径，按优先级从低到高排列。

    Claude Code 实际加载顺序（高优先级覆盖低优先级）：
      项目 .local > 项目 > 用户 .local > 用户
    本函数返回反向（低 → 高），方便后续按顺序写入 dict 让高优先级覆盖。
    """
    home = Path.home()
    return [
        home / '.claude' / 'settings.json',
        home / '.claude' / 'settings.local.json',
        Path('.claude/settings.json'),
        Path('.claude/settings.local.json'),
    ]


def resolve_effective_config() -> dict[str, FieldSource]:
    """合并 env + 多层 settings.json，按优先级返回每字段的最终值与来源。

    优先级（高 → 低）：
      process env
      > 项目 .claude/settings.local.json
      > 项目 .claude/settings.json
      > 用户 ~/.claude/settings.local.json
      > 用户 ~/.claude/settings.json
    """
    result: dict[str, FieldSource] = {key: FieldSource('', '') for key in ENV_KEYS}

    # 1) settings 从低到高累积，让高优先级覆盖低优先级
    for path in settings_paths_low_to_high():
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        # 顶层 "model" 字段映射到 model
        top_model = data.get('model')
        if isinstance(top_model, str) and top_model.strip():
            result['model'] = FieldSource(top_model.strip(), f'settings:{path}')
        # env section 里的 ANTHROPIC_*
        env_section = data.get('env')
        if isinstance(env_section, dict):
            for key, env_var in ENV_KEYS.items():
                raw = env_section.get(env_var)
                if isinstance(raw, str) and raw.strip():
                    result[key] = FieldSource(raw.strip(), f'settings:{path}')

    # 2) process env 最高优先级
    for key, env_var in ENV_KEYS.items():
        val = os.environ.get(env_var, '').strip()
        if val:
            result[key] = FieldSource(val, 'env')

    return result


def settings_overrides_listing() -> list[tuple[Path, str]]:
    """列出所有 settings.json 里出现的 model 相关字段（诊断段，不参与决策）。"""
    overrides: list[tuple[Path, str]] = []
    seen_paths: set[Path] = set()
    for path in settings_paths_low_to_high():
        if not path.exists() or path in seen_paths:
            continue
        seen_paths.add(path)
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if 'model' in data:
            overrides.append((path, f'model={data["model"]}'))
        env_section = data.get('env')
        if isinstance(env_section, dict):
            for env_var in ENV_KEYS.values():
                if env_var in env_section:
                    overrides.append((path, f'env.{env_var}={env_section[env_var]}'))
    return overrides


def fmt(field: FieldSource, fallback: str) -> str:
    """把字段格式化成 'value' 或 'value（来自 settings 文件）'。"""
    if not field.value:
        return fallback
    if field.origin == 'env':
        return field.value
    if field.origin.startswith('settings:'):
        return f'{field.value}（来自 {field.origin[len("settings:"):]}）'
    return field.value


def output_frontmatter(
    cfg: Optional[dict[str, FieldSource]] = None,
    ccswitch: Optional[dict] = None,
    opencode_state: Optional[dict] = None,
) -> None:
    """输出可追加到 agent 创建笔记 frontmatter 的 YAML 片段。

    输出二至三行（model / generated_at / [host]），不含开闭 ---。
    Agent 可直接追加到已有 frontmatter 块末尾。

    model 取值优先级（按 harness 路由结果）：
      - opencode_state 存在 → `{provider_id}/{model_id}`（诚实，与 opencode 内部 ID 一致）
      - cc-switch 激活 provider 推导值（诚实，随切换更新）
      - settings 的 model 标签（可能与真实后端解耦）
      - 'unknown'

    主机输出 model + generated_at；从机额外输出 host（host 字段存在 = 从机创作）。
    model 字段存在即标识 agent 创作（人写笔记不应设 model 字段）。

    降级语义（来自 opencode 路径的并发/陈旧检测）：当 opencode_state['degraded']
    为 True 时追加 `confidence: low`，标识 model 可能受同目录并发/陈旧会话污染。
    非降级分支输出不变（无 confidence 行，与单会话旧行为一致）。
    """
    from datetime import datetime, timezone
    degraded = False
    if opencode_state:
        pid = (opencode_state.get('provider_id') or '').strip()
        mid = (opencode_state.get('model_id') or '').strip()
        model = f'{pid}/{mid}' if pid and mid else 'unknown'
        degraded = bool(opencode_state.get('degraded', False))
    else:
        model_field = (cfg or {}).get('model', FieldSource('unknown', ''))
        base_url = (cfg or {}).get('base_url', FieldSource('', '')).value
        # BASE_URL 未显式设置时，cc-switch 激活源比 settings 的 model 标签更可信
        if ccswitch and not base_url:
            model = ccswitch_identity(ccswitch)
        else:
            model = model_field.value or 'unknown'
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')

    # 主从机判断：从机额外输出 host
    try:
        import subprocess
        actual_host = subprocess.check_output(['hostname'], text=True).strip()
    except Exception:
        actual_host = 'unknown'
    primary_host = os.environ.get('PRIMARY_HOST', '').strip()

    if primary_host and actual_host != primary_host:
        print(f'host: {actual_host}')
    print(f'model: {model}')
    if degraded:
        print(f'confidence: low')
    print(f'generated_at: {now}')


def main() -> int:
    harness = detect_harness()

    # --frontmatter 模式：输出 agent 溯源 YAML 片段
    if '--frontmatter' in sys.argv:
        if harness == 'codex':
            output_frontmatter({'model': FieldSource('openai/codex', 'runtime')})
        elif harness == 'opencode':
            output_frontmatter(opencode_state=read_opencode_model_state())
        else:
            cfg = resolve_effective_config()
            output_frontmatter(cfg, read_ccswitch_active('claude'))
        return 0

    print('=== 模型自检 ===')
    print(f'Harness:  {harness}')

    if harness == 'codex':
        print('Provider: OpenAI / Codex')
        print('BASE_URL: (由 Codex 宿主管理；当前进程未暴露)')
        print('MODEL:    openai/codex（稳定 harness 身份；精确模型 ID 未暴露）')
        print()
        print('提示：')
        print('  - Harness 判定依据：CODEX_THREAD_ID env')
        print('  - 不读取 opencode.db / cc-switch.db / .claude settings 猜测 Codex 模型')
        return 0

    # opencode 分支：不读 ANTHROPIC_* env / .claude / cc-switch（避免 Claude Code 残留假阳性）
    if harness == 'opencode':
        state = read_opencode_model_state()
        if not state:
            print('Provider: (无法读取 opencode 状态；opencode.db session 表和 model.json 均不可用)')
            print()
            print('提示：')
            print('  - Harness 判定依据：OPENCODE_BIN_PATH env')
            print('  - 模型来源：opencode.db session 表（权威）→ model.json recent[0]（兜底）')
            print('  - 不读 ANTHROPIC_* env / .claude/settings.json / cc-switch.db')
            return 0
        opencode_cfg = read_opencode_provider_config()
        provider_desc, base_url = detect_provider_opencode(state, opencode_cfg)
        pid = state['provider_id']
        mid = state['model_id']
        degraded = bool(state.get('degraded', False))
        reason = state.get('reason', '')
        print(f'Provider: {provider_desc}')
        print(f'  └ opencode session.model: providerID={pid}, modelID={mid}')
        print(f'BASE_URL: {base_url or "(内置 provider；未在 opencode.json 中显式声明)"}')
        print(f'MODEL:    {pid}/{mid}')
        if degraded:
            print(f'  ⚠ 并发/陈旧会话检测：model 可受同目录并发污染（reason={reason}）；'
                  f'请核验 session 表或在单一会话时复测')
        if opencode_cfg:
            print('opencode.json 自定义 provider（诊断用）：')
            for pid_key, info in sorted(opencode_cfg.items()):
                extras = []
                if info.get('baseURL'):
                    extras.append(f'baseURL={info["baseURL"]}')
                if info.get('models'):
                    extras.append(f'models={list(info["models"].keys())}')
                print(f'  - {pid_key}: ' + (', '.join(extras) if extras else '(无模型/baseURL)'))
        print()
        print('提示：')
        print('  - Harness 判定依据：OPENCODE_BIN_PATH env')
        print('  - 模型来源：opencode.db session 表最新行（权威，随会话切换实时更新）')
        print('  - 不读 ANTHROPIC_* env / .claude/settings.json / cc-switch.db（避免 Claude Code 残留假阳性）')
        print('  - 内置 provider（zhipuai-coding-plan 等）不在 opencode.json 中，仅 auth.json 有凭据')
        return 0

    # === Claude Code 分支（原逻辑，保持不变） ===
    cfg = resolve_effective_config()

    base_url = cfg['base_url'].value
    main_model = cfg['model'].value
    ccswitch = read_ccswitch_active('claude')

    has_api_key = bool(os.environ.get('ANTHROPIC_API_KEY', '').strip())
    has_auth_token = bool(os.environ.get('ANTHROPIC_AUTH_TOKEN', '').strip())

    print(f'Provider: {detect_provider(base_url, main_model, ccswitch)}')
    if ccswitch:
        extra = f', model={ccswitch["model"]}' if ccswitch['model'] else ''
        print(f'  └ cc-switch active (app=claude): {ccswitch["name"]} '
              f'[category={ccswitch["category"]}{extra}]')
    print(f'BASE_URL: {fmt(cfg["base_url"], "(unset → 默认 Anthropic)")}')
    model_note = ('  ⚠️ 仅为标签，权威后端以 cc-switch active 为准'
                  if (ccswitch and not base_url and main_model) else '')
    print(f'MODEL:    {fmt(cfg["model"], "(unset → harness 默认)")}{model_note}')

    sonnet = cfg['sonnet'].value
    opus = cfg['opus'].value
    haiku = cfg['haiku'].value

    # 三档全设且一致 → 显示统一行；否则按差异列出
    all_set = bool(sonnet and opus and haiku)
    all_same = all_set and sonnet == opus == haiku

    if all_same:
        unified = sonnet
        if main_model and unified == main_model:
            print(f'层级别名: Sonnet / Opus / Haiku 全部 → {unified}（统一后端）')
        elif main_model:
            print(f'层级别名: Sonnet / Opus / Haiku 全部 → {unified}（与 MODEL={main_model} 不同）')
        else:
            print(f'层级别名: Sonnet / Opus / Haiku 全部 → {unified}（统一后端，MODEL 未显式设）')
    else:
        diffs: list[str] = []
        if sonnet and sonnet != main_model:
            diffs.append(f'Sonnet → {sonnet}')
        if opus and opus != main_model:
            diffs.append(f'Opus → {opus}')
        if haiku and haiku != main_model:
            diffs.append(f'Haiku → {haiku}')
        if diffs:
            print(f'层级别名: {"; ".join(diffs)}')

    print(f'Auth:     api_key={"set" if has_api_key else "unset"}, '
          f'auth_token={"set" if has_auth_token else "unset"}')

    overrides = settings_overrides_listing()
    if overrides:
        print('settings 文件中的覆盖项（诊断用）：')
        for path, note in overrides:
            print(f'  - {path}: {note}')

    print()
    print('提示：')
    print('  - Provider 权威性：显式 BASE_URL > cc-switch 激活源 > 默认 Anthropic')
    print('  - MODEL 字段是 settings 里的标签，可能被 cc-switch 通用配置写死、与真实后端解耦')
    print('  - harness 注入的 commit Co-Authored-By 模板 ≠ 真实模型')
    print('  - 上述 Provider（及 cc-switch active 行）才是回答你的真实后端')
    return 0


if __name__ == '__main__':
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from common import log_script_run
    log_script_run()
    raise SystemExit(main())
