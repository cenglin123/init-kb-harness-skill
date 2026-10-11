/**
 * kb-guard.js — opencode 宿主动作侧护栏插件
 *
 * 生成来源：plan 20260821-宿主动作侧护栏机制增强
 * 协议契约位置：.meta/scripts/host_guard.py docstring
 * 插件 API：opencode v2（default export { id, setup } + ctx 域 hook；v2.0.24 起不接受具名导出函数）。
 * compact 哨兵：
 *   - compaction hook 记录 compact 发生并预取哨兵文本（live 失败则内联硬编码副本）
 *   - context hook 在该 session 下一次 agent 循环请求的 system 注入哨兵（一次性）
 *
 * fail-open 边界声明：
 *   宿主侧插件崩溃不阻断，软规则兜底。
 *   spawn 失败（无 python 等）→ console.warn 放行。
 *   host_guard.py exit 2（内部错误）→ throw Error 阻断。
 */

import { spawn } from "child_process";
import { appendFileSync, mkdirSync } from "fs";
import { join } from "path";

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

// compact 后哨兵注入的时效窗口与一次性标记
const COMPACT_WINDOW_MS = 30 * 60_000;

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
        // 宿主服务进程无 console：不隐藏则 python 新建 console，被默认终端接管为可见窗口并抢焦点
        windowsHide: true,
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

// 注：不 import "@opencode/plugin"——Plugin.define 在运行时是恒等糖，
// 独立编译版宿主对该裸导入解析失败；直接导出定义对象即可（id + setup）。
export default {
  id: "kb-guard",
  async setup(ctx) {
    const directory = ctx.location.directory;

    // sessionID → { ts, text, injected }：compact 发生记录 + 哨兵文本缓存
    const compactedAt = new Map();

    // ─── tool.execute.before：工具调用前门禁 ───────────────────────────
    await ctx.tool.hook("execute.before", async (event) => {
      const toolName = event?.tool;
      if (!toolName || !INTERCEPT_TOOLS.has(toolName)) {
        return;
      }

      const payload = JSON.stringify({
        tool_name: toolName,
        tool_input: event.input || {},
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

    // ─── compaction：记录 compact 发生并预取哨兵文本 ────────────────────
    await ctx.session.hook("compaction", async (event) => {
      const result = await runGuard(directory, "compact-context", "", [
        "--plain",
      ]);
      const text =
        result && !result.error && result.stdout.trim()
          ? result.stdout.trim()
          : COMPACT_SENTINEL;

      // 清理过期条目，记录本次 compact
      const now = Date.now();
      for (const [sid, mark] of compactedAt) {
        if (now - mark.ts > COMPACT_WINDOW_MS) compactedAt.delete(sid);
      }
      compactedAt.set(event.sessionID, { ts: now, text, injected: false });

      // 诊断日志
      try {
        const logDir = join(directory, ".meta", "logs");
        mkdirSync(logDir, { recursive: true });
        const line = `${new Date().toISOString()} session.compacted\n`;
        appendFileSync(join(logDir, "compact-guard.log"), line, "utf-8");
      } catch {
        // 日志写入失败静默
      }
    });

    // ─── context：compact 后首次 agent 循环请求注入哨兵（一次性）─────────
    await ctx.session.hook("context", (event) => {
      const mark = compactedAt.get(event.sessionID);
      if (!mark || mark.injected || Date.now() - mark.ts > COMPACT_WINDOW_MS) {
        return;
      }
      mark.injected = true;
      event.system.push({ type: "text", text: mark.text });
    });
  },
};
