#!/usr/bin/env python3
"""模型自检：识别当前 Agent 实际调用的 harness、provider 与 model。

用法：
    python .meta/scripts/whoami.py                  # 自检（人读）
    python .meta/scripts/whoami.py --frontmatter    # 输出 agent 溯源 YAML（agent/model/generated_at）

两层识别（本地只读，无网络/token/写入，绝不读取 token/key）：
  1. Harness 路由：CODEX_THREAD_ID > CLAUDECODE=1 > OPENCODE_BIN_PATH > ~/.cc-switch|~/.claude > unknown
     （opencode 命中后独占，不读 Claude Code 残留状态）
  2. Provider 识别：
     - codex：固定报告 openai/codex
     - opencode：opencode.db session 表（权威，按 cwd+活跃窗口防跨目录并发）> model.json recent[0]（降级兜底）
     - claude-code：显式 BASE_URL > cc-switch 激活 provider（CC_SWITCH_DB 可覆盖）> 默认 Anthropic
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import socket
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

ENV_KEYS: dict[str, str] = {
    'base_url': 'ANTHROPIC_BASE_URL',
    'model': 'ANTHROPIC_MODEL',
    'sonnet': 'ANTHROPIC_DEFAULT_SONNET_MODEL',
    'opus': 'ANTHROPIC_DEFAULT_OPUS_MODEL',
    'haiku': 'ANTHROPIC_DEFAULT_HAIKU_MODEL',
}

OPENCODE_STATE_DIR = Path.home() / '.local' / 'state' / 'opencode'
OPENCODE_CONFIG_DIR = Path.home() / '.config' / 'opencode'
OPENCODE_SHARE_DIR = Path.home() / '.local' / 'share' / 'opencode'

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


class FieldSource(NamedTuple):
    """字段最终值与来源标记：'env' / 'settings:<path>' / ''。"""
    value: str
    origin: str


def _read_json_dict(path: Path) -> Optional[dict]:
    """静默读取 JSON 对象文件，不存在、损坏或非 dict 返回 None。"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _query_sqlite_ro(db_path: Path, sql: str, params: tuple = (), fetchall: bool = False):
    """只读模式查询 SQLite，缺失/加锁/schema 不符静默返回 None。"""
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=1.0)
        try:
            cur = con.execute(sql, params)
            return cur.fetchall() if fetchall else cur.fetchone()
        finally:
            con.close()
    except Exception:
        return None


def read_ccswitch_active(app_type: str = 'claude') -> Optional[dict]:
    """只读 cc-switch 当前激活 provider（仅 name/category/model，不读凭据）。"""
    db_path = Path(
        os.environ.get('CC_SWITCH_DB', '').strip()
        or str(Path.home() / '.cc-switch' / 'cc-switch.db')
    )
    row = _query_sqlite_ro(
        db_path,
        'SELECT name, category, settings_config FROM providers '
        'WHERE is_current=1 AND app_type=? LIMIT 1',
        (app_type,),
    )
    if not row:
        return None
    name, category, scfg = row
    model_intent = ''
    try:
        data = json.loads(scfg) if isinstance(scfg, str) else {}
        if isinstance(data, dict) and isinstance(data.get('model'), str):
            model_intent = data['model'].strip()
    except Exception:
        pass
    return {'name': name or '', 'category': category or '', 'model': model_intent}


def provider_from_ccswitch(cc: dict) -> str:
    """将 cc-switch 激活 provider 映射为人类可读描述。"""
    name = cc.get('name', '')
    category = cc.get('category', '')
    low = name.lower()
    if category == 'official' and 'claude' in low:
        return 'Anthropic（官方 Claude Code · cc-switch active）'
    if category == 'official':
        return f'{name}（官方 · cc-switch active）'
    for hint, label in PROVIDER_HINTS.items():
        if hint in low:
            return f'{label}（cc-switch active: {name}）'
    return f'{name}（cc-switch active · category={category}）'


def ccswitch_identity(cc: dict) -> str:
    """由 cc-switch 激活 provider 推导可写入 frontmatter 的诚实模型标识。"""
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
    return f'{fam}-{mid}' if (mid and mid not in fam) else fam


def detect_provider(base_url: str, model: str, ccswitch: Optional[dict] = None) -> str:
    """按可信度识别 provider：显式 BASE_URL > cc-switch 激活源 > 默认 Anthropic。"""
    if base_url:
        base_lower = base_url.lower()
        for hint, name in PROVIDER_HINTS.items():
            if hint in base_lower:
                return name
        return f'未知（请人工识别 BASE_URL: {base_url}）'
    if ccswitch:
        return provider_from_ccswitch(ccswitch)
    if model:
        return 'Anthropic（默认直连；未检测到 cc-switch/显式 BASE_URL，MODEL 仅为标签）'
    return 'Anthropic（默认直连，BASE_URL 与 MODEL 均未设）'


def detect_harness() -> str:
    """按优先级识别当前 harness：CODEX_THREAD_ID > CLAUDECODE=1 > OPENCODE_BIN_PATH > 本地文件信号 > unknown。"""
    if os.environ.get('CODEX_THREAD_ID', '').strip():
        return 'codex'
    if os.environ.get('CLAUDECODE', '').strip() == '1':
        return 'claude-code'
    if os.environ.get('OPENCODE_BIN_PATH', '').strip():
        return 'opencode'
    home = Path.home()
    if (home / '.cc-switch' / 'cc-switch.db').exists() or (home / '.claude' / 'settings.json').exists():
        return 'claude-code'
    return 'unknown'


def _normalize_dir(p: str) -> str:
    """纯函数路径归一化（POSIX 斜杠 + 小写），避免 Windows 反斜杠与大小写失配。"""
    return p.replace('\\', '/').lower() if p else ''


def select_session_model(
    rows: list[dict],
    cwd: str,
    now_ms: int,
    window_ms: int = 120000,
) -> tuple[Optional[str], bool, str]:
    """从 opencode session 行中选择当前会话的 model，返回 (model, degraded, reason)。

    消除跨目录并发污染；同目录并发/无近期活跃/无同目录行均标记 degraded=True。
    """
    cwd_n = _normalize_dir(cwd)
    same_dir = [r for r in rows if _normalize_dir(r.get('directory', '')) == cwd_n]
    if not same_dir:
        if rows:
            glob_latest = max(rows, key=lambda r: r.get('time_updated', 0))
            return glob_latest.get('model'), True, 'no session in cwd'
        return None, True, 'no session in cwd'

    active = [r for r in same_dir if (now_ms - r.get('time_updated', 0)) <= window_ms]
    if len(active) == 1:
        return active[0].get('model'), False, ''
    if len(active) >= 2:
        latest = max(active, key=lambda r: r.get('time_updated', 0))
        return latest.get('model'), True, 'same-dir concurrency'
    latest_same = max(same_dir, key=lambda r: r.get('time_updated', 0))
    return latest_same.get('model'), True, 'stale, no active session in window'


def read_opencode_model_from_db() -> Optional[dict]:
    """从 opencode.db session 表读取当前会话模型（权威源）。"""
    rows_raw = _query_sqlite_ro(
        OPENCODE_SHARE_DIR / 'opencode.db',
        'SELECT id, directory, time_updated, model FROM session '
        'WHERE model IS NOT NULL AND model != "" '
        'ORDER BY time_updated DESC',
        fetchall=True,
    )
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
    pid = str(data.get('providerID', '')).strip()
    mid = str(data.get('id', '')).strip()
    if not pid or not mid:
        return None
    return {'provider_id': pid, 'model_id': mid, 'degraded': degraded, 'reason': reason}


def read_opencode_model_state() -> Optional[dict]:
    """读取 opencode 当前模型：优先 opencode.db，缺失时降级读 model.json recent[0]。"""
    state = read_opencode_model_from_db()
    if state:
        return state
    data = _read_json_dict(OPENCODE_STATE_DIR / 'model.json')
    recent = data.get('recent') if data else None
    if not isinstance(recent, list) or not recent or not isinstance(recent[0], dict):
        return None
    pid = str(recent[0].get('providerID', '')).strip()
    mid = str(recent[0].get('modelID', '')).strip()
    if not pid or not mid:
        return None
    return {
        'provider_id': pid,
        'model_id': mid,
        'degraded': True,
        'reason': 'model.json fallback (DB unavailable; recent[0] may be stale)',
    }


def read_opencode_provider_config() -> dict:
    """白名单读取 ~/.config/opencode/opencode.json 的 provider 段（仅 name/models.*.name/options.baseURL）。"""
    data = _read_json_dict(OPENCODE_CONFIG_DIR / 'opencode.json')
    providers = data.get('provider') if data else None
    if not isinstance(providers, dict):
        return {}
    result: dict[str, dict] = {}
    for pid, pinfo in providers.items():
        if not isinstance(pinfo, dict):
            continue
        entry: dict = {}
        name = pinfo.get('name')
        if isinstance(name, str) and name.strip():
            entry['name'] = name.strip()
        models_in = pinfo.get('models')
        if isinstance(models_in, dict):
            models_out = {
                mid: minfo['name'].strip()
                for mid, minfo in models_in.items()
                if isinstance(minfo, dict) and isinstance(minfo.get('name'), str) and minfo['name'].strip()
            }
            if models_out:
                entry['models'] = models_out
        opts = pinfo.get('options')
        if isinstance(opts, dict) and isinstance(opts.get('baseURL'), str) and opts['baseURL'].strip():
            entry['baseURL'] = opts['baseURL'].strip()
        if entry:
            result[pid] = entry
    return result


def detect_provider_opencode(state: dict, cfg: dict) -> tuple[str, str]:
    """返回 opencode 的 (provider_description, base_url)。"""
    pid = (state.get('provider_id') or '').strip()
    base_url = cfg[pid].get('baseURL', '') if pid and pid in cfg else ''
    desc = OPENCODE_PROVIDER_LABELS.get(pid, f'{pid}（未映射）')
    return desc, base_url


def settings_paths_low_to_high() -> list[Path]:
    """Claude Code settings.json 候选路径，按优先级从低到高排列。"""
    home = Path.home()
    return [
        home / '.claude' / 'settings.json',
        home / '.claude' / 'settings.local.json',
        Path('.claude/settings.json'),
        Path('.claude/settings.local.json'),
    ]


def resolve_effective_config() -> dict[str, FieldSource]:
    """合并多层 settings.json 与 process env（env 优先级最高）。"""
    result: dict[str, FieldSource] = {key: FieldSource('', '') for key in ENV_KEYS}
    for path in settings_paths_low_to_high():
        data = _read_json_dict(path)
        if not data:
            continue
        top_model = data.get('model')
        if isinstance(top_model, str) and top_model.strip():
            result['model'] = FieldSource(top_model.strip(), f'settings:{path}')
        env_section = data.get('env')
        if isinstance(env_section, dict):
            for key, env_var in ENV_KEYS.items():
                raw = env_section.get(env_var)
                if isinstance(raw, str) and raw.strip():
                    result[key] = FieldSource(raw.strip(), f'settings:{path}')
    for key, env_var in ENV_KEYS.items():
        val = os.environ.get(env_var, '').strip()
        if val:
            result[key] = FieldSource(val, 'env')
    return result


def settings_overrides_listing() -> list[tuple[Path, str]]:
    """列出所有 settings.json 中的 model/ANTHROPIC_* 字段（仅诊断展示）。"""
    overrides: list[tuple[Path, str]] = []
    seen_paths: set[Path] = set()
    for path in settings_paths_low_to_high():
        if path in seen_paths:
            continue
        seen_paths.add(path)
        data = _read_json_dict(path)
        if not data:
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
    """格式化字段为 'value' 或 'value（来自 settings 文件）'。"""
    if not field.value:
        return fallback
    if field.origin.startswith('settings:'):
        return f'{field.value}（来自 {field.origin[len("settings:"):]}）'
    return field.value


def harness_agent_label() -> str:
    """返回 frontmatter `agent` 字段；支持 WHOAMI_AGENT env 覆盖。"""
    return os.environ.get('WHOAMI_AGENT', '').strip() or detect_harness()


def output_frontmatter(
    cfg: Optional[dict[str, FieldSource]] = None,
    ccswitch: Optional[dict] = None,
    opencode_state: Optional[dict] = None,
) -> None:
    """输出可追加到 agent 笔记 frontmatter 的 YAML 片段（不含开闭 ---）。"""
    degraded = False
    if opencode_state:
        pid = (opencode_state.get('provider_id') or '').strip()
        mid = (opencode_state.get('model_id') or '').strip()
        model = f'{pid}/{mid}' if pid and mid else 'unknown'
        degraded = bool(opencode_state.get('degraded', False))
    else:
        model_field = (cfg or {}).get('model', FieldSource('unknown', ''))
        base_url = (cfg or {}).get('base_url', FieldSource('', '')).value
        if ccswitch and not base_url:
            model = ccswitch_identity(ccswitch)
        else:
            model = model_field.value or 'unknown'

    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    try:
        actual_host = socket.gethostname().strip() or 'unknown'
    except Exception:
        actual_host = 'unknown'
    primary_host = os.environ.get('PRIMARY_HOST', '').strip()

    if primary_host and actual_host != primary_host:
        print(f'host: {actual_host}')
    print(f'agent: {harness_agent_label()}')
    print(f'model: {model}')
    if degraded:
        print('confidence: low')
    print(f'generated_at: {now}')


def _print_opencode_report() -> int:
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
    pid, mid = state['provider_id'], state['model_id']
    print(f'Provider: {provider_desc}')
    print(f'  └ opencode session.model: providerID={pid}, modelID={mid}')
    print(f'BASE_URL: {base_url or "(内置 provider；未在 opencode.json 中显式声明)"}')
    print(f'MODEL:    {pid}/{mid}')
    if state.get('degraded'):
        print(f'  ⚠ 并发/陈旧会话检测：model 可受同目录并发污染（reason={state.get("reason", "")}）；'
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


def _print_claude_report() -> int:
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

    sonnet, opus, haiku = cfg['sonnet'].value, cfg['opus'].value, cfg['haiku'].value
    if sonnet and sonnet == opus == haiku:
        if main_model and sonnet == main_model:
            print(f'层级别名: Sonnet / Opus / Haiku 全部 → {sonnet}（统一后端）')
        elif main_model:
            print(f'层级别名: Sonnet / Opus / Haiku 全部 → {sonnet}（与 MODEL={main_model} 不同）')
        else:
            print(f'层级别名: Sonnet / Opus / Haiku 全部 → {sonnet}（统一后端，MODEL 未显式设）')
    else:
        diffs = [
            f'{label} → {val}'
            for label, val in (('Sonnet', sonnet), ('Opus', opus), ('Haiku', haiku))
            if val and val != main_model
        ]
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


def main() -> int:
    harness = detect_harness()

    if '--frontmatter' in sys.argv:
        if harness == 'codex':
            output_frontmatter({'model': FieldSource('openai/codex', 'runtime')})
        elif harness == 'opencode':
            output_frontmatter(opencode_state=read_opencode_model_state())
        else:
            output_frontmatter(resolve_effective_config(), read_ccswitch_active('claude'))
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
    if harness == 'opencode':
        return _print_opencode_report()
    return _print_claude_report()


if __name__ == '__main__':
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from common import log_script_run
        log_script_run()
    except Exception:
        pass
    raise SystemExit(main())
