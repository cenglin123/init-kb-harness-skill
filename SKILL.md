---
name: init-kb-harness
description: Initialize a self-maintaining knowledge-base harness on an Obsidian vault (possibly immature/empty). Scaffolds maintenance pipeline + semantic retrieval + memory + governance as a tiered install (Foundation/Memory/Governance). Use when the user says "给这个仓库装知识库系统" / "bootstrap kb harness" / "init knowledge base" / wants to turn a plain Obsidian vault into a self-maintaining second-brain. Not for vaults that already have a `.meta/scripts/` harness.
---

# init-kb-harness · 知识库 Harness 引导器

给**任意** Obsidian 仓库（含不健全/空仓库）从零建立一套可自维护、自沉淀、自运行的 harness 框架。分层安装，按成熟度解锁。

> **与 init-agent-docs 的关系**：本技能**调用** init-agent-docs 铺 docs/ 层级（STRUCTURE.md 等渐进披露文档），但 **AGENTS.md 一律以本技能的 `refs/agents-template.md` 为准**（图书管理员准则 + bootstrap 状态 + 维护管线）——init-agent-docs 若已生成 AGENTS.md，由本技能模板覆盖。两者 AGENTS.md 叙事不同（init-agent-docs 是通用 agent 协作目录，本技能是知识库维护准则），非叠加关系。

---

## 何时使用

- 用户要把一个普通 Obsidian vault 变成"自维护的 second brain"
- 关键词：「装知识库系统」「bootstrap harness」「init knowledge base」「给这个仓库建立维护体系」
- **不适用**：vault 已有 `.meta/scripts/maintain*.py`（已装过 harness）；纯代码仓库（非知识库）

---

## 设计原则

### Occam 始终生效，bootstrap 期容忍必要建立

用户约束：「Occam 可以不那么严格（因为就是要给它建立一个体系）」。

**落地**：Occam **始终生效**——不存在"暂停/恢复"的状态机。bootstrap 期只是**容忍建立必要的体系结构**（维护管线、检索、入口），完成后新增结构仍须自证必要。本技能设一个 `bootstrap_completed_at` **完成标记**（状态记录，非机械门控），帮 agent 知道"必要建立期"是否结束。

### 三自循环 ↔ CONSTITUTION 第零部四目标

本技能安装的三组机制对应知识库的四目标（CONSTITUTION 第零部原文：永无死角 / 不重复建设 / 自维护 / 知识复利）：

| 机制组 | 对应目标 | 安装件 |
|--------|---------|--------|
| **自维护**（maintain 管线） | 自维护 + 永无死角 + 不重复建设 | maintain-lite.py + embed/summarize/index/graph/health |
| **自沉淀**（memory + 衰减） | 知识复利 | memory/ 结构 + dream.py（Phase 2） |
| **自运行**（入口 + 检索 + 硬约束） | 全部四目标的跨会话可持续 | AGENTS.md + ask.py + pre-commit hook |

"三自"只是这三组机制的简称，非新理论；上表与四目标的映射是教学性归类，非因果推导依据。

---

## 执行流程

### Phase 0 · 体检（read-only，必跑）

**不动手，先诊断。** 产出 `kb-bootstrap-plan.md` 到目标仓库根。

1. **规模/结构扫描**：笔记数、目录树、git 状态
2. **隐私嗅探**：按 `refs/privacy-scan.patterns`（中英双覆盖 + 凭据 glob + 高熵串）扫描，结果写入 plan 供用户人审。**生成 `.meta/rules/category-privacy.md`**（来自 `templates/category-privacy.md`，嗅探结果填入 `## 隐私目录` 段——必须裸目录名列表项，对齐 `_load_privacy_dirs()` 消费契约）。散落敏感文件（不在隐私目录）建议**移入隐私子目录**后纳入保护——当前机制仅目录级生效，散落文件不自动排除
3. **taxonomy 推断**：用 `refs/taxonomy-inference.prompt`（LLM 主推断 + 规则后置兜底）提议分类，写入 `docs/TAXONOMY.md`（draft，待用户确认）
4. **成熟度判定**：按 `refs/maturity-rubric.md`（全量化机械可判）建议装到哪个 Phase
5. **输出 plan**：含嗅探结果 + taxonomy 草案 + Phase 建议 + 用户确认栏

**强制暂停**：Phase 0 完成后向用户呈现 `kb-bootstrap-plan.md`，在 plan 顶部写入字段 `phase_0_confirmed: false`。**只有当用户勾选为 `phase_0_confirmed: true` 后才进 Phase 1**。重复触发时读此字段判断是否已确认。

### Phase 1 · 自维持骨架（Foundation · 拆 1a/1b）

**这是"容忍必要体系建立"的核心阶段。** 装完即可自维护。

#### Phase 1a（最小集，`notes<30` 适用）

调用 `init-agent-docs` 铺 docs/ 层级（STRUCTURE.md；若它生成 AGENTS.md，随后由本技能模板覆盖）→ 拷贝最小脚本集到 `.meta/scripts/`：
- `common.py`（EXCLUDE_DIRS 动态化）+ `maintain-lite.py`（精简版）+ `embed.py` + `summarize.py` + `build_index.py` + `ask.py`
- `.env.example` → `.env`（填 API key + PRIMARY_HOST + ARCHIVE_MARKERS）
- `docs/CONSTITUTION.md`（来自 `refs/constitution-template.md`）+ `docs/TAXONOMY.md`（Phase 0 草案）+ `AGENTS.md`（来自 `refs/agents-template.md`，`bootstrap_status: in_progress`）
- 跑 `python .meta/scripts/sync_agents.py`（生成 CLAUDE.md/GEMINI.md）
- 安装 `.githooks/pre-commit`（来自 `refs/pre-commit-template`）并**接线**：`git config core.hooksPath .githooks`（不配则 hook 静默永不触发）。依赖 Git Bash（Git for Windows 自带；纯 GitHub Desktop/TortoiseGit 无 bash 环境需另装）。

#### Phase 1b（`30≤notes≤200` 追加）

补：`health_report.py`（阈值已参数化）+ `build_graph.py` + `inbox_scan.py`（prompt 已去特定仓库示例）+ `check_sidecar_sources.py` + `detect_renames.py` + `whoami.py` + `changelog_append.py`

#### Bootstrap 完成判定

1. `python .meta/scripts/maintain-lite.py --full` 成功（embed + summarize + index + 可选 graph/health 全跑通）
2. `health-report.md` 生成（1b）或 `build_index` 产出（1a）
3. **人审门**：用户在 `kb-bootstrap-plan.md` 勾选 `[x] 会话可用确认`（判据：已用 ask.py 成功检索到 ≥1 条相关结果，且用户主观确认检索质量可接受）
4. → 把 AGENTS.md 的 `bootstrap_status` 改 `completed`，填 `bootstrap_completed_at: <date>`，**重跑 `sync_agents.py`**（任何 AGENTS.md 改动都必须重跑，否则三文件 MD5 漂移、pre-commit hook 拦死后续 commit）

> **诚实标注**：maturity-rubric 的 Phase 解锁判定全部机械可判；bootstrap 完成判定含一次人审门（用户确认检索质量）——两者故意不同，前者决定装多少、后者决定体系是否就绪。

**此后 Occam 继续生效（一直生效），新增结构须自证必要。**

### Phase 2 · 自沉淀（Memory · 按需）

**触发**（maturity-rubric，量化）：`notes>200 OR orphan_ratio>0.3`。

- 拷贝 `templates/memory-scaffold/` 到 `.meta/memory/`（MEMORY.md + role.md + workflows/）
- 补脚本：`dream.py` + `semantic_lint.py` + `synthesize.py` + `knowledge_map.py` + `bm25_index.py`
- **换回完整 `maintain.py`**（解锁 dream/synthesis_index/knowledge_map/bm25 步骤；maintain-lite 退役）
- 跑 `maintain.py` 验证

### Phase 3 · 治理（Governance · 按需）

**触发**（maturity-rubric，量化）：`has_host_field≥2` 或 `git_author_count≥2`。

- 引用独立 converge SKILL 建 `.meta/converge/` `.meta/deliberations/` `.meta/audit/` 三 charter
- pre-commit hook 升级到完整版（GOV 文档变更检测 + plan status + converge 路径纠正）
- CONSTITUTION 升级到完整版（六部 + 第五点五部治理体系详述，见 `refs/governance-multi-axis.md`）

---

## 幂等性

每个 Phase 可独立追加，不破坏已装部分：
- 已装 Phase 1a 的仓库，later 装 1b：**覆盖**拷贝 1b 脚本到 scripts/（已存在的覆盖），重跑 `maintain-lite.py --full`
- 已装 Phase 1b 的仓库，later 装 Phase 2：补脚本 + 换完整 maintain.py + 重跑
- 重跑同一 Phase：按下方「重跑行为（按文件分类）」表执行，不重复建目录

### 重跑行为（按文件分类）

| 文件类 | 重跑行为 | 理由 |
|--------|---------|------|
| `scripts/*.py` + maintain-lite.py | **覆盖**（以 skill bundle 为准） | 脚本是机械产物，以 skill bundle 为准覆盖 |
| `refs/` 模板（constitution/agents/pre-commit） | **覆盖** | 模板随 skill 演进 |
| `.env` | **跳过若存在** | 含用户 API key / PRIMARY_HOST / 阈值，覆盖会清掉用户配置 |
| `.meta/rules/category-privacy.md` | **跳过若存在** | Phase 0 嗅探结果 + 用户人审，不可覆盖 |
| `docs/TAXONOMY.md` | **跳过若存在** | Phase 0 推断 + 用户确认 |
| `docs/CHANGELOG.md` | **追加不覆盖** | 历史记录 |
| `AGENTS.md` / `CLAUDE.md` / `GEMINI.md` | **覆盖**（经 sync_agents.py） | 由 AGENTS 模板生成 |

**Phase 1→2 切换**：装 Phase 2 时**删除** `maintain-lite.py`、拷贝完整 `maintain.py`，并把 AGENTS.md 的 `bootstrap_phase` 从 `1a`/`1a+1b` 改为 `+2`、常用脚本段 `maintain-lite.py` 引用改为 `maintain.py`，重跑 sync_agents.py。

### bootstrap_phase 状态机

`bootstrap_phase` 字段枚举 ∈ `{1a, 1a+1b, +2, +3}`。转换清单：

| 当前 | 目标 | 触发 | 脚本动作 | frontmatter 字段 |
|------|------|------|----------|------------------|
| `1a` | `1a+1b` | 用户完成 Phase 1b | 覆盖拷贝 1b 脚本 | `bootstrap_phase` 改 `1a+1b` |
| `1a+1b` | `+2` | 用户开始 Phase 2 | 删 maintain-lite + 拷 maintain.py | `bootstrap_phase` 改 `+2`；跑 sync_agents |
| `+2` | `+3` | 用户开始 Phase 3 | 建 `.meta/{converge,deliberations,audit}/` + 装 `check_plan_status.py` + hook 换完整版 + CONSTITUTION 升级 | `bootstrap_phase` 改 `+3` |

完成判定：每个转换后跑对应 Phase 的 `maintain[-lite] --full` 验证幂等。

重复触发本技能时，先读目标仓库 AGENTS.md 的 `bootstrap_status` 与 `bootstrap_phase`：
- `bootstrap_status: in_progress` + `bootstrap_phase` 标识中断处（如读到 `1a` → 补装到 `1a+1b`；读到 `1a+1b` 且 notes>200 → 建议 Phase 2）
- `bootstrap_status: completed` → 提示"harness 已装（phase=<值>），是否升级 Phase？"
- 无 `bootstrap_status` 字段（首次） → 从 Phase 0 开始

---

## 产物清单（Phase 1 完整）

```
target-vault/
├── AGENTS.md / CLAUDE.md / GEMINI.md        ← init-agent-docs + 维护准则段
├── .env                                      ← 从 env.example 填充
├── .githooks/pre-commit                      ← 精简版 hook
├── docs/
│   ├── CONSTITUTION.md                       ← 精简版（三元原则 + 持久性四型 + 治理体系指针）
│   ├── TAXONOMY.md                           ← Phase 0 推断（draft）
│   └── CHANGELOG.md                          ← 首条 bootstrap 记录
├── .meta/
│   ├── scripts/                              ← Phase 1a 或 1a+1b 脚本集
│   └── rules/
│       ├── category-privacy.md               ← Phase 0 嗅探结果（模板：templates/category-privacy.md）
│       └── retrieval.md                      ← ask.py 优先
└── kb-bootstrap-plan.md                      ← Phase 0 产出（保留作执行记录）
```

---

## 约束与诚实标注

1. **不健全仓库与 harness 的矛盾**：`notes<10` 时提示"自重可能超内容，建议先攒内容"；Phase 1a 是最小缓解，非消除。
2. **单用户单机假设**：本 harness 默认单机模型。多用户/多机仓库的主从边界、治理需 Phase 3 重新评估。
3. **API 后端**：Phase 1 默认绑死 DeepSeek+智谱（`common.py` client 无工厂）。换后端需改 `common.py`。
4. **快照维护债**：bundle 脚本会随上游演进 drift。维护者 drift 检测说明见 README「for maintainers」小节。

---

## 触发后的第一步

收到触发指令后，**先读**：
1. 目标仓库根是否有 `.meta/scripts/`（已装？）
2. `refs/maturity-rubric.md` + `refs/privacy-scan.patterns` + `refs/taxonomy-inference.prompt`
3. 然后从 Phase 0 开始。
