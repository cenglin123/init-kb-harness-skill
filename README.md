# init-kb-harness · 知识库 Harness 引导器

给**任意** Obsidian 仓库（含不健全/空仓库、office 文档为主的仓库）从零建立一套可自维护、自沉淀、自运行的 harness 框架——维护管线（批量任务并行）+ 语义检索（.md + docx/xlsx/pptx/pdf 提取）+ 记忆 + converge 治理。**Phase 1→2→3 全量必装**，一次 bootstrap 完成。

这是一个 **agent skill**（给 AI 编码助手用的能力包），不是独立命令行工具。需要在 Claude Code / Codex CLI / opencode 等 agent harness 里加载后由 agent 触发执行。

> **与 init-agent-docs 的关系**：标准执行序是**先 init-agent-docs 打底**（docs/ 层级、plan-as-handoff、CHANGELOG 脚本化），**再本技能叠加**（检索/维护/记忆/治理）。两者哲学同源（AGENTS.md 是目录不是百科、渐进披露、Occam+Bitter Lesson）；AGENTS.md 一律以本技能模板为准覆盖（非叠加）。详见 SKILL.md 首节。

---

## ⚠️ API 后端（重要）

**默认绑 DeepSeek（chat）+ 智谱 Zhipu（embed），均为付费中文 API。** 换后端需改 `.meta/scripts/common.py` 的 client 类（不提供工厂抽象）。

- `DEEPSEEK_API_KEY` → chat completion（summarize.py / synthesize.py / semantic_lint.py --deep 等）
- `ZHIPU_API_KEY` → embedding（embed.py / ask.py 查询）
- 两把 key 自备，走目标仓库的 `.env`（见 `templates/env.example`）
- **DeepSeek 模型选型**：一律用最新版的**次等**模型（当前 `deepseek-v4-flash`；完整策略见 SKILL.md §约束）

---

## Installation

Installation 仅指 Phase 1-3 的机械步骤。Phase 0（体检 + 隐私嗅探 + taxonomy 推断 + 交互式引导）由技能触发，不是手动安装步骤。

### 前置：Phase 0（由技能触发，read-only 体检 + 交互式引导）

在目标 Obsidian 仓库触发本技能后，agent 先跑 Phase 0 体检（隐私嗅探 + taxonomy 推断 + 仓库画像，含 office 文档统计），随后**一步一步引导用户决策**：隐私目录 / taxonomy / 全量安装确认 / API 配置四个决策点逐个给出选项供选择，选择结果记录进 `kb-bootstrap-plan.md`（执行记录）。全部决策完成即进 Phase 1。

### Phase 1→2→3（机械安装，一次完成）

1. 检测/调用 `init-agent-docs` 打底（已装则复用不重铺）
2. 拷贝**全量脚本集**到 `.meta/scripts/`（含 maintain.py 完整管线 + extract_office + dream/semantic_lint/synthesize/knowledge_map/bm25_index）
3. `pip install -r requirements.txt`（含 office 提取依赖）
4. `.env.example` → `.env`（API key + `PRIMARY_HOST` + `MAINTAIN_CONCURRENCY` + office/阈值参数）
5. `docs/CONSTITUTION.md` + `docs/TAXONOMY.md` + `AGENTS.md`（`bootstrap_status: in_progress`）→ 跑 `sync_agents.py`
6. 安装 `.githooks/pre-commit`（统一版）并接线：`git config core.hooksPath .githooks`
7. Phase 2：拷贝 memory-scaffold 到 `.meta/memory/`
8. Phase 3：建 `.meta/converge/` charter + `.meta/governed-files.txt` + `.env:CONVERGE_DIR`
9. **完成判定**：`python .meta/scripts/maintain.py --full` 跑通 + agent 引导用户试跑 ask.py 检索（office 为主仓库验证命中 office 内容）、交互确认质量可接受 → `bootstrap_status: completed`

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
- **office 提取**：python-docx / openpyxl / python-pptx / pypdf（随 requirements.txt 安装；缺装时对应格式跳过）
- **Git Bash**（Git for Windows 自带；pre-commit hook 是 bash 脚本。纯 GitHub Desktop / TortoiseGit 无 bash 环境需另装）
- **Agent harness**：Claude Code / Codex CLI / opencode 等（需支持 skill 加载）

---

## Phase 概览（全量必装，顺序执行）

| Phase | 性质 | 装什么 |
|-------|------|--------|
| **0** | read-only 体检 + 交互引导 | 分步决策引导（隐私 / taxonomy / 全量确认 / API），决策记录进 `kb-bootstrap-plan.md` |
| **1** | 骨架 + 检索 + office 提取 | 全量脚本集（maintain.py 完整管线 + extract_office/embed/summarize/index/graph/bm25/knowledge_map/health/ask/whoami 等）+ AGENTS.md + pre-commit |
| **2** | 自沉淀 | memory 结构（MEMORY.md + role + workflows）；dream/synthesize/semantic_lint 脚本已随 Phase 1 拷入 |
| **3** | 治理 | `.meta/converge/` charter（**唯一治理目录**，converge + fresh verifier 单一生命周期）+ governed-files SSOT + CONVERGE_DIR 绑定 |

维护批量任务默认并行：脚本内 ThreadPool 并发（`MAINTAIN_CONCURRENCY`）+ maintain.py 编排层 embed∥summarize + agent 手工批量维护必须并行派发（写入 AGENTS.md 准则）。

---

## for maintainers

本 skill 的 bundle 脚本（`scripts/*.py`）与作者自用知识库的 `.meta/scripts/` 演进会 drift。

- 维护者检测 drift：`diff -r ~/.agents/skills/init-kb-harness/scripts/ <源仓库>/.meta/scripts/`（源仓库 = 作者的 vault，或 forker 的上游 fork）。
- v0.4 起 bundle 与 vault 的**有意差异**（diff 时豁免）：bundle 无 vault 特化件（co_retrieval / gc / maintain_lock / maintain_trigger / governance_check / synthesis_index / search_sessions / inflight）；bundle 独有 extract_office.py（office 提取）与 embed/summarize 的 ThreadPool 并发；ask.py 去掉 co_retrieval 回写。
- DeepSeek 发布新代模型时，同步升级 `templates/env.example` 与 `scripts/common.py` 的默认型号为该代次等型号（策略见 SKILL.md §约束）。

---

## LICENSE

MIT，见 [LICENSE](LICENSE)。
