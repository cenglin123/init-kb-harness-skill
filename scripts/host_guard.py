#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
host_guard.py — 宿主动作侧护栏（plan 20260821-宿主动作侧护栏机制增强）

单文件、零第三方依赖、Python ≥ 3.10。
stdin/stdout 显式 UTF-8（含 Windows 中文路径安全）。

子命令：
  pre-tool [--host <claude|codex|opencode|generic>]
  prompt [--host <claude|codex|generic>]
  compact-context [--plain]
  simulate-host --simulate-host <hostname>

协议契约位置：本文件 docstring。
fail-open 边界：宿主侧插件崩溃不阻断，软规则兜底。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from pathlib import Path

# ─── Windows UTF-8 输出保障 ────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ─── 常量 ─────────────────────────────────────────────────────────────────

# vault 根 = 脚本自身路径 parents[2]（.meta/scripts/host_guard.py → vault root）
# 支持 HOST_GUARD_VAULT_ROOT 环境变量覆盖（测试用）
_VAULT_ROOT: Path | None = None


def vault_root_from_script() -> Path:
    """从脚本位置上溯两级定位 vault root。支持环境变量覆盖。"""
    if _VAULT_ROOT is not None:
        return _VAULT_ROOT
    override = os.environ.get("HOST_GUARD_VAULT_ROOT", "").strip()
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[2]

# 工具名分类（全小写；运行时 tool_name 经 _norm_tool_name 归一化后比较）
_READ_TOOLS = {"read", "grep"}
_EDIT_WRITE_TOOLS = {
    "edit", "write", "create", "apply_patch", "multiedit",
    "str_replace_editor", "write_file", "replace", "editfile",
    "editfiles", "notebookedit",
}
_BASH_TOOLS = {"bash"}
_GLOB_TOOLS = {"glob"}

# 目标路径提取键名集合
_PATH_KEYS = {
    "file_path", "filePath", "file", "path", "target",
    "target_file", "targetPath", "filename",
}

# CHANGELOG 直改 deny
_CHANGELOG_DENY = {
    "reason": "CHANGELOG 直改被拦截",
    "rule_ref": "AGENTS.md 约束速查表",
    "correct_entry": "python .meta/scripts/changelog_append.py",
}

# CLAUDE.md / GEMINI.md 直改 deny
_AGENTS_SYNC_DENY = {
    "reason": "CLAUDE.md/GEMINI.md 直改被拦截",
    "rule_ref": "AGENTS.md 约束速查表",
    "correct_entry": "编辑 AGENTS.md 后运行 python .meta/scripts/sync_agents.py",
}

# compact 哨兵文本
_COMPACT_TEXT = (
    "【compact 恢复·强制自检】你刚从压缩恢复。"
    "在继续任何实质性工作前必须完成："
    "①主从自检：比对 hostname 与 .env:PRIMARY_HOST，从机进入受限写入模式；"
    "②模型自检：运行 python .meta/scripts/whoami.py；"
    "③读取 .meta/memory/memory.md 及其中索引到的最近项目记忆。"
    "完成前禁止写操作、禁止回答涉及库内状态的问题。"
)

# 触发词列表
_TRIGGER_WORDS = ["基于知识库", "结合知识库", "用库的视角"]


# ─── 工具函数 ─────────────────────────────────────────────────────────────


def load_policy(root: Path) -> dict:
    """加载 host-write-policy.json。缺失/损坏时抛异常。"""
    p = root / ".meta" / "rules" / "host-write-policy.json"
    if not p.exists():
        raise FileNotFoundError(f"策略文件缺失: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"策略文件损坏: {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"策略文件格式错误: {p}")
    return data


def load_primary_host(root: Path) -> str:
    """读 .env 的 PRIMARY_HOST。缺失/无该键/读取失败返回空字符串。"""
    env_file = root / ".env"
    if not env_file.exists():
        return ""
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == "PRIMARY_HOST":
                    return v.strip()
    except Exception:
        pass
    return ""


def is_primary_host(hostname: str, primary_host: str) -> bool:
    """判定是否为主机。primary_host 为空时 fail-closed 返回 False。"""
    if not primary_host:
        return False
    return hostname == primary_host


def _norm_tool_name(name: str) -> str:
    """归一化工具名为小写（strip + lower）。"""
    return name.strip().lower()


def normalize_path(rel: str, vault_root: Path | None = None) -> str:
    """归一化为 vault 相对 POSIX 形（剥 ./、vault 内绝对路径转相对）。"""
    if not rel:
        return ""
    rel = rel.replace("\\", "/")
    # 剥 ./
    while rel.startswith("./"):
        rel = rel[2:]
    # vault 内绝对路径转相对
    if vault_root is None:
        vault_root = vault_root_from_script()
    vault_str = str(vault_root).replace("\\", "/")
    if rel.startswith(vault_str):
        rel = rel[len(vault_str):]
        while rel.startswith("/"):
            rel = rel[1:]
    return rel


def extract_paths_from_tool_input(
    tool_input: dict, tool_name: str, vault_root: Path | None = None
) -> list[str]:
    """从 tool_input 提取目标路径列表。"""
    paths = []
    if not isinstance(tool_input, dict):
        return paths
    # 标准路径键
    for key in _PATH_KEYS:
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            paths.append(normalize_path(val, vault_root))
    # Bash 类工具取 command 字符串
    if _norm_tool_name(tool_name) in _BASH_TOOLS:
        cmd = tool_input.get("command", "")
        if isinstance(cmd, str):
            paths.append(cmd)
    return paths


def _segment_match(segments: list[str], pattern_segs: list[str]) -> bool:
    """段式 glob 递归匹配。

    - `**` 匹配零或多整段
    - `*` 匹配段内任意非 `/` 字符
    - `?` 匹配单个非 `/` 字符
    """
    pi = 0
    si = 0
    while pi < len(pattern_segs) and si < len(segments):
        if pattern_segs[pi] == "**":
            pi += 1
            if pi == len(pattern_segs):
                return True
            while si < len(segments):
                if _segment_match(segments[si:], pattern_segs[pi:]):
                    return True
                si += 1
            return False
        else:
            if not _segment_glob_match(segments[si], pattern_segs[pi]):
                return False
            si += 1
            pi += 1
    while pi < len(pattern_segs) and pattern_segs[pi] == "**":
        pi += 1
    return pi == len(pattern_segs) and si == len(segments)


def _segment_glob_match(text: str, pattern: str) -> bool:
    """单段 glob 匹配（`*` 不跨 `/`，`?` 单字符）。"""
    ti = 0
    pi = 0
    star_pi = -1
    star_ti = -1
    while ti < len(text):
        if pi < len(pattern) and (pattern[pi] == text[ti] or pattern[pi] == "?"):
            ti += 1
            pi += 1
        elif pi < len(pattern) and pattern[pi] == "*":
            star_pi = pi
            star_ti = ti
            pi += 1
        elif star_pi != -1:
            pi = star_pi + 1
            star_ti += 1
            ti = star_ti
        else:
            return False
    while pi < len(pattern) and pattern[pi] == "*":
        pi += 1
    return pi == len(pattern)


def glob_match(path: str, pattern: str) -> bool:
    """段式 glob 匹配。

    - `**` 匹配零或多整段
    - `*` 段内任意非 `/` 字符
    - `?` 单个非 `/` 字符
    - 零依赖手写实现，替换 fnmatch
    """
    path = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    p_segs = [s for s in pattern.split("/") if s]
    t_segs = [s for s in path.split("/") if s]
    return _segment_match(t_segs, p_segs)


def glob_list_match(path: str, patterns: list[str]) -> bool:
    """路径匹配任一 pattern。"""
    return any(glob_match(path, p) for p in patterns)


def read_frontmatter_fields(filepath: Path) -> dict:
    """读 YAML frontmatter 的 source 和 host 字段。缺失返回空 dict。"""
    result = {}
    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception:
        return result
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|\Z)", text, re.DOTALL)
    if not match:
        return result
    fm_text = match.group(1)
    for field in ("source", "host"):
        m = re.search(rf"^{field}:\s*([^\s#]+)\s*(?:#.*)?$", fm_text, re.MULTILINE)
        if m:
            result[field] = m.group(1).strip("'\"")
    return result


def make_deny_output(
    reason: str,
    rule_ref: str,
    correct_entry: str = "N/A",
) -> dict:
    """构造 deny 输出 JSON 信封。"""
    deny_msg = f"[DENY] {reason} | 规则来源: {rule_ref} | 正确入口: {correct_entry}"
    return {
        "decision": "deny",
        "reason": deny_msg,
        "rule_ref": rule_ref,
        "correct_entry": correct_entry,
    }


def make_allow_output() -> dict:
    """构造 allow 输出 JSON 信封。"""
    return {
        "decision": "allow",
        "reason": "allowed",
        "rule_ref": "N/A",
        "correct_entry": "N/A",
    }


def output_json(obj: dict) -> None:
    """输出 JSON 到 stdout。"""
    print(json.dumps(obj, ensure_ascii=False))


# ─── pre-tool 判定逻辑 ────────────────────────────────────────────────────


def _check_privacy(
    paths: list[str],
    privacy_dirs: list[str],
    tool_name: str,
) -> dict | None:
    """P0 隐私检查。命中则返回 deny 输出，否则 None。"""
    tn = _norm_tool_name(tool_name)

    # Glob 类工具始终 allow
    if tn in _GLOB_TOOLS:
        return None

    # Bash 命令包含隐私目录前缀
    if tn in _BASH_TOOLS:
        for p in paths:
            # 归一化反斜杠以支持 Windows 路径
            p_norm = p.replace("\\", "/")
            if "000【备忘录" in p_norm:
                return make_deny_output(
                    "Bash 命令包含隐私目录前缀",
                    ".meta/rules/category-privacy.md",
                )
        return None

    # Read/Grep/Edit/Write 类工具
    is_access_tool = (
        tn in _READ_TOOLS
        or tn in _EDIT_WRITE_TOOLS
    )
    if not is_access_tool:
        return None

    for p in paths:
        if glob_list_match(p, privacy_dirs):
            return make_deny_output(
                "目标命中隐私目录",
                ".meta/rules/category-privacy.md",
            )
    return None


def _check_edit_redirect(
    paths: list[str],
    tool_name: str,
) -> dict | None:
    """P3 编辑重定向检查。"""
    if _norm_tool_name(tool_name) not in _EDIT_WRITE_TOOLS:
        return None
    for p in paths:
        if p == "docs/CHANGELOG.md":
            return make_deny_output(**_CHANGELOG_DENY)
        if p in ("CLAUDE.md", "GEMINI.md"):
            return make_deny_output(**_AGENTS_SYNC_DENY)
    return None


def _check_secondary_write(
    paths: list[str],
    tool_name: str,
    is_primary: bool,
    deny_rules: list[dict],
    hostname: str,
    vault_root: Path,
) -> dict | None:
    """P1 从机写禁检查。仅当 is_primary 为 false 时激活。"""
    if is_primary:
        return None
    if _norm_tool_name(tool_name) not in _EDIT_WRITE_TOOLS:
        return None

    for rule in deny_rules:
        rtype = rule.get("type")
        rname = rule.get("name", "unnamed")

        if rtype == "reference":
            # reference 类型不参与机械判定
            continue

        if rtype == "path_only":
            rule_paths = rule.get("paths", [])
            for p in paths:
                if glob_list_match(p, rule_paths):
                    return make_deny_output(
                        rule.get("reason", "从机写禁"),
                        f"host-write-policy.json#{rname}",
                    )

        elif rtype == "permission_qualified":
            path_pattern = rule.get("path_pattern", "")
            activate_when = rule.get("activate_when", "")
            allow_when = rule.get("allow_when", {})
            default_decision = rule.get("default_decision", "deny")

            # 检查 activate_when（仅从机激活）
            if "is_primary == false" in activate_when and is_primary:
                continue

            for p in paths:
                if not glob_match(p, path_pattern):
                    continue
                # 读目标 plan 的 frontmatter
                abs_path = vault_root / p
                fm = read_frontmatter_fields(abs_path)
                source = fm.get("source", "")
                host = fm.get("host", "")

                required_source = allow_when.get("source_equals", "")
                host_equals = allow_when.get("host_equals_current", False)

                if source == required_source and (not host_equals or host == hostname):
                    continue  # allow
                else:
                    return make_deny_output(
                        rule.get("reason", "从机写禁"),
                        f"host-write-policy.json#{rname}",
                    )

    return None


def run_pre_tool(args: argparse.Namespace) -> int:
    """pre-tool 子命令主逻辑。"""
    root = vault_root_from_script()

    # 读 stdin（字节级 UTF-8，避免 Windows 控制台代码页解码乱码）
    try:
        if hasattr(sys.stdin, "buffer"):
            raw = sys.stdin.buffer.read().decode("utf-8")
        else:
            raw = sys.stdin.read()
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        err_msg = "stdin 非合法 JSON"
        print(err_msg, file=sys.stderr)
        return 2
    except Exception as exc:
        err_msg = f"stdin 读取失败: {exc}"
        print(err_msg, file=sys.stderr)
        return 2

    # 加载策略
    try:
        policy = load_policy(root)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # 字段宽容解析
    tool_name = (
        payload.get("tool_name")
        or payload.get("toolName")
        or ""
    )
    tool_input_raw = (
        payload.get("tool_input")
        or payload.get("toolInput")
        or payload.get("toolArgs")
        or {}
    )
    if isinstance(tool_input_raw, str):
        try:
            tool_input = json.loads(tool_input_raw)
        except Exception:
            tool_input = {}
    else:
        tool_input = tool_input_raw if isinstance(tool_input_raw, dict) else {}

    hostname = payload.get("host", "") or socket.gethostname()
    primary_host = load_primary_host(root)
    is_primary = is_primary_host(hostname, primary_host)
    privacy_dirs = policy.get("privacy_dirs", [])
    deny_rules = policy.get("deny_rules", [])

    # simulate-host 覆盖
    if hasattr(args, "simulate_host") and args.simulate_host:
        hostname = args.simulate_host
        is_primary = is_primary_host(hostname, primary_host)

    # 提取目标路径
    paths = extract_paths_from_tool_input(tool_input, tool_name, root)

    # 判定顺序
    # P0 隐私
    result = _check_privacy(paths, privacy_dirs, tool_name)
    if result:
        if hasattr(args, "simulate_host") and args.simulate_host:
            result["simulated"] = True
            output_json(result)
            return 0
        return _output_by_host(args.host, result)

    # P3 编辑重定向
    result = _check_edit_redirect(paths, tool_name)
    if result:
        if hasattr(args, "simulate_host") and args.simulate_host:
            result["simulated"] = True
            output_json(result)
            return 0
        return _output_by_host(args.host, result)

    # P1 从机写禁
    result = _check_secondary_write(
        paths, tool_name, is_primary, deny_rules, hostname, root
    )
    if result:
        if hasattr(args, "simulate_host") and args.simulate_host:
            result["simulated"] = True
            output_json(result)
            return 0
        return _output_by_host(args.host, result)

    # allow
    allow = make_allow_output()
    if hasattr(args, "simulate_host") and args.simulate_host:
        allow["simulated"] = True
    output_json(allow)
    return 0


def _output_by_host(host: str, result: dict) -> int:
    """根据 --host 参数调整输出格式和 exit code。"""
    deny_msg = result.get("reason", "")

    if host == "claude":
        # Claude: hookSpecificOutput + exit 0
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": deny_msg,
            }
        }
        output_json(output)
        return 0

    if host == "codex":
        # Codex: stderr 打印 deny 模板 + exit 2
        print(deny_msg, file=sys.stderr)
        return 2

    # opencode / generic: 默认 JSON 信封 + exit 1
    output_json(result)
    return 1


# ─── prompt 子命令 ────────────────────────────────────────────────────────


def run_prompt(args: argparse.Namespace) -> int:
    """prompt 子命令主逻辑。"""
    try:
        if hasattr(sys.stdin, "buffer"):
            raw = sys.stdin.buffer.read().decode("utf-8")
        else:
            raw = sys.stdin.read()
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0
    except Exception:
        return 0

    # 字段宽容解析
    text = (
        payload.get("prompt")
        or payload.get("message")
        or payload.get("text")
        or ""
    )

    # 命中触发词
    for word in _TRIGGER_WORDS:
        if word in text:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": (
                        "提醒：检测到知识库引用触发词。按本库制度，"
                        "任何寻找仓库内容的步骤默认先跑 python .meta/scripts/ask.py（语义检索）；"
                        "判据以检索动作发生为准。"
                    ),
                }
            }
            output_json(output)
            return 0

    # 不命中 → 无输出 exit 0
    return 0


# ─── compact-context 子命令 ────────────────────────────────────────────────


def run_compact_context(args: argparse.Namespace) -> int:
    """compact-context 子命令主逻辑。"""
    if args.plain:
        print(_COMPACT_TEXT)
    else:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": _COMPACT_TEXT,
            }
        }
        output_json(output)
    return 0


# ─── simulate-host 子命令 ──────────────────────────────────────────────────


def run_simulate_host(args: argparse.Namespace) -> int:
    """simulate-host 子命令：复用 pre-tool 逻辑，永远 exit 0。"""
    return run_pre_tool(args)


# ─── 主入口 ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="宿主动作侧护栏（plan 20260821-宿主动作侧护栏机制增强）"
    )
    sub = parser.add_subparsers(dest="command")

    # pre-tool
    pt = sub.add_parser("pre-tool", help="工具调用前门禁检查")
    pt.add_argument("--host", choices=["claude", "codex", "opencode", "generic"], default="generic")

    # prompt
    pr = sub.add_parser("prompt", help="用户提示触发词检查")
    pr.add_argument("--host", choices=["claude", "codex", "generic"], default="generic")

    # compact-context
    cc = sub.add_parser("compact-context", help="compact 哨兵输出")
    cc.add_argument("--plain", action="store_true", help="输出裸文本（供 opencode 插件用）")

    # simulate-host
    sh = sub.add_parser("simulate-host", help="模拟指定 hostname 运行 pre-tool 判定")
    sh.add_argument("--simulate-host", required=True, help="模拟的 hostname")
    sh.add_argument("--host", choices=["claude", "codex", "opencode", "generic"], default="generic")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "pre-tool":
        return run_pre_tool(args)
    elif args.command == "prompt":
        return run_prompt(args)
    elif args.command == "compact-context":
        return run_compact_context(args)
    elif args.command == "simulate-host":
        return run_simulate_host(args)
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
