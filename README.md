# init-kb-harness · 知识库 Harness 引导器

给**任意** Obsidian 仓库（含不健全/空仓库）从零建立一套可自维护、自沉淀、自运行的 harness 框架。分层安装，按成熟度解锁——维护管线 + 语义检索 + 记忆 + 治理。

这是一个 **agent skill**（给 AI 编码助手用的能力包），不是独立命令行工具。需要在 Claude Code / Codex CLI / opencode 等 agent harness 里加载后由 agent 触发执行。

> **与 init-agent-docs 的关系**：本技能**调用** init-agent-docs 铺 docs/ 层级，但 AGENTS.md 一律以本技能模板为准（非叠加）。

---

## ⚠️ API 后端（重要）

**默认绑 DeepSeek（chat）+ 智谱 Zhipu（embed），均为付费中文 API。** 换后端需改 `.meta/scripts/common.py` 的 client 类（Phase 1 不提供工厂抽象）。

- `DEEPSEEK_API_KEY` → chat completion（summarize.py / inbox_scan.py 等）
- `ZHIPU_API_KEY` → embedding（embed.py）
- 两把 key 自备，走目标仓库的 `.env`（见 `templates/env.example`）

---

## Installation

Installation 仅指 Phase 1a 的机械步骤。Phase 0（体检 + 隐私嗅探 + taxonomy 推断）由技能触发，不是手动安装步骤。

### 前置：Phase 0（由技能触发，read-only）

在目标 Obsidian 仓库触发本技能后，agent 会先跑 Phase 0 体检，产出 `kb-bootstrap-plan.md`（含隐私嗅探结果 + taxonomy 草案 + Phase 建议）。**用户人工审核**后在 plan 顶部把 `phase_0_confirmed: false` 改为 `true`，agent 才进 Phase 1。

### Phase 1a（机械安装步骤）

用户确认 Phase 0 后，agent 执行：

1. 调用 `init-agent-docs` 铺 docs/ 层级
2. 拷贝最小脚本集到 `.meta/scripts/`（`common.py` / `maintain-lite.py` / `embed.py` / `summarize.py` / `build_index.py` / `ask.py`）
3. `.env.example` → `.env`（填 API key + `PRIMARY_HOST` + `ARCHIVE_MARKERS`）
4. `docs/CONSTITUTION.md` + `docs/TAXONOMY.md` + `AGENTS.md`（`bootstrap_status: in_progress`）
5. 跑 `python .meta/scripts/sync_agents.py`（生成 CLAUDE.md / GEMINI.md）
6. 安装 `.githooks/pre-commit`（来自 `refs/pre-commit-template`）并接线：`git config core.hooksPath .githooks`
7. **完成判定**：`python .meta/scripts/maintain-lite.py --full` 跑通 + 用户在 plan 勾选「会话可用确认」→ agent 把 `bootstrap_status` 改 `completed`

> Phase 1b / 2 / 3 按需追加，见下方 Phase 概览。

---

## 触发词

在 agent harness 里对目标仓库说以下任一即触发：

| 中文 | 英文 |
|------|------|
| 装知识库系统 | bootstrap kb harness |
| 给这个仓库建立维护体系 | init knowledge base |
| 初始化知识库 harness | set up kb maintenance |

---

## 依赖

- **Python 3.10+**
- **DeepSeek API key**（chat，付费）+ **智谱 Zhipu API key**（embedding，付费）
- **Git Bash**（Git for Windows 自带；pre-commit hook 是 bash 脚本。纯 GitHub Desktop / TortoiseGit 无 bash 环境需另装）
- **Agent harness**：Claude Code / Codex CLI / opencode 等（需支持 skill 加载）

---

## Phase 概览

| Phase | 触发条件 | 装什么 |
|-------|---------|--------|
| **0** | 首次触发（read-only 体检） | `kb-bootstrap-plan.md`（隐私嗅探 + taxonomy 草案 + Phase 建议） |
| **1a** | `notes<30` | 最小自维护骨架（maintain-lite + embed/summarize/index/ask + AGENTS.md + pre-commit） |
| **1b** | `30≤notes≤200` 追加 | health_report + graph + inbox_scan + whoami + changelog_append 等 |
| **2** | `notes>200 OR orphan_ratio>0.3` | 自沉淀（memory 结构 + dream + semantic_lint + synthesize + 换回完整 maintain.py） |
| **3** | `has_host_field≥2` 或 `git_author_count≥2` | 治理（converge/deliberate/audit 三 charter + pre-commit 升级完整版 + CONSTITUTION 升级） |

---

## for maintainers

本 skill 的 bundle 脚本（`scripts/*.py`）与作者自用知识库的 `.meta/scripts/` 演进会 drift。

- 历史上 bundle 内置 `scripts/skill-sync-check.py` 做 drift 检测；现**已从 bundle 移除**（对 99% 公开用户是死代码——他们没有上游可 drift）。
- 作者本人将 `skill-sync-check.py` 保留为 vault 内维护脚本（不公开发布）。
- **fork / 同步上游时**：在源 vault 用 `HARNESS_SOURCE_VAULT=/path/to/source/vault python skill-sync-check.py` 做 SHA256 逐脚本比对。

---

## LICENSE

MIT，见 [LICENSE](LICENSE)。
