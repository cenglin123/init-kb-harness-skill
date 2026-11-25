---
name: init-kb-harness
description: Initialize a self-maintaining knowledge-base harness on an Obsidian vault (possibly immature/empty, or office-document-heavy). Default install is zero-API and includes local embeddings (fastembed/bge-small-zh) plus BM25, graph, and office extraction; semantic recall is baseline, not optional. The LLM layer (summaries/rerank) is opt-in via DeepSeek. Embedding endpoint may be switched to any OpenAI-compatible /embeddings (local Ollama/vLLM or cloud). Scaffolds maintenance pipeline + office extraction + memory + converge governance as a full install (Phase 1→2→3 all required). Use when the user says "给这个仓库装知识库系统" / "bootstrap kb harness" / "init knowledge base" / wants to turn a plain Obsidian vault into a self-maintaining second-brain. Existing harnesses use only the explicit agent-docs layout migration when requested; do not rebootstrap them.
---

# init-kb-harness · 知识库 Harness 引导器

给**任意** Obsidian 仓库（含不健全/空仓库、office 文档为主的仓库）从零建立一套可自维护、自沉淀、自运行的 harness 框架。**Phase 1→2→3 全量必装**，一次 bootstrap 顺序完成。

---

## 安装模式：语义层是基本盘，LLM 层按需

本套件面向**含大量 office / 非结构化文件的既有仓库**（不只是 agent 自己的记忆），语义召回是基础能力，不是装饰。因此分层如下：

| 层 | 默认 | 依赖 | 买到了什么 | 代价 |
|---|---|---|---|---|
| **基础层** | ✅ 开 | 无 | BM25 倒排（`bm25_index.py`）、图谱/反链、office 提取（子进程并行 + 单文件超时）、索引/健康/记忆/治理、agentic grep/glob | 0 |
| **语义层** | ✅ 开 | 内置模型 fastembed + bge-small-zh（随离线套装预置；也可指任意 OpenAI 兼容 `/embeddings`：Ollama/vLLM/LM Studio/云端） | 向量召回、`--hybrid`、语义关联 sidecar（`build_links.py`，纯余弦无 LLM）、`ask.py` 低置信自动升级的基础 | 全量建库一次（本地 CPU；云端几分钱），之后按 hash 增量 |
| **LLM 层** | ❌ 关 | `HARNESS_MODE=full` + `DEEPSEEK_API_KEY` | 摘要 / tag / synthesize / semantic_lint P2 / `--rerank` / `--deep` | 每篇一次、按 hash 增量；**不参与排序**，只影响可读性与综述素材 |

**默认 = 基础层 + 语义层（本地内置模型），零 API。** LLM 层默认不开：摘要不参与排序（BM25/向量索引都不收 `.meta/summaries`），它只服务"命中列表多一行说明"和"主题综述素材"。

**升级嵌入模型**（例如换 Zhipu / qwen3-embedding 等更高维模型）：改 `EMBED_BASE_URL` + `EMBED_MODEL` → `python .meta/scripts/embed.py --full` 重建向量库。内置模型保证升级前始终有可用嵌入。换模型后建议跑一次 `calibrate_thresholds.py`——默认阈值随后端给出（内置 0.58/0.72/0.62/0.50，HTTP 0.70/0.85/0.75/0.60），但语料不同仍需校准；样本 <20 对时脚本会回退默认。

**配置变了怎么办**（唯一运维纪律）：模型/维度/分块变化后 `embed.py` 会**拒绝增量并提示** `--full`；`--full` 先写临时表、**全部成功**后才在一条事务里切换——任何一片失败都不切换；旧库保留，但要先把 `EMBED_*` 回退到与库一致的配置才能查询。

**模式推断仅用于旧仓库**：未写 `HARNESS_MODE` 且嵌入库**没有** `embed_meta` 记录（旧版产物）时按 `full`；新式仓库（语义层产物有 meta）一律按 `lite`，不再由"库存在"反推 LLM 层。

**`--full` 的边界**：`maintain.py --full` 只重建本地派生件（索引 / 图谱 / 关联 / 健康 / 记忆）；重嵌入是 `embed.py --full`（整库换代的成本高，必须显式）。

| 能力 | 默认 | 加 LLM 层 |
|------|------|----------|
| BM25 / 图谱 / 索引 / office / health / dream | ✅ | ✅ |
| 向量召回 / `--hybrid` / semantic 边 | ✅ | ✅ |
| 摘要行 / tag 汇总 / synthesize / `--rerank` / `--deep` | ❌ | ✅ |

任一层都受**大文件闸门**约束（硬约束，写在脚本里）：超过 `LARGE_FILE_THRESHOLD_MB`（默认 1MB）的单文件不直接进嵌入/LLM 管线，挂起登记 `.meta/large-files-pending.md`，维护完成后 agent 必须向用户询问处理方式（全套 / 仅排除嵌入与LLM / 跳过 / 切分 / 删除），决定记录 `.meta/rules/large-file-policy.md`。

---

## 与 init-agent-docs 的关系（先打底，再叠加）

**标准执行序：先跑 init-agent-docs 打底，再跑本技能叠加。** 实际使用中两者几乎总是连用——init-agent-docs 负责通用 agent 协作文档层，本技能在其上叠加知识库特有的检索/维护/记忆/治理。

### 哲学同源

两个技能共享同一套设计哲学，叠加时不会互相打架：

- **仓库即唯一事实源**——Agent 看不到的等于不存在；一切状态落文件
- **AGENTS.md 是目录不是百科**——常驻上下文只放硬约束/默认偏好/导航指针，细节走渐进披露（AGENTS.md → docs/STRUCTURE.md → docs/*.md 按需读取）
- **计划是一等公民**——复杂任务先落 `docs/plans/`，plan 文件本身是跨会话/跨 agent 的交接协议（plan-as-handoff）
- **Occam + Bitter Lesson 双护栏**——防治理系统自增殖；通用方法（embedding/LLM）优于硬编码先验
- **软约束靠文档、硬约束靠工具**——能用 pre-commit/脚本强制的规则优先编码为工具

### 分工与叠加规则

| 层 | init-agent-docs 提供 | 本技能叠加 |
|----|---------------------|-----------|
| 文档层级 | docs/STRUCTURE.md、plans/、CHANGELOG 脚本化 | docs/CONSTITUTION.md、docs/TAXONOMY.md |
| 入口 | AGENTS.md 单一入口 | **AGENTS.md 以本技能 `refs/agents-template.md` 为准覆盖**（图书管理员准则 + 维护管线 + bootstrap 状态） |
| 脚本 | changelog.py / audit.py / check_all.py | `.meta/scripts/` 全套维护管线 |
| 治理 | ultraverge 判定原则 | converge 单一生命周期落地（`.meta/converge/` + governed-files SSOT） |

### 已装 init-agent-docs 的检测与复用

Phase 1 开始前检测目标仓库：已有 `docs/STRUCTURE.md`、`docs/plans/`、`scripts/changelog.py` 或 `.meta/scripts/changelog.py` 等 init-agent-docs 产物 → **复用不重铺**（docs/ 层级保留；CHANGELOG 脚本以已装者为准，不重复装 changelog_append.py 的等价物）；未装 → 先调用 init-agent-docs 完成打底再继续。

**知识库路径统一是叠加步骤，不是删除“从机残骸”**：所有维护脚本最终在 `.meta/scripts/`，记忆内容与索引入口只在 `.meta/memory/`。打底后、拷 KB 脚本前按 [refs/agent-docs-layout.md](refs/agent-docs-layout.md) 迁移：保留 changelog/audit/check_all；文档维护器改名 docs_maintain.py 接入 KB maintain，索引只由 memory_index.py 写。仅处理已识别资产；不删除整个 `.agents/`。已有 completed 库明确请求此布局迁移时只走该参考流程，不重跑隐私/API 问答、不覆盖库内用户规则。

### 可选 worktree 协作桥接

知识库可选择沿用 init-agent-docs 的 Git worktree 隔离与主工作树占用声明约定。仅在目标是 Git 仓库且用户选择启用时安装或保留；新库把通用 helper 放在 `.meta/scripts/worktree_task.py`，提供 8 个动作（`create` / `check` / `integrate` / `cleanup` / `list` / `claim-main` / `release-main` / `main-claim`）。任务身份仅来自 Git worktree 列表与 `task/*` 分支名；Git common-dir 下只存共享原子锁与 `main-claim.json`。KB 不复制第二份 runtime，不安装强制 hook，也不要求固定任务分类、宿主专属分支前缀或自动通知。具体动作与参数以 helper 的 `--help` 为准。

不启用、没有 Git，或当前任务只是只读检索/评审时，不创建 task worktree 或写入占用状态。创建 task worktree 时 `--name` 与 `--owner` 成对传入或同时省略，分支名（`task/<id>[-<slug>]`）与目录（`wt-<id>[-<slug>]`）由 helper 稳定派生；主工作树写入前用 `claim-main --owner ...` 声明占用，整合前用 `release-main --owner ...` 释放，`integrate` 仅在无活动 `main-claim` 且通过快进校验时合入。

此工具提供本地 Git 隔离与占用硬护栏，但不构成系统安全边界：不会拦截直接文件编辑、手工 Git、其他 clone 或任意 GUI 操作。`owner` 字段只是声明，不是身份认证；`main-claim` 状态仅在同一 Git common-dir 的 linked worktree 间共享，不会自动同步到另一 clone 或设备。主从权限始终优先于 task owner：从机仅可只读执行 `list` / `check` / `main-claim`；从机不得创建 task worktree、修改 `main-claim` 或执行 Git 写命令。worktree 只隔离 Git 文件，不隔离共享数据库、派生维护和部署；需要这些写入时由主机在主工作树整合并统一执行。


---

## 何时使用

- 用户要把一个普通 Obsidian vault 变成"自维护的 second brain"
- 仓库以 office 文档（xlsx/docx/pptx/pdf）为主、.md 很少——本技能的提取管线正对此设计
- 关键词：「装知识库系统」「bootstrap harness」「init knowledge base」「给这个仓库建立维护体系」
- **不适用全量 bootstrap**：vault 已有 `.meta/scripts/maintain*.py`（已装过 harness）；已有库仅在用户请求时执行上述布局迁移。纯代码仓库不适用。

---

## 设计原则

### Occam 始终生效，bootstrap 期容忍必要建立

**落地**：Occam **全程生效**。bootstrap 期**容忍建立必要的体系结构**（维护管线、检索、入口），完成后新增结构仍须自证必要。`bootstrap_completed_at` 标记必要建立期的结束边界。

### 批量任务必须并行（效率红线）

维护是批量任务，串行执行是效率事故。三层落地：

1. **脚本内并发**：embed.py / summarize.py / extract_office.py 内部用 ThreadPool 并发 API 调用与文件提取，并发数 `.env:MAINTAIN_CONCURRENCY`（默认 6，配合 `API_RATE_LIMIT_MS` 限速；对应层未启用时这些步骤不跑）
2. **编排层并行**：maintain.py 在两层都启用时把 embed 与 summarize 作为独立进程并行跑
3. **agent 手工批量维护**：批量整理/迁移/打标/改写多文件时，**必须**并行派发（subagent 或一次响应内的并行工具调用），禁止逐文件串行循环——此准则同时写入目标仓库 AGENTS.md

### 记忆系统双硬约束

1. **任务执行前必查记忆**：目标仓库 AGENTS.md（`refs/agents-template.md`）必须包含硬要求——agent 接到任务后、执行前必须先检索记忆系统（读 `.meta/memory/MEMORY.md` 索引 + `ask.py --scope memory`），查看可能的过往经验；唯一豁免是用户当次明确说不必要。
2. **MEMORY.md 索引脚本自动维护**：`.meta/scripts/memory_index.py` 扫描 `.meta/memory/` 下所有子目录与 `docs/problems/bugfix/` 经验自动重建 MEMORY.md 索引标记段（`<!-- memory-index:start/end -->`，段内禁止手改），自动展示各分类当前全部条目。三层强制：maintain.py 每次维护自动重建 + pre-commit hook 拦截过期索引提交 + `--check` 模式供校验。**不允许依赖 agent 记得手动维护索引。**

### 三自循环 ↔ CONSTITUTION 第零部四目标

本技能安装的三组机制对应知识库的四目标（CONSTITUTION 第零部原文：永无死角 / 不重复建设 / 自维护 / 知识复利）：

| 机制组 | 对应目标 | 安装件 |
|--------|---------|--------|
| **自维护**（maintain 管线） | 自维护 + 永无死角 + 不重复建设 | maintain.py + extract_office/embed/build_links/index/graph/bm25/knowledge_map/health（+ 可选 summarize） |
| **自沉淀**（memory + 衰减） | 知识复利 | memory/ 结构 + dream.py + synthesize.py |
| **自运行**（入口 + 检索 + 硬约束） | 全部四目标的跨会话可持续 | AGENTS.md + ask.py + pre-commit hook + converge 治理 |

---

## 执行流程

### Phase 0 · 体检 + 交互式引导（read-only，必跑）

**不动手，先诊断；随后一步一步引导用户做决策。**

1. **规模/结构扫描**：笔记数、**office 文档数（含 .doc/.xls/.ppt 老格式单列）**、目录树、git 状态
2. **隐私嗅探**：按 `refs/privacy-scan.patterns`（中英双覆盖 + 凭据 glob + 高熵串）扫描，结果带到下方决策点 ① 供用户人审。**生成 `.meta/rules/category-privacy.md`**（来自 `templates/category-privacy.md`，用户确认后的隐私目录填入 `## 隐私目录` 段——必须裸目录名列表项，对齐 `_load_privacy_dirs()` 消费契约）。散落敏感文件（不在隐私目录）建议**移入隐私子目录**后纳入保护——当前机制仅目录级生效，散落文件不自动排除。隐私目录内的 office 文档同样不提取
3. **taxonomy 推断**：用 `refs/taxonomy-inference.prompt`（LLM 主推断 + 规则后置兜底）提议分类，写入 `docs/TAXONOMY.md`（draft，决策点 ② 由用户定夺）
4. **仓库画像**：按 `refs/maturity-rubric.md`（全量化机械可判）产出画像与提示（小库自重提示、office 为主提示、老格式转存提示、参数阈值建议）——**只影响提示与参数，不裁剪安装范围**
5. **分步引导用户决策（核心）**：扫描完成后，按下方决策点**逐个**向用户提问——每点给出**具体选项**让用户在上下文里即时决策，每步有记录、可中断可续。优先用 harness 的交互提问机制（opencode `question` 工具 / Claude Code AskUserQuestion）；无交互机制时退化为在对话中列出编号选项请用户回复序号。**一次只问一个决策点**，等用户回答后再进下一个：

   | 步骤 | 决策点 | 呈现内容 | 选项 |
   |------|--------|---------|------|
   | ① | 隐私目录 | 嗅探命中文件清单 + 建议隐私目录 | a) 全部纳入 b) 仅部分（用户点名）c) 都不纳入；散落文件追加一问：移入隐私子目录 / 保持现状仅记录 |
   | ② | taxonomy | TAXONOMY.md 草案要点 | a) 接受 b) 用户口述调整、agent 改后再示 c) 留 draft 以后再定 |
   | ③ | 安装模式与增强层 | 仓库画像 + rubric 提示 + 能力对照（见"安装模式"节）；**只问用户现有条件**，不催配 key | a) **默认（推荐）**：内置本地嵌入 + BM25 + 图谱，零 API b) 换更高质量嵌入：本机已有 Ollama / 任意 OpenAI 兼容端点，或云端（换后重建向量库）c) 加 LLM 层：DeepSeek 摘要/精排（需 key，可选）d) 极小仓库先攒内容、暂不 bootstrap |
   | ④ | 增强层配置（**仅决策点 ③ 选了 b/c 时进行；默认无需任何配置**） | 语义层：`EMBED_BASE_URL` + `EMBED_MODEL`（本地端点无需 key）；LLM 层：`DEEPSEEK_API_KEY`；可选：PRIMARY_HOST | a) 现在提供（贴入对话或用户自行编辑 .env）b) 稍后自填（骨架照装、对应层保持关闭，不影响其余能力） |
   | ⑤ | worktree 协作桥接（**仅目标是 Git 仓库时询问**） | 说明 Git worktree 隔离与主工作树占用声明用途 | a) 启用：复用 init-agent-docs helper，按需为复杂/并行任务建 task worktree b) 不启用：保留普通 agent 工作流，不安装 helper；非 Git 仓库自动记为不适用 |

   每步的用户选择**记录进 `kb-bootstrap-decisions.md`**（定位：**执行记录**——记下每步选项、选择结果）。
6. **所有适用决策点都有结果 → 直接进入 Phase 1**。中断后重触发时沿用 `kb-bootstrap-decisions.md` 中已确认的选择；只在用户明确要求重选时才重新询问，不重复确认已有协作桥接选择。

### Phase 1 · 骨架 + 检索 + Office 提取（必装）

**这是"容忍必要体系建立"的核心阶段。** 装完即可自维护。

1. 检测/调用 `init-agent-docs` 打底（见上方"已装检测与复用"）。覆盖 AGENTS.md 前，读取现有协作路由、用户改写的任务范围约定，以及 `kb-bootstrap-decisions.md` 中对此桥接的选择；沿用已确认的决定，不重复询问。先保留已启用协作约定及用户调整，再与本模板合并。
2. 先完成 [agent-docs 布局迁移](refs/agent-docs-layout.md)，再拷贝**全部 KB 脚本**到 `.meta/scripts/`（见下方脚本清单——一次拷齐；已有 changelog.py 时不再部署等价 changelog_append.py；保留 docs_maintain.py/audit.py/check_all.py；各层共用同一套脚本）。用户选择启用协作桥接且目标是 Git 仓库时，从新版 init-agent-docs `assets/scripts/` 安装或复用唯一的 `worktree_task.py`；不另建 helper 副本或 hook
3. `templates/env.example` → `.env`（默认 `HARNESS_MODE=lite` + `EMBED_PROVIDER=auto` 走内置本地模型，无需再配；要换嵌入端点填 `EMBED_BASE_URL` + `EMBED_MODEL`；要 LLM 层设 `full` 并填 key；另有 PRIMARY_HOST + MAINTAIN_CONCURRENCY + office 并发/超时 + 阈值）
4. 安装 Python 依赖：**优先用离线套装**（`assets\scripts\15-install-kb-deps.ps1`，含 wheel + 内置嵌入模型缓存，固定 Python 3.13）；没有套装时才 `pip install -r requirements.txt`
5. `docs/CONSTITUTION.md`（来自 `refs/constitution-template.md`）+ `docs/TAXONOMY.md`（Phase 0 草案）+ `AGENTS.md`（来自 `refs/agents-template.md`，`bootstrap_status: in_progress`）——AGENTS.md 是唯一入口，不生成任何同步副本。仅当目标是 Git 仓库且用户选择启用时，加入或合并可选协作段并安装唯一 helper；否则移除模板中的可选协作段，不装该 helper。替换模板时保留已确认的协作约定、用户自定义任务范围规则及其决定记录，不静默丢弃。
6. 仅在目标仓库有 Git 时，安装 `.githooks/pre-commit`（来自 `refs/pre-commit-template`，统一版）并接线：`git config core.hooksPath .githooks`。没有 Git 时不安装或接线 hook，也不安装协作 helper；Git Bash 缺失时按下方前置缺失交互处理。
7. 按 TAXONOMY.md 创建内容目录（如日记/复盘/收件箱等，名称因用户偏好而异）。若用户使用 Obsidian daily-notes 插件，配置 `.obsidian/daily-notes.json` 指向对应目录

> **安装中的交互引导**：机械步骤遇前置缺失（无 Python / 无 Git Bash / 所选层的端点或 key 未配 / office 依赖装不上）不当场失败，给用户选项：a) 现在补齐 b) 跳过该件继续（明确标注后果，如 hook 不生效、LLM 步骤暂不可跑、office 内容暂不可检索）c) 中止，处理后再续。选择记录进 `kb-bootstrap-decisions.md`。

### Phase 1.5 · 宿主护栏（可选，用户确认后装）

把本库已声明的**机械路径规则**硬化到宿主动作侧：隐私目录读禁、从机写禁、CHANGELOG 编辑重定向、compact 哨兵注入、触发词检索提醒。**护栏只拦机械路径规则，不拦判断**；状态侧（pre-commit）仍是地基，本层是加固而非替代，软规则原文保留。

**前提**：Phase 1 已装（`host_guard.py` 随脚本集落位）；Phase 0 隐私目录已经用户确认。

1. **宿主探测**：检测本机实际使用的 agent 宿主（opencode / Claude Code / Codex / 其他），逐个向用户确认后只生成对应适配；一个都不使用则跳过本 Phase。
2. **策略落盘**：`templates/host-write-policy.json` → `.meta/rules/host-write-policy.json`。目标库 AGENTS.md 若已有演进版从机禁改清单，以该清单为准转写，不用模板覆盖。
3. **按宿主生成适配**（全部指向同一脚本 `.meta/scripts/host_guard.py`，单一策略源）：
   - **opencode**：`templates/host-guardrails/opencode.json` → 项目根 `opencode.json`（已存在则只合并 `permission` 段）；`templates/host-guardrails/kb-guard.js` → `.opencode/plugins/kb-guard.js`
   - **Claude Code**：`templates/host-guardrails/claude-settings-fragment.json` 合并进 `.claude/settings.json`（保留既有 `permissions.allow` 等全部键，追加 `deny` 与 `hooks`）
   - **Codex**：`templates/host-guardrails/codex-hooks.json` → `.codex/hooks.json`；提示用户需在 Codex `/hooks` 审查信任后生效
   - **其他宿主**：无适配则如实保持软约束，不伪造覆盖
4. **验证**：`python .meta/scripts/host_guard.py compact-context` 输出 SessionStart JSON；`simulate-host` 模拟从机判定；构造隐私路径 payload 实测 deny/allow（用例形态参考 vault 侧 `.meta/tests/test_host_guard.py`）。
5. **登记**：AGENTS.md 写明哪些宿主已接线、哪些仍软约束（compact 哨兵段措辞绑定实测结果，实测失败的宿主保持"技术债"原措辞）。

### Phase 2 · 自沉淀（必装）

- 仅补齐 `templates/memory-scaffold/` 中缺失文件到 `.meta/memory/`，不得覆盖 Phase 1 已迁并的自由文本、角色与记忆（MEMORY.md + user/ + feedback/ 骨架；其余子目录按需涌现，不预建。知识库内容由用户笔记目录承担，记忆只存 agent 专属物）
- MEMORY.md 的"当前记忆条目"索引段由 `memory_index.py` 自动维护（见上方"记忆系统双硬约束"）；拷入后立即跑 `python .meta/scripts/memory_index.py` 生成首版索引
- 填充 AGENTS.md 的「项目记忆」内联段：活跃主题待 vault 使用后自然涌现，bootstrap 阶段写"待补"；用户称呼 / 关键偏好需向用户补问一句后填入（勿留未替换的方括号占位符、勿臆造）
- Phase 2 的脚本（dream / semantic_lint / synthesize / knowledge_map / bm25_index）已随 Phase 1 拷入，此处无脚本动作

### Phase 3 · 治理（必装）

- 建 `.meta/converge/{active,done}/` + README charter（来自 `refs/converge-readme-template.md`）——**只此一个治理目录**，不建 deliberations/audit
- 生成 `.meta/governed-files.txt`（来自 `refs/governed-files-template.txt`，按目标仓库实际治理边界增删）
- `.env` 设 `CONVERGE_DIR=.meta/converge`（converge SKILL 产物路径绑定；hook 拦截误落 `.converge/`）
- 治理叙事统一为单一 plan 生命周期：`起草 plan → converge 收敛 → 按授权执行 → fresh verifier 验收 → 用户确认后归档 docs/plans/completed/（status: done）`；治理文档改动走 ultraverge（详见 `refs/governance-multi-axis.md`）

### Bootstrap 完成判定

1. `python .meta/scripts/maintain.py --full` **退出码 0**（默认：extract_office + embed + build_links + index + graph + bm25 + knowledge_map + health + dream；LLM 层另含 summarize）。退出码 3 = 语义层缺失/失败，bootstrap 不可判完成
2. `health-report.md` 与 `.index/manifest.md` 生成；健康报告的「语义层」段无 ⚠️（覆盖率为 0 或配置不一致都算未完成）；若仓库有 office 文档，`.meta/office-extracts/` 有产出
3. **人审门（交互式）**：agent 引导用户跑一条真实问题检索——正常情况用 `ask.py "query"` 语义检索；若它报告语义层不可用（临时降级 BM25），说明 bootstrap **还不能判完成**，先按提示修复（office 为主的仓库应验证能命中 office 内容），然后给出选项请用户判定检索质量：a) 可接受 → 完成 b) 需调整 → 排查后重试 c) 暂不确认（保持 `in_progress`，原因记录进 plan）
4. → 把 AGENTS.md 的 `bootstrap_status` 改 `completed`、`bootstrap_phase` 改 `phase3`，填 `bootstrap_completed_at: <date>`

> **只有启用的层才受配置门控**：默认安装无需任何 key，但**语义层必须真的建出向量**（`maintain.py --full` 退出码 0 且 health 语义段无告警）才可判 completed。选了 LLM 层但 key 稍后自填的仓库，`summarize` 必然失败——把那层的开关关掉（`HARNESS_MODE=lite`）即可完成 bootstrap，并在 kb-bootstrap-decisions.md 注明原因，key 配好后再开。

> **诚实标注**：maturity-rubric 的画像提示全部机械可判；bootstrap 完成判定含一次人审门（用户确认检索质量）——前者是信息呈现、后者决定体系是否就绪。

**此后 Occam 继续生效，新增结构须自证必要。**

---

## 脚本清单（全量安装，一次拷齐）

### 管线入口与检索

| 脚本 | 功能 | 依赖 | CLI |
|------|------|------|-----|
| `maintain.py` | 维护主入口（全管线编排，见文件头）；语义层默认启用（内置模型），LLM 层看 `HARNESS_MODE=full` + key；语义层缺失/失败时退出码 3 | 编排下方各脚本 | `--full`（重建本地派生件，不强制重嵌入）/ `--no-git` / `--semantic-lint` / `--skip-changelog` |
| `ask.py` | 语义检索 / 查重 / 孤儿 / 反链 / 图遍历；**语义层运行时不可用（库空/维度不符/端点断/索引损坏）时自动降级**：默认查询与 --check 走 BM25（含 --scope 过滤），两者都不可用时退出码 2；--deep/--rerank 需 LLM 层；有嵌入库时**低置信自动升级**（top-1<阈值自动 hybrid/deep/rerank 合并，统计写 `.meta/escalation-stats.jsonl`，阈值 `.env:ESCALATION_THRESHOLD` 可调） | embeddings.sqlite + graph.json + bm25 索引；embeddings 端点（查询）、DeepSeek（--rerank/--deep） | `"query"` / `--check` / `--orphans` / `--backlinks` / `--neighbors` / `--path` / `--deep` / `--bm25` / `--hybrid` / `--rerank` / `--scope` / `--decay` / `--save` |
| `whoami.py` | 模型/provider 自检（两层识别：harness 路由 codex/claude-code/opencode → 内部 provider）；`--frontmatter` 输出溯源 YAML（agent/model/generated_at，`WHOAMI_AGENT` env 可显式指定宿主） | 本地只读，零 API | 无参 / `--frontmatter` |

### 提取与索引层

| 脚本 | 功能 | 依赖 | CLI |
|------|------|------|-----|
| `extract_office.py` | office 文档（.docx/.xlsx/.pptx/.pdf）纯文本提取 → `.meta/office-extracts/` sidecar；增量按 hash；孤儿清理；老格式登记 `_legacy-formats.md`；**子进程并行**，单文件超过 `OFFICE_EXTRACT_TIMEOUT_S`（默认 120s）即终止跳过并登记 `_timeouts.md` | python-docx/openpyxl/python-pptx/pypdf（缺装时对应格式跳过并提示） | `--full` |
| `embed.py` | 笔记 + office 提取件 + memory → embeddings.sqlite；**默认内置本地模型（fastembed），可切任意 OpenAI 兼容 `/embeddings`**；HTTP 按 `EMBED_BATCH` 切请求批并发、内置扁平批量；两者都按 `EMBED_FILE_BATCH`/`EMBED_CHUNK_BATCH` 分批并**立即写库**；先探针后写库；配置变更**拒绝增量并提示 `--full`**，`--full` 先写临时表再原子换表；大文件闸门：超阈值且无 allow 决定的文件硬性跳过（llm_skip 决定同时清除历史向量） | HTTP 端点 或 fastembed | `--full` |
| `summarize.py` | 每篇生成摘要 / tag sidecar；**跨文件并发 API**（LLM 层；`HARNESS_MODE=full` 时由 maintain 调用）；大文件闸门同 embed | DeepSeek | `--full` |
| `build_index.py` | `.index/manifest.md` + 分类清单 + topics.md（office 文档以 📎 标记纳入） | 本地 | 无参 |
| `calibrate_thresholds.py` | 只读采样当前嵌入库的相似度分布，输出建议的 `LINKS_SIM_THRESHOLD` / `LINKS_HIGH_THRESHOLD` / `SEMANTIC_THRESHOLD` / `ESCALATION_THRESHOLD`（换模型或换语料后跑一次） | 本地（需 embeddings.sqlite） | `--limit` / `--json` |
| `build_links.py` | `.meta/links/` 语义关联 sidecar（纯余弦 + wikilink，**无 LLM**）；属语义层，按 content_hash+模型+阈值增量；graph 的 semantic 边读它（`--orphans` 只看用户手写链接） | 本地（需 embeddings.sqlite） | `--full` |
| `build_graph.py` | 全局图谱 graph.json（wikilink + semantic 边）+ 断链报告 | 本地（semantic 边来自 build_links 产物；语义层关闭时只有 wiki/md 边） | 无参 |
| `bm25_index.py` | BM25 稀疏倒排索引（自实现算法、零 API、零嵌入库依赖；**增量构建**——词频缓存只重算变化文件，并行分词，lite/full 均可用）→ `.meta/bm25_index.pkl.gz`（+ 文档词频缓存 `bm25_doc_cache.pkl.gz`；`--rebuild` 忽略缓存全量重建） | 本地 | `--build` / `--rebuild` / `--query "xxx" --top-k N` |

### 派生报告与自沉淀层

| 脚本 | 功能 | 依赖 | CLI |
|------|------|------|-----|
| `knowledge_map.py` | 消费 graph.json → `.meta/knowledge-map.md`（god nodes / 跨域连接 / 社区 / 偏差信号） | 本地零 API | 无参 |
| `health_report.py` | `.meta/health-report.md`（孤儿/收件箱/稀疏分类 + **语义层覆盖率/模型/新鲜度/挂起与超时** 告警，阈值 env 可调） | 本地（收件箱建议需 LLM 层，未启用时只列文件） | 无参 |
| `dream.py` | 记忆活性扫描 + 衰减预警 + 唤醒检测 → `.meta/dream-report.md`（所有建议均为提案不自动执行） | 本地零 API（git + frontmatter） | `--full`（预留） |
| `semantic_lint.py` | P0 断链 / P1 孤儿概念·过时标记（本地启发式）/ P2 矛盾检测（DeepSeek，**LLM 层**）→ `.meta/semantic-lint-report.md` | P2 需 DeepSeek | 默认 quick / `--deep` / 回归测试类 `--check-*` |
| `synthesize.py` | 主题合成（多篇聚合生成综述）→ `.meta/syntheses/`（**LLM 层**；未启用时由 agent 直接读文兼任） | DeepSeek | `--theme` / `--scope`（glob，逗号分隔）/ `--prompt` / `--max-notes` |

### 支撑脚本（穷举）

| 脚本 | 功能 |
|------|------|
| `common.py` | 公共库：env 加载 / API client（含跨线程全局限速）/ 扫描与排除 / office 与并发配置 / 链接解析 / git 包装 |
| `check_gov_consistency.py` | 治理边界一致性校验（SSOT / AGENTS 明线 / hook GOV_PATTERNS 三方比对；bootstrap 同批 staged 时豁免）；pre-commit 第 9 段在治理边界文件变更时自动触发 |
| `docs_maintain.py` / `audit.py` / `check_all.py` / `changelog.py` | 从 init-agent-docs 源重定位；文档审计、定期提醒、完工检查和 CHANGELOG；docs_maintain 委托唯一 memory_index |
| `memory_index.py` | MEMORY.md 记忆索引自动重建（硬约束，索引标记段禁止手改）；`--check` 供 pre-commit 校验 |
| `detect_renames.py` | 重命名检测（git + hash 双通道），迁移伴生元数据 |
| `check_sidecar_sources.py` | 校验/修复 `.meta/{summaries,links,tags}/` sidecar 的 source 字段 |
| `host_guard.py` | 宿主护栏动态门禁（**Phase 1.5 选用**，不配 hook 则为无害闲置）：pre-tool 判定（隐私读禁/从机写禁/编辑重定向）、prompt 触发词提醒、compact-context 注入文本、simulate-host dry-run；策略源 `.meta/rules/host-write-policy.json` |
| `inbox_scan.py` | 收件箱扫描 + LLM 归类建议（health_report 调用） |
| `changelog_append.py` | 旧库兼容追加器；已有 agent-docs changelog.py 时不部署 |
| `check_plan_status.py` | docs/plans 路径（`active/`、`completed/`、`not_planned/`，兼容 `done/`）与 frontmatter status 一致性（pre-commit 调用） |

> maintain.py 的 workflow frequency 步骤依赖 `search_sessions.py`（vault 特化脚本，bundle 不含）——缺失时自动跳过并告警。

---

## 幂等性

每次重跑不破坏已装部分，按下表分类处理：

| 文件类 | 重跑行为 | 理由 |
|--------|---------|------|
| `.meta/scripts/*.py`（含 KB maintain.py） | **覆盖**（以 skill bundle 为准） | 脚本是机械产物，以 skill bundle 为准覆盖 |
| 旧 `scripts/` / `.agents/memory/` | 按迁移清单逐项处理；成功后重跑只校验，不重复追加记忆；未知内容保留并报告 | 备份、冲突处理及验证见 agent-docs-layout.md；不删除 `.agents/skills` 等其他资产 |
| `refs/` 模板（constitution/agents/pre-commit/converge-readme） | **覆盖** | 模板随 skill 演进 |
| `.env` | **跳过若存在** | 含用户 API key / PRIMARY_HOST / 阈值，覆盖会清掉用户配置 |
| `.meta/rules/category-privacy.md` | **跳过若存在** | Phase 0 嗅探结果 + 用户人审，不可覆盖 |
| `.meta/governed-files.txt` | **跳过若存在** | 用户可能已按本仓库边界增删 |
| `docs/TAXONOMY.md` | **跳过若存在** | Phase 0 推断 + 用户确认 |
| `docs/CHANGELOG.md` | **追加不覆盖** | 历史记录 |
| `.meta/rules/host-write-policy.json` | **跳过若存在** | 用户可能已按本仓库边界增删（Phase 1.5） |
| 宿主配置文件（`opencode.json` / `.claude/settings.json` / `.codex/hooks.json`） | **合并式更新** | 保留既有权限/hook 规则，只补缺失段（Phase 1.5） |
| `.opencode/plugins/kb-guard.js` | **覆盖** | 以 skill bundle 为准（Phase 1.5） |
| `.meta/converge/` `.meta/memory/` 内容 | **跳过若存在**（只补缺失的 README/骨架） | 持久型，含收敛证据与记忆 |
| `.meta/office-extracts/` | 由 extract_office.py 按 hash 增量管理 | 派生型 |
| `AGENTS.md` | **条件覆盖**（in_progress rerun 按 Phase 续装；completed 升级保留内联段及已存在的可选协作约定，详见下方升级流程；legacy 旧版兼容直接覆盖为新模板，详见旧版兼容段） | 内联段含用户自由文本不可重建；可选协作约定按现状合并或保留；其余为机械规则随模板；AGENTS.md 是唯一入口，不存在同步副本 |

### bootstrap_phase 状态记录

`bootstrap_phase` 是**断点记录**（非解锁门控），枚举 ∈ `{phase0, phase1, phase2, phase3}`，表示"已完成到哪个 Phase"。全部完成 = `phase3` + `bootstrap_status: completed`。

重复触发本技能时：
0. **先检测仓库根是否存在旧名 `kb-bootstrap-plan.md`**（v0.5.7 前生成）→ 存在则重命名为 `kb-bootstrap-decisions.md`（决策记录不丢，仅文件名变更）
1. 读目标仓库 AGENTS.md 的 `bootstrap_status` 与 `bootstrap_phase`，按下方分支处理：
- `bootstrap_status: in_progress` → 从 `bootstrap_phase` 的下一个 Phase 续装
- `bootstrap_status: completed` → 提示"harness 已装，是否重跑维护 / 升级 bundle 脚本？"；若升级 bundle 脚本，AGENTS.md 按幂等表"条件覆盖"处理：① 读旧 AGENTS.md 的「项目记忆」内联段已填内容并备份；② 识别已安装的可选 worktree 协作段及其用户调整；③ 用新模板覆盖 AGENTS.md；④ 把备份内联段填回新模板对应位置，并合并或保留已安装的协作约定（勿重置为占位符或静默移除用户调整）；⑤ 若新模板内联段结构跨版本变更（增删字段/改名），须显式给出迁移映射，不能机械填回
- 无 `bootstrap_status` 字段（首次）→ 从 Phase 0 开始
- 已存在 `kb-bootstrap-decisions.md` 决策记录（Phase 0 中断过）→ 复述历史决策，给用户选项：沿用 / 重新决策

### 旧版兼容

检测到旧版安装（目标仓库存在 `maintain-lite.py` 或 `.meta/deliberations/`）时，按"旧版迁移"路径处理：

- 检测到旧版脚本 `maintain-lite.py` → 替换为 `maintain.py`，拷入全量脚本
- `.meta/deliberations/` 或 audit 历史内容原地保留，在其 README 标注"只读证据归档，禁止新增"
- 治理统一走 `.meta/converge/`
- `AGENTS.md`：**覆盖前提示用户**——"检测到旧版 AGENTS.md，将被新模板覆盖（含「项目记忆」内联段占位符）；非内联段区域的用户定制将丢失（git 历史可恢复）"；确认后覆盖为新模板，按 Phase 2 流程填充内联段。**覆盖安全依据**：旧版无内联段→不丢失内联段自由文本（独立第三场景，非 in_progress 语义）；非内联段区域按模板权威原则覆盖（幂等表理由栏）；不走 completed 升级流程的①备份③填回④迁移映射（旧版无内联段，跳过这三步）
- **三文件体系清退（旧版安装必做）**：检测到 `CLAUDE.md` / `GEMINI.md` 同步副本、`.meta/scripts/sync_agents.py` 或 pre-commit hook 内嵌的三文件 MD5 检查段时，按以下步骤清退：
  1. 直接删除两副本（内容已并入 AGENTS.md 模板；单一入口下保留只会制造两处维护与漂移）；
  2. 删除 `.meta/scripts/sync_agents.py`（治理一致性校验由 `check_gov_consistency.py` 承接）；
  3. hook 换装：用 `refs/pre-commit-template` 统一版**整体覆盖**目标仓库 `.githooks/pre-commit`（不再含 MD5 段——脚本已删而 hook 仍调用会卡死提交路径），覆盖后 `bash -n .githooks/pre-commit` 自检，并确认 `git config core.hooksPath .githooks` 已接线；
  4. 验证：`python .meta/scripts/check_gov_consistency.py --check-gov-consistency` 应通过（治理边界三方一致；此后 pre-commit 第 9 段自动把关）；
  5. 优先用 `.meta/scripts/changelog.py add`（旧库仅有 changelog_append.py 时沿用它）记一条迁移条目（标题建议「三文件体系清退，统一 AGENTS.md 单一入口」，正文列出删除的文件与 hook 变更）；
  6. **习惯废止告知（须向用户明说）**：原「改完 AGENTS.md 跑 sync_agents.py」的习惯动作随脚本删除而废止——AGENTS.md 是唯一入口，保存即生效，无任何同步步骤。

---

## 产物清单（bootstrap 完成后）

```
target-vault/
├── AGENTS.md                                   ← 本技能模板（唯一入口；图书管理员准则 + 并行准则 + 维护管线 + 提交策略）
├── <内容目录>/                               ← 按 TAXONOMY.md 定义（日记/复盘/收件箱/工作流等,名称因用户偏好而异）
├── .env                                      ← 从 env.example 填充（默认内置本地嵌入 + BM25；换端点/加 LLM 层时才改）
├── .githooks/pre-commit                      ← 统一版 hook（plan status + converge 路径 + GOV 提醒 + key/隐私 + MEMORY.md 索引一致性）
├── docs/
│   ├── STRUCTURE.md / plans/ / CURRENT.md    ← init-agent-docs 打底（若装）
│   ├── CONSTITUTION.md                       ← 三元原则 + 持久性四型 + 多轴门控 + converge 生命周期
│   ├── TAXONOMY.md                           ← Phase 0 推断（draft）
│   └── CHANGELOG.md                          ← 首条 bootstrap 记录
├── .meta/
│   ├── scripts/                              ← 全量脚本集（见脚本清单；host_guard.py 随集落位，Phase 1.5 接线启用）
│   ├── office-extracts/                      ← office 文档提取 sidecar（派生型）
│   ├── memory/                               ← MEMORY.md + user/role.md + workflows/ 等（Phase 2）
│   ├── converge/{active,done}/ + README.md   ← 治理（Phase 3，唯一治理目录）
│   ├── governed-files.txt                    ← 治理文档机械 SSOT
│   └── rules/
│       ├── category-privacy.md               ← Phase 0 嗅探结果（模板：templates/category-privacy.md）
│       ├── retrieval.md                      ← ask.py 优先
│       └── host-write-policy.json            ← Phase 1.5（可选）：从机禁改清单机器可读 SSOT
├── opencode.json / .claude/settings.json / .codex/hooks.json / .opencode/plugins/kb-guard.js   ← Phase 1.5（可选）：按宿主探测生成的动作侧护栏适配
└── kb-bootstrap-decisions.md                      ← Phase 0 决策记录（交互引导各步的选择与结果）
```

---

## 约束与诚实标注

1. **不健全仓库与 harness 的矛盾**：`notes+office_docs<10` 时提示"自重可能超内容，建议先攒内容"；全量安装对空数据优雅退化，但 LLM 步骤产出价值有限。
2. **office 提取质量边界**：提取只保纯文本——表格结构简化为 `|` 分隔、图表/图片内文字/批注不保真；.doc/.xls/.ppt 老格式不支持（登记提示转存）。检索命中提取件时永远指向源文件；**编辑改源文件，不改提取件**。
3. **并发与限速**：MAINTAIN_CONCURRENCY 默认 6，与 API_RATE_LIMIT_MS 配合。触发 API 限流时先调低并发，不要关并发。
4. **单用户单机假设**：本 harness 默认单机模型。多用户/多机仓库的主从边界需在 Phase 0 画像提示后由用户确认。
5. **嵌入与 LLM 分层**：语义层是基础能力，默认用**内置本地模型**（fastembed + `bge-small-zh-v1.5`，450 字分块、512 维，零 API），也可切 **HTTP**（任意 OpenAI 兼容 `/embeddings`：Ollama、vLLM、LM Studio、智谱、OpenAI，同一份代码，维度实测）。`EMBED_PROVIDER=auto` 下 HTTP 配置优先、否则内置；`base_url`/`model` 只填一个会报配置错误（不静默降级）；`off` 仅排障用。换来源后必须 `embed.py --full`。LLM 层是可选增强（`HARNESS_MODE=full` + `DEEPSEEK_API_KEY`），**DeepSeek 模型一律选最新版的次等模型**（当前 `deepseek-v4-flash`）：维护管线是批量任务，旗舰 pro 类模型成本不划算。
6. **能力边界与恢复**：语义层是基础能力——未就绪时 `ask.py` 临时降级 BM25 并说明原因，但 `maintain` 会以**退出码 3** 结束、`health-report` 会报覆盖率与告警，bootstrap 不可判定完成。恢复三步：① `pip install fastembed`（离线套装用 `15-install-kb-deps.ps1`）；② 模型缓存缺失/损坏 → `-ForceModelCopy` 或设 `HF_ENDPOINT` 后联网首跑；③ 换过模型/维度 → `embed.py --full`。默认模型 `bge-small-zh-v1.5` 是中文 512-token 模型：英文/多语库召回会偏弱，建议切多语模型或 HTTP 端点。<br>LLM 层关闭时摘要/tag 不存在、`ask.py` 结果无摘要行——这是取舍；office 超大/损坏文件超时会跳过并登记（`.meta/office-extracts/_timeouts.md` / `_failures.md`），非 UTF-8 笔记登记 `.meta/embed-skipped.md`，均不影响其余文件与基础索引。
7. **大文件闸门（任一层启用时）**：超 `LARGE_FILE_THRESHOLD_MB`（默认 1MB，`.env` 可调）的单文件被脚本硬性挡在 embed/summarize 外并挂起（`.meta/large-files-pending.md`），**BM25/图谱/manifest 索引照常覆盖该文件**；维护完成后 agent 必须逐个向用户询问 5 个处理选项，决定落 `.meta/rules/large-file-policy.md`（allow / llm_skip / skip；切分与删除完成后删除对应行即自动出清单）。`llm_skip` 会在下次维护清除该文件历史向量。闸门只拦 LLM 管线，不拦 agentic grep/glob 阅读——agent 按用户要求仍可人工读改大文件。
8. **治理机制不复刻**：converge 机制权威源是全局 converge SKILL；本技能只在目标仓库落路径绑定与 charter 指针。
9. **快照维护债**：bundle 脚本会随上游演进 drift。维护者 drift 检测说明见 README「for maintainers」小节。
10. **宿主护栏边界**：Phase 1.5 的护栏只拦机械路径规则（隐私目录、从机禁改清单、编辑重定向），不拦判断类规则（如检索策略选择）；它是状态侧（pre-commit）的加固层而非替代，软规则原文保留；各宿主 fail-open/fail-closed 行为以 `host_guard.py` docstring 与接线文件注释为准，不宣称为安全边界。
11. **派生件与版本控制**：可重建的二进制派生件——`embeddings.sqlite`（含 `-wal/-shm`）、`bm25_index.pkl.gz`、`bm25_doc_cache.pkl.gz`——由 `maintain.py` 自动写入 `.gitignore` 受管块并从 git 索引移除（工作区文件保留），避免每轮维护把几十 MB 二进制堆进历史。`file-dates.json` / 图谱 / links / manifest 等文本派生件仍入库。多机同步走网盘/rsync；从机只读且需要检索时，由主机重建后再同步。

---

## 触发后的第一步

收到触发指令后，**先读**：
1. 目标仓库根是否有 `.meta/scripts/`（已装？旧版 maintain-lite？）、是否有 `kb-bootstrap-decisions.md`（历史决策记录？）
2. `refs/maturity-rubric.md` + `refs/privacy-scan.patterns` + `refs/taxonomy-inference.prompt`
3. 然后从 Phase 0 开始（扫描 → 分步交互引导用户决策）。
