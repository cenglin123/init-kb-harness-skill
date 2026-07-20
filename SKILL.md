---
name: init-kb-harness
description: Initialize a self-maintaining knowledge-base harness on an Obsidian vault (possibly immature/empty, or office-document-heavy). Scaffolds maintenance pipeline (parallel batch jobs) + semantic retrieval (md + docx/xlsx/pptx/pdf extraction) + memory + converge governance as a full install (Phase 1→2→3 all required). Use when the user says "给这个仓库装知识库系统" / "bootstrap kb harness" / "init knowledge base" / wants to turn a plain Obsidian vault into a self-maintaining second-brain. Not for vaults that already have a `.meta/scripts/` harness.
---

# init-kb-harness · 知识库 Harness 引导器

给**任意** Obsidian 仓库（含不健全/空仓库、office 文档为主的仓库）从零建立一套可自维护、自沉淀、自运行的 harness 框架。**Phase 1→2→3 全量必装**，一次 bootstrap 顺序完成。

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
| 入口 | AGENTS/CLAUDE/GEMINI 三文件同步 | **AGENTS.md 以本技能 `refs/agents-template.md` 为准覆盖**（图书管理员准则 + 维护管线 + bootstrap 状态） |
| 脚本 | changelog.py / agent_links.py / audit.py | `.meta/scripts/` 全套维护管线（同步机制统一用本技能 sync_agents.py） |
| 治理 | ultraverge 判定原则 | converge 单一生命周期落地（`.meta/converge/` + governed-files SSOT） |

### 已装 init-agent-docs 的检测与复用

Phase 1 开始前检测目标仓库：已有 `docs/STRUCTURE.md`、`docs/plans/`、`scripts/changelog.py` 等 init-agent-docs 产物 → **复用不重铺**（docs/ 层级保留；CHANGELOG 脚本以已装者为准，不重复装 changelog_append.py 的等价物）；未装 → 先调用 init-agent-docs 完成打底再继续。

---

## 何时使用

- 用户要把一个普通 Obsidian vault 变成"自维护的 second brain"
- 仓库以 office 文档（xlsx/docx/pptx/pdf）为主、.md 很少——本技能的提取管线正对此设计
- 关键词：「装知识库系统」「bootstrap harness」「init knowledge base」「给这个仓库建立维护体系」
- **不适用**：vault 已有 `.meta/scripts/maintain*.py`（已装过 harness）；纯代码仓库（非知识库）

---

## 设计原则

### Occam 始终生效，bootstrap 期容忍必要建立

**落地**：Occam **全程生效**。bootstrap 期**容忍建立必要的体系结构**（维护管线、检索、入口），完成后新增结构仍须自证必要。`bootstrap_completed_at` 标记必要建立期的结束边界。

### 批量任务必须并行（效率红线）

维护是批量任务，串行执行是效率事故。三层落地：

1. **脚本内并发**：embed.py / summarize.py / extract_office.py 内部用 ThreadPool 并发 API 调用与文件提取，并发数 `.env:MAINTAIN_CONCURRENCY`（默认 6，配合 `API_RATE_LIMIT_MS` 限速）
2. **编排层并行**：maintain.py 把 embed 与 summarize 作为独立进程并行跑
3. **agent 手工批量维护**：批量整理/迁移/打标/改写多文件时，**必须**并行派发（subagent 或一次响应内的并行工具调用），禁止逐文件串行循环——此准则同时写入目标仓库 AGENTS.md

### 三自循环 ↔ CONSTITUTION 第零部四目标

本技能安装的三组机制对应知识库的四目标（CONSTITUTION 第零部原文：永无死角 / 不重复建设 / 自维护 / 知识复利）：

| 机制组 | 对应目标 | 安装件 |
|--------|---------|--------|
| **自维护**（maintain 管线） | 自维护 + 永无死角 + 不重复建设 | maintain.py + extract_office/embed/summarize/index/graph/bm25/knowledge_map/health |
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
   | ③ | 全量安装确认 | 仓库画像 + rubric 提示 + 参数阈值建议 | a) 确认全量安装（Phase 1+2+3）b) 极小仓库先攒内容、暂不 bootstrap |
   | ④ | API 配置 | env 字段清单（必填：DeepSeek/智谱 key；可选：PRIMARY_HOST） | a) 现在提供（贴入对话或用户自行编辑 .env）b) 稍后自填（骨架照装，LLM 步骤暂不可跑） |

   每步的用户选择**记录进 `kb-bootstrap-plan.md`**（定位：**执行记录**——记下每步选项、选择结果、日期）。
6. **四个决策点全部有结果 → 直接进入 Phase 1**。中断后重触发时读 plan 中的决策记录，问用户"沿用历史决策 / 重新决策"。

### Phase 1 · 骨架 + 检索 + Office 提取（必装）

**这是"容忍必要体系建立"的核心阶段。** 装完即可自维护。

1. 检测/调用 `init-agent-docs` 打底（见上方"已装检测与复用"；若它生成 AGENTS.md，随后由本技能模板覆盖）
2. 拷贝**全部脚本**到 `.meta/scripts/`（见下方脚本清单——一次拷齐，不分批）
3. `templates/env.example` → `.env`（填 API key + PRIMARY_HOST + MAINTAIN_CONCURRENCY + OFFICE_EXTRACT_EXTS + 阈值）
4. `pip install -r requirements.txt`（含 office 提取依赖 python-docx/openpyxl/python-pptx/pypdf）
5. `docs/CONSTITUTION.md`（来自 `refs/constitution-template.md`）+ `docs/TAXONOMY.md`（Phase 0 草案）+ `AGENTS.md`（来自 `refs/agents-template.md`，`bootstrap_status: in_progress`）
6. **紧接着**跑 `python .meta/scripts/sync_agents.py`（生成 CLAUDE.md/GEMINI.md）——步骤 5+6 视为一个原子动作：中间中断会造成三文件 MD5 不一致、pre-commit hook 拦截提交；续装时检测到不一致先补跑 sync_agents.py
7. 安装 `.githooks/pre-commit`（来自 `refs/pre-commit-template`，统一版）并**接线**：`git config core.hooksPath .githooks`（不配则 hook 静默永不触发）。依赖 Git Bash（Git for Windows 自带；纯 GitHub Desktop/TortoiseGit 无 bash 环境需另装）

> **安装中的交互引导**：机械步骤遇前置缺失（无 Python / 无 Git Bash / API key 未填 / office 依赖装不上）不当场失败，给用户选项：a) 现在补齐 b) 跳过该件继续（明确标注后果，如 hook 不生效、LLM 步骤暂不可跑、office 内容暂不可检索）c) 中止，处理后再续。选择记录进 `kb-bootstrap-plan.md`。

### Phase 2 · 自沉淀（必装）

- 拷贝 `templates/memory-scaffold/` 到 `.meta/memory/`（目录树与 MEMORY.md 自述一致：MEMORY.md + user/role.md + workflows/README.md + feedback/、project/、reference/ 空目录）
- Phase 2 的脚本（dream / semantic_lint / synthesize / knowledge_map / bm25_index）已随 Phase 1 拷入，此处无脚本动作

### Phase 3 · 治理（必装）

- 建 `.meta/converge/{active,done}/` + README charter（来自 `refs/converge-readme-template.md`）——**只此一个治理目录**，不建 deliberations/audit
- 生成 `.meta/governed-files.txt`（来自 `refs/governed-files-template.txt`，按目标仓库实际治理边界增删）
- `.env` 设 `CONVERGE_DIR=.meta/converge`（converge SKILL 产物路径绑定；hook 拦截误落 `.converge/`）
- 治理叙事统一为单一 plan 生命周期：`起草 plan → converge 收敛 → 按授权执行 → fresh verifier 验收 → 用户确认后 done`；治理文档改动走 ultraverge（详见 `refs/governance-multi-axis.md`）

### Bootstrap 完成判定

1. `python .meta/scripts/maintain.py --full` 成功（extract_office + embed ∥ summarize + index + graph + bm25 + knowledge_map + health + dream 全跑通）
2. `health-report.md` 与 `.index/manifest.md` 生成；若仓库有 office 文档，`.meta/office-extracts/` 有产出
3. **人审门（交互式）**：agent 引导用户用 ask.py 跑一条真实问题检索（office 为主的仓库应验证能命中 office 内容），然后给出选项请用户判定检索质量：a) 可接受 → 完成 b) 需调整 → 排查后重试 c) 暂不确认（保持 `in_progress`，原因记录进 plan）
4. → 把 AGENTS.md 的 `bootstrap_status` 改 `completed`、`bootstrap_phase` 改 `phase3`，填 `bootstrap_completed_at: <date>`，**重跑 `sync_agents.py`**（任何 AGENTS.md 改动都必须重跑，否则三文件 MD5 漂移、pre-commit hook 拦死后续 commit）

> **API key 未配置时不得标 completed**：Phase 0 决策点 ④ 选了"稍后自填"的仓库，`maintain.py --full` 的 LLM 步骤必然失败——保持 `in_progress` 并在 kb-bootstrap-plan.md 注明阻塞原因，key 配好后重跑完成判定。

> **诚实标注**：maturity-rubric 的画像提示全部机械可判；bootstrap 完成判定含一次人审门（用户确认检索质量）——前者是信息呈现、后者决定体系是否就绪。

**此后 Occam 继续生效，新增结构须自证必要。**

---

## 脚本清单（全量安装，一次拷齐）

### 管线入口与检索

| 脚本 | 功能 | 依赖 | CLI |
|------|------|------|-----|
| `maintain.py` | 维护主入口（全管线编排，见文件头） | 编排下方各脚本 | `--full` / `--no-git` / `--semantic-lint` / `--skip-changelog` |
| `ask.py` | 语义检索 / 查重 / 孤儿 / 反链 / 图遍历；**低置信自动升级**（top-1<0.6 自动 hybrid/deep/rerank 合并，统计写 `.meta/escalation-stats.jsonl`） | embeddings.sqlite + graph.json + bm25 索引；Zhipu embed（查询）、DeepSeek（--rerank/--deep） | `"query"` / `--check` / `--orphans` / `--backlinks` / `--neighbors` / `--path` / `--deep` / `--bm25` / `--hybrid` / `--rerank` / `--scope` / `--decay` / `--save` |
| `whoami.py` | 模型/provider 自检（两层识别：harness 路由 codex/claude-code/opencode → 内部 provider） | 本地只读，零 API | 无参 / `--frontmatter`（溯源 YAML） |

### 提取与索引层

| 脚本 | 功能 | 依赖 | CLI |
|------|------|------|-----|
| `extract_office.py` | office 文档（.docx/.xlsx/.pptx/.pdf）纯文本提取 → `.meta/office-extracts/` sidecar；增量按 hash；孤儿清理；老格式登记 `_legacy-formats.md`；ThreadPool 并发 | python-docx/openpyxl/python-pptx/pypdf（缺装时对应格式跳过并提示） | `--full` |
| `embed.py` | 笔记 + office 提取件 + memory → embeddings.sqlite；**跨文件并发 API** | Zhipu embedding | `--full` |
| `summarize.py` | 每篇生成摘要/tag/关联 sidecar；**跨文件并发 API** | DeepSeek + embeddings | `--full` |
| `build_index.py` | `.index/manifest.md` + 分类清单 + topics.md（office 文档以 📎 标记纳入） | 本地 | 无参 |
| `build_graph.py` | 全局图谱 graph.json（wikilink + semantic 边）+ 断链报告 | 本地（消费 embeddings） | 无参 |
| `bm25_index.py` | BM25 稀疏索引（自实现零外部依赖）→ `.meta/bm25_index.json.gz` | embeddings.sqlite | `--build` / `--query "xxx" --top-k N` |

### 派生报告与自沉淀层

| 脚本 | 功能 | 依赖 | CLI |
|------|------|------|-----|
| `knowledge_map.py` | 消费 graph.json → `.meta/knowledge-map.md`（god nodes / 跨域连接 / 社区 / 偏差信号） | 本地零 API | 无参 |
| `health_report.py` | `.meta/health-report.md`（孤儿/收件箱/稀疏分类告警，阈值 env 可调） | 本地 | 无参 |
| `dream.py` | 记忆活性扫描 + 衰减预警 + 唤醒检测 → `.meta/dream-report.md`（所有建议均为提案不自动执行） | 本地零 API（git + frontmatter） | `--full`（预留） |
| `semantic_lint.py` | P0 断链 / P1 孤儿概念·过时标记（本地启发式）/ P2 矛盾检测（DeepSeek）→ `.meta/semantic-lint-report.md` | P2 需 DeepSeek | 默认 quick / `--deep` / 回归测试类 `--check-*` |
| `synthesize.py` | 主题合成（多篇聚合生成综述）→ `.meta/syntheses/` | DeepSeek | `--theme` / `--scope`（glob，逗号分隔）/ `--prompt` / `--max-notes` |

### 支撑脚本（穷举）

| 脚本 | 功能 |
|------|------|
| `common.py` | 公共库：env 加载 / API client（含跨线程全局限速）/ 扫描与排除 / office 与并发配置 / 链接解析 / git 包装 |
| `sync_agents.py` | AGENTS.md → CLAUDE.md/GEMINI.md 三文件同步（MD5 校验） |
| `detect_renames.py` | 重命名检测（git + hash 双通道），迁移伴生元数据 |
| `check_sidecar_sources.py` | 校验/修复 `.meta/{summaries,links,tags}/` sidecar 的 source 字段 |
| `inbox_scan.py` | 收件箱扫描 + LLM 归类建议（health_report 调用） |
| `changelog_append.py` | CHANGELOG 条目脚本化插入 |
| `check_plan_status.py` | docs/plans 路径与 frontmatter status 一致性（pre-commit 调用） |

> maintain.py 的 workflow frequency 步骤依赖 `search_sessions.py`（作者 vault 特化件，bundle 不含）——缺失时自动跳过并告警，属预期降级。

---

## 幂等性

每次重跑不破坏已装部分，按下表分类处理：

| 文件类 | 重跑行为 | 理由 |
|--------|---------|------|
| `scripts/*.py`（含 maintain.py） | **覆盖**（以 skill bundle 为准） | 脚本是机械产物，以 skill bundle 为准覆盖 |
| `refs/` 模板（constitution/agents/pre-commit/converge-readme） | **覆盖** | 模板随 skill 演进 |
| `.env` | **跳过若存在** | 含用户 API key / PRIMARY_HOST / 阈值，覆盖会清掉用户配置 |
| `.meta/rules/category-privacy.md` | **跳过若存在** | Phase 0 嗅探结果 + 用户人审，不可覆盖 |
| `.meta/governed-files.txt` | **跳过若存在** | 用户可能已按本仓库边界增删 |
| `docs/TAXONOMY.md` | **跳过若存在** | Phase 0 推断 + 用户确认 |
| `docs/CHANGELOG.md` | **追加不覆盖** | 历史记录 |
| `.meta/converge/` `.meta/memory/` 内容 | **跳过若存在**（只补缺失的 README/骨架） | 持久型，含收敛证据与记忆 |
| `.meta/office-extracts/` | 由 extract_office.py 按 hash 增量管理 | 派生型 |
| `AGENTS.md` / `CLAUDE.md` / `GEMINI.md` | **覆盖**（覆盖后立即跑 sync_agents.py；重跑先查 MD5，不一致只补跑同步） | 由 AGENTS 模板生成；中断会致三文件漂移 |

### bootstrap_phase 状态记录

`bootstrap_phase` 是**断点记录**（非解锁门控），枚举 ∈ `{phase0, phase1, phase2, phase3}`，表示"已完成到哪个 Phase"。全部完成 = `phase3` + `bootstrap_status: completed`。

重复触发本技能时，先读目标仓库 AGENTS.md 的 `bootstrap_status` 与 `bootstrap_phase`：
- `bootstrap_status: in_progress` → 从 `bootstrap_phase` 的下一个 Phase 续装
- `bootstrap_status: completed` → 提示"harness 已装，是否重跑维护 / 升级 bundle 脚本？"
- 无 `bootstrap_status` 字段（首次）→ 从 Phase 0 开始
- 已存在 `kb-bootstrap-plan.md` 决策记录（Phase 0 中断过）→ 复述历史决策，给用户选项：沿用 / 重新决策

### 旧版兼容

检测到旧版安装（目标仓库存在 `maintain-lite.py` 或 `.meta/deliberations/`）时，按"旧版迁移"路径处理：

- 检测到旧版脚本 `maintain-lite.py` → 替换为 `maintain.py`，拷入全量脚本
- `.meta/deliberations/` 或 audit 历史内容原地保留，在其 README 标注"只读证据归档，禁止新增"
- 治理统一走 `.meta/converge/`

---

## 产物清单（bootstrap 完成后）

```
target-vault/
├── AGENTS.md / CLAUDE.md / GEMINI.md        ← 本技能模板（图书管理员准则 + 并行准则 + 维护管线）
├── .env                                      ← 从 env.example 填充
├── .githooks/pre-commit                      ← 统一版 hook（plan status + converge 路径 + GOV 提醒 + key/隐私/MD5）
├── docs/
│   ├── STRUCTURE.md / plans/ / CURRENT.md    ← init-agent-docs 打底（若装）
│   ├── CONSTITUTION.md                       ← 三元原则 + 持久性四型 + 多轴门控 + converge 生命周期
│   ├── TAXONOMY.md                           ← Phase 0 推断（draft）
│   └── CHANGELOG.md                          ← 首条 bootstrap 记录
├── .meta/
│   ├── scripts/                              ← 全量脚本集（见脚本清单）
│   ├── office-extracts/                      ← office 文档提取 sidecar（派生型）
│   ├── memory/                               ← MEMORY.md + user/role.md + workflows/ 等（Phase 2）
│   ├── converge/{active,done}/ + README.md   ← 治理（Phase 3，唯一治理目录）
│   ├── governed-files.txt                    ← 治理文档机械 SSOT
│   └── rules/
│       ├── category-privacy.md               ← Phase 0 嗅探结果（模板：templates/category-privacy.md）
│       └── retrieval.md                      ← ask.py 优先
└── kb-bootstrap-plan.md                      ← Phase 0 决策记录（交互引导各步的选择与结果）
```

---

## 约束与诚实标注

1. **不健全仓库与 harness 的矛盾**：`notes+office_docs<10` 时提示"自重可能超内容，建议先攒内容"；全量安装对空数据优雅退化，但 LLM 步骤产出价值有限。
2. **office 提取质量边界**：提取只保纯文本——表格结构简化为 `|` 分隔、图表/图片内文字/批注不保真；.doc/.xls/.ppt 老格式不支持（登记提示转存）。检索命中提取件时永远指向源文件；**编辑改源文件，不改提取件**。
3. **并发与限速**：MAINTAIN_CONCURRENCY 默认 6，与 API_RATE_LIMIT_MS 配合。触发 API 限流时先调低并发，不要关并发。
4. **单用户单机假设**：本 harness 默认单机模型。多用户/多机仓库的主从边界需在 Phase 0 画像提示后由用户确认。
5. **API 后端**：默认绑死 DeepSeek+智谱（`common.py` client 无工厂）。换后端需改 `common.py`。**DeepSeek 模型一律选最新版的次等模型**（当前 `deepseek-v4-flash`）：维护管线是批量任务，旗舰 pro 类模型成本不划算；DeepSeek 发布新代时升级为该代次等型号，并同步 `templates/env.example` 与 `common.py` 默认值。
6. **治理机制不复刻**：converge 机制权威源是全局 converge SKILL；本技能只在目标仓库落路径绑定与 charter 指针。
7. **快照维护债**：bundle 脚本会随上游演进 drift。维护者 drift 检测说明见 README「for maintainers」小节。

---

## 触发后的第一步

收到触发指令后，**先读**：
1. 目标仓库根是否有 `.meta/scripts/`（已装？旧版 maintain-lite？）、是否有 `kb-bootstrap-plan.md`（历史决策记录？）
2. `refs/maturity-rubric.md` + `refs/privacy-scan.patterns` + `refs/taxonomy-inference.prompt`
3. 然后从 Phase 0 开始（扫描 → 分步交互引导用户决策）。
