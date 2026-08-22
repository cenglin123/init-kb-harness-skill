/**
 * kb-guard.js — opencode 宿主动作侧护栏插件
 *
 * 生成来源：plan 20260821-宿主动作侧护栏机制增强
 * 协议契约位置：.meta/scripts/host_guard.py docstring
 *
 * fail-open 边界声明：
 *   宿主侧插件崩溃不阻断，软规则兜底。
 *   spawn 失败（无 python 等）→ console.warn 放行。
 *   host_guard.py exit 2（内部错误）→ throw Error 阻断。
 */

import { spawn } from "child_process";
import { appendFileSync, mkdirSync } from "fs";
import { join, dirname } from "path";

const GUARD_SCRIPT = ".meta/scripts/host_guard.py";

const INTERCEPT_TOOLS = new Set([
  "edit",
  "write",
  "read",
  "grep",
  "bash",
  "multiedit",
  "patch",
]);

const COMPACT_SENTINEL =
  "【compact 恢复·强制自检】你刚从压缩恢复。" +
  "在继续任何实质性工作前必须完成：" +
  "①主从自检：比对 hostname 与 .env:PRIMARY_HOST，从机进入受限写入模式；" +
  "②模型自检：运行 python .meta/scripts/whoami.py；" +
  "③读取 .meta/memory/memory.md 及其中索引到的最近项目记忆。" +
  "完成前禁止写操作、禁止回答涉及库内状态的问题。";

/**
 * 运行 host_guard.py 子命令，返回 stdout。
 * spawn 失败时返回 null（fail-open）。
 */
function runGuard(directory, subcommand, stdinData, extraArgs = []) {
  return new Promise((resolve) => {
    const args = [GUARD_SCRIPT, subcommand, ...extraArgs];
    let proc;
    try {
      proc = spawn("python", args, {
        cwd: directory,
        stdio: ["pipe", "pipe", "pipe"],
        env: { ...process.env, PYTHONIOENCODING: "utf-8" },
      });
    } catch {
      console.warn(`[kb-guard] spawn 失败（无 python?），放行`);
      resolve(null);
      return;
    }

    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d.toString("utf-8")));
    proc.stderr.on("data", (d) => (stderr += d.toString("utf-8")));

    proc.on("close", (code) => {
      if (code === 2) {
        // host_guard 内部错误或 codex deny
        resolve({ error: true, stderr, stdout, code });
      } else {
        resolve({ error: false, stdout, stderr, code });
      }
    });

    proc.on("error", () => {
      console.warn(`[kb-guard] 进程启动失败，放行`);
      resolve(null);
    });

    if (stdinData) {
      proc.stdin.write(stdinData, "utf-8");
      proc.stdin.end();
    }
  });
}

export const KbGuard = async ({ directory, client }) => {
  // ─── tool.execute.before：工具调用前门禁 ───────────────────────────
  client.on("tool.execute.before", async (output) => {
    const toolName = output?.tool;
    if (!toolName || !INTERCEPT_TOOLS.has(toolName)) {
      return;
    }

    const payload = JSON.stringify({
      tool_name: toolName,
      tool_input: output.args || {},
      cwd: directory,
    });

    const result = await runGuard(directory, "pre-tool", payload, [
      "--host",
      "opencode",
    ]);

    if (result === null) {
      // spawn 失败 → fail-open
      return;
    }

    if (result.error) {
      throw new Error(`host_guard 内部错误: ${result.stderr}`);
    }

    try {
      const data = JSON.parse(result.stdout.trim());
      if (data.decision === "deny") {
        throw new Error(data.reason);
      }
    } catch (e) {
      if (e.message && e.message.startsWith("[DENY]")) {
        throw e;
      }
      // JSON 解析失败 → fail-open
      console.warn(`[kb-guard] stdout 解析失败，放行: ${e.message}`);
    }
  });

  // ─── experimental.session.compacting：compact 哨兵注入 ─────────────
  client.on("experimental.session.compacting", async (output) => {
    const result = await runGuard(directory, "compact-context", "", [
      "--plain",
    ]);

    if (result && !result.error && result.stdout.trim()) {
      output.context.push(result.stdout.trim());
    } else {
      // 失败 → 内联硬编码副本
      output.context.push(COMPACT_SENTINEL);
    }
  });

  // ─── session.compacted：记录日志 ─────────────────────────────────
  client.on("session.compacted", () => {
    try {
      const logDir = join(directory, ".meta", "logs");
      mkdirSync(logDir, { recursive: true });
      const logFile = join(logDir, "compact-guard.log");
      const line = `${new Date().toISOString()} session.compacted\n`;
      appendFileSync(logFile, line, "utf-8");
    } catch {
      // 日志写入失败静默
    }
  });
};
