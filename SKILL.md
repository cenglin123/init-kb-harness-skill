---
name: init-kb-harness
description: Initialize a self-maintaining knowledge-base harness on an Obsidian vault (possibly immature/empty, or office-document-heavy). Aliases: IKB / ikb. Default install is zero-API and includes local embeddings (fastembed/bge-small-zh) plus BM25, graph, and office extraction; semantic recall is baseline, not optional. The LLM layer (summaries/rerank) is opt-in via DeepSeek. Embedding endpoint may be switched to any OpenAI-compatible /embeddings (local Ollama/vLLM or cloud). Scaffolds maintenance pipeline + office extraction + memory + converge governance as a full install (Phase 1→2→3 all required). Use when the user says "IKB" / "ikb" / "给这个仓库装知识库系统" / "bootstrap kb harness" / "init knowledge base" / wants to turn a plain Obsidian vault into a self-maintaining second-brain. Existing harnesses use only the explicit agent-docs layout migration when requested; do not rebootstrap them.
---

# init-kb-harness（别名：IKB / ikb）· 知识库 Harness 引导器

给**任意** Obsidian 仓库（含不健全/空仓库、office 文档为主的仓库）从零建立一套可自维护、自沉淀、自运行的 harness 框架。**Phase 1→2→3 全量必装**，一次 bootstrap 顺序完成。

---

## 安装模式：语义层是基本盘，LLM 层按需

| 层 | 默认 | 依赖 | 能力 | 代价 |
|---|---|---|---|---|
| **基础层** | ✅ 开 | 无 | BM25 倒排（`bm25_index.py`）、图谱/反链、office 提取（子进程并行 + 超时）、索引/健康/记忆/治理、agentic grep/glob | 0 |
| **语义层** | ✅ 开 | 内置 `fastembed` + `bge-small-zh-v1.5`（离线套装预置；也可切任意 OpenAI 兼容 `/embeddings`：Ollama/vLLM/LM Studio/云端） | 向量召回、`--hybrid`、语义关联 sidecar（`build_links.py`，纯余弦无 LLM）、`ask.py` 低置信自动升级 | 全量建库一次，之后按 hash 增量 |
| **LLM 层** | ❌ 关 | `HARNESS_MODE=full` + `DEEPSEEK_API_KEY`（选新版次等模型，如 `deepseek-v4-flash`） | 摘要 / tag / `synthesize` / `semantic_lint` P2 / `--rerank` / `--deep` | 每篇一次、按 hash 增量；**不参与排序** |

- **默认 = 基础层 + 语义层（零 API）**。摘要不进 BM25/向量索引，仅提升命中可读性与综述素材。
- **配置变更纪律**：切换模型/维度/分块后 `embed.py` 拒绝增量并提示 `embed.py --full`（临时表全量成功后原子切换；建议随后跑 `calibrate_thresholds.py`）。`maintain.py --full` 只重建本地派生件，不强制重嵌入。
- **模式推断**：未写 `HARNESS_MODE` 且嵌入库无 `embed_meta` 时按 `full`；有 meta 一律按 `lite`。
- **大文件闸门（硬约束）**：超 `LARGE_FILE_THRESHOLD_MB`（默认 1MB）的单文件不进嵌入/LLM 管线，挂起 `.meta/large-files-pending.md`（BM25/图谱/manifest 照常覆盖）；维护后须向用户询问 5 项处理决定并记录 `.meta/rules/large-file-policy.md`。

---

## 与 init-agent-docs 的关系

**标准执行序：先用 `init-agent-docs` 打底，再叠加本技能。** 两者共享「仓库即唯一事实源 / `AGENTS.md` 作目录渐进披露 / plan-as-handoff / Occam + Bitter Lesson / 软约束靠文档、硬约束靠工具」哲学。

| 层 | `init-agent-docs` 提供 | 本技能叠加 |
|----|----------------------|-----------|
| 文档层级 | `docs/STRUCTURE.md`、`plans/`、脚本化 CHANGELOG | `docs/CONSTITUTION.md`、`docs/TAXONOMY.md` |
| 入口 | `AGENTS.md` 单一入口 | **以 `refs/agents-template.md` 覆盖合并**（保留既有协作约定与用户定制） |
| 脚本与记忆 | `changelog.py` / `audit.py` / `check_all.py` | `.meta/scripts/` 全套维护管线 + `.meta/memory/` 唯一记忆入口 |
| 治理 | ultraverge 判定原则 | `.meta/converge/` 单一生命周期 + `governed-files.txt` SSOT |

- **复用与路径统一**：已有 `docs/STRUCTURE.md`、`docs/plans/`、`scripts/changelog.py` 或 `.meta/scripts/changelog.py` 时复用不重铺；打底后、拷 KB 脚本前按 [refs/agent-docs-layout.md](refs/agent-docs-layout.md) 统一脚本与记忆路径（不删 `.agents/` 其他资产）。已有 `completed` 库明确请求布局迁移时只走该参考流程，不重跑问答或覆盖用户规则。
- **可选 worktree 协作桥接**：仅在 Git 仓库且用户启用时，于 `.meta/scripts/worktree_task.py` 安装/复用唯一 helper（8 个动作：`create`/`check`/`integrate`/`cleanup`/`list`/`claim-main`/`release-main`/`main-claim`；具体参数见 `--help`）。`--name`/`--owner` 成对传入或省略；主工作树写入前 `claim-main`、整合前 `release-main`。此为本地 Git 隔离护栏而非安全边界：从机仅可执行只读动作（`list`/`check`/`main-claim`），共享库与派生维护由主机在主工作树统一执行。

---

## 何时使用

- 将普通 Obsidian vault 建成自维护 second brain；尤其适合 office 文档（`.docx`/`.xlsx`/`.pptx`/`.pdf`）为主的仓库。
- 触发词：「装知识库系统」「bootstrap harness」「init knowledge base」「给这个仓库建立维护体系」。
- **不适用全量 bootstrap**：已有 `.meta/scripts/maintain*.py`（仅按需走布局迁移或升级）或纯代码仓库。

---

## 核心设计原则与硬约束

1. **Occam 全程生效**：bootstrap 期容忍必要结构建立，`bootstrap_completed_at` 标定边界，此后新增结构须自证必要。
2. **批量任务必须并行**：脚本内并发（`MAINTAIN_CONCURRENCY` 默认 6 + `API_RATE_LIMIT_MS`）+ `maintain.py` 进程级并行 + agent 批量改写必须并行派发，禁止逐文件串行。
3. **记忆系统双硬约束**：① 执行任务前必查 `.meta/memory/MEMORY.md` + `ask.py --scope memory`（除非用户当次豁免）；② `MEMORY.md` 索引标记段（`<!-- memory-index:start/end -->`）由 `memory_index.py` 自动维护，pre-commit 拦截过期索引。
4. **三自循环 ↔ CONSTITUTION 四目标**：自维护（`maintain.py` 全管线）对应自维护/永无死角/不重复建设；自沉淀（`memory/` + `dream.py` + `synthesize.py`）对应知识复利；自运行（`AGENTS.md` + `ask.py` + pre-commit + converge）保障跨会话可持续。
5. **能力边界与恢复**：
   - 小库（`notes+office_docs<10`）提示自重风险但不裁剪安装；默认单机假设，多机主从边界在 Phase 0 确认。
   - office 提取仅保纯文本（老格式 `.doc`/`.xls`/`.ppt` 登记提示转存；超时/失败登记跳过），命中提取件时永远编辑源文件。
   - 语义层未就绪时 `ask.py` 临时降级 BM25，但 `maintain.py` 退出码 3 且 bootstrap 不可判完成；恢复顺序：① 装 `fastembed`（或跑 `15-install-kb-deps.ps1`）② 补模型缓存（`-ForceModelCopy` / `HF_ENDPOINT`）③ 换配置后跑 `embed.py --full`。中文 512-token 默认模型对多语库偏弱，建议切多语或 HTTP 端点。
   - 宿主护栏（Phase 1.5）仅硬化机械路径规则，是 pre-commit 的加固而非安全边界；converge 机制权威源在全局 converge SKILL，本技能只落路径绑定与 charter。
   - 二进制派生件（`embeddings.sqlite*`、`bm25_*.pkl.gz`）由 `maintain.py` 写入 `.gitignore` 受管块并移出 git 索引；文本派生件仍入库。

---

## 执行路由（Phase 0 → 3）

详细步骤、决策选项、完成判定、幂等表、旧版迁移与产物清单见按需参考文档：

| 阶段 / 主题 | 目标 | 按需读取 |
|---|---|---|
| **Phase 0 · 体检与交互引导**（read-only，必跑） | 规模/隐私/taxonomy/画像扫描，逐项确认 5 个决策点并写入 `kb-bootstrap-decisions.md` | [refs/bootstrap-phases.md](refs/bootstrap-phases.md) + [refs/maturity-rubric.md](refs/maturity-rubric.md) + [refs/privacy-scan.patterns](refs/privacy-scan.patterns) + [refs/taxonomy-inference.prompt](refs/taxonomy-inference.prompt) |
| **Phase 1 · 骨架 + 检索 + Office**（必装） | `init-agent-docs` 复用与布局迁移、全量脚本与依赖安装、模板落盘、Git hook 接线 | [refs/bootstrap-phases.md](refs/bootstrap-phases.md) + [refs/agent-docs-layout.md](refs/agent-docs-layout.md) + [refs/agents-template.md](refs/agents-template.md) + [refs/constitution-template.md](refs/constitution-template.md) + [refs/pre-commit-template](refs/pre-commit-template) |
| **Phase 1.5 · 宿主护栏**（可选） | 按宿主探测生成 opencode / Claude Code / Codex 动作侧护栏，全部指向 `host_guard.py` | [refs/bootstrap-phases.md](refs/bootstrap-phases.md) |
| **Phase 2 · 自沉淀**（必装） | 补齐 `.meta/memory/` 骨架、生成首版 `MEMORY.md` 索引、填充 `AGENTS.md` 项目记忆内联段 | [refs/bootstrap-phases.md](refs/bootstrap-phases.md) |
| **Phase 3 · 治理**（必装） | 建 `.meta/converge/{active,done}/`、`governed-files.txt`、`.env` 路径绑定与单一 plan 生命周期 | [refs/bootstrap-phases.md](refs/bootstrap-phases.md) + [refs/converge-readme-template.md](refs/converge-readme-template.md) + [refs/governed-files-template.txt](refs/governed-files-template.txt) + [refs/governance-multi-axis.md](refs/governance-multi-axis.md) |
| **完成判定 / 幂等 / 续装 / 旧版兼容 / 产物清单** | `maintain.py --full` 退出码 0 + 健康无告警 + 用户检索人审门 → `bootstrap_status: completed` | [refs/bootstrap-phases.md](refs/bootstrap-phases.md) |
| **全量脚本清单（25 个 KB 脚本）** | 管线入口、提取索引、派生自沉淀、支撑脚本的依赖与 CLI 规格 | [refs/script-inventory.md](refs/script-inventory.md) |

---

## 触发后的第一步

收到触发指令后，**先读**：
1. 目标仓库根是否有 `.meta/scripts/`（已装 / 旧版 `maintain-lite.py`）、`AGENTS.md` 的 `bootstrap_status` / `bootstrap_phase`、以及 `kb-bootstrap-decisions.md`（或旧名 `kb-bootstrap-plan.md`）。
2. `refs/bootstrap-phases.md` 的 Phase 0（或断点对应节）+ `refs/maturity-rubric.md` + `refs/privacy-scan.patterns` + `refs/taxonomy-inference.prompt`。
3. 从 Phase 0 开始扫描并分步交互引导用户决策（续装或迁移按 `refs/bootstrap-phases.md` 对应分支执行）。
