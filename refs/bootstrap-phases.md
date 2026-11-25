# Bootstrap 执行手册（Phase 0 → 3、幂等与旧版兼容）

本手册承接 `SKILL.md` 的分阶段执行细节、完成判定、幂等策略、`bootstrap_phase` 断点续装、旧版迁移与产物清单。执行任一阶段前按需读取对应小节。

---

## Phase 0 · 体检 + 交互式引导（read-only，必跑）

**不动手，先诊断；随后一步一步引导用户做决策。**

1. **规模/结构扫描**：笔记数、**office 文档数（含 `.doc`/`.xls`/`.ppt` 老格式单列）**、目录树、git 状态。
2. **隐私嗅探**：按 `refs/privacy-scan.patterns`（中英双覆盖 + 凭据 glob + 高熵串）扫描，结果带到下方决策点 ① 供用户人审。**生成 `.meta/rules/category-privacy.md`**（来自 `templates/category-privacy.md`，用户确认后的隐私目录填入 `## 隐私目录` 段——必须裸目录名列表项，对齐 `_load_privacy_dirs()` 消费契约）。散落敏感文件（不在隐私目录）建议**移入隐私子目录**后纳入保护——当前机制仅目录级生效，散落文件不自动排除。隐私目录内的 office 文档同样不提取。
3. **taxonomy 推断**：用 `refs/taxonomy-inference.prompt`（LLM 主推断 + 规则后置兜底）提议分类，写入 `docs/TAXONOMY.md`（draft，决策点 ② 由用户定夺）。
4. **仓库画像**：按 `refs/maturity-rubric.md`（全量化机械可判）产出画像与提示（小库自重提示、office 为主提示、老格式转存提示、参数阈值建议）——**只影响提示与参数，不裁剪安装范围**。
5. **分步引导用户决策（核心）**：扫描完成后，按下方决策点**逐个**向用户提问——每点给出**具体选项**让用户在上下文里即时决策，每步有记录、可中断可续。优先用 harness 的交互提问机制（opencode `question` 工具 / Claude Code `AskUserQuestion`）；无交互机制时退化为在对话中列出编号选项请用户回复序号。**一次只问一个决策点**，等用户回答后再进下一个：

   | 步骤 | 决策点 | 呈现内容 | 选项 |
   |------|--------|---------|------|
   | ① | 隐私目录 | 嗅探命中文件清单 + 建议隐私目录 | a) 全部纳入 b) 仅部分（用户点名）c) 都不纳入；散落文件追加一问：移入隐私子目录 / 保持现状仅记录 |
   | ② | taxonomy | TAXONOMY.md 草案要点 | a) 接受 b) 用户口述调整、agent 改后再示 c) 留 draft 以后再定 |
   | ③ | 安装模式与增强层 | 仓库画像 + rubric 提示 + 能力对照（见 `SKILL.md` 安装模式节）；**只问用户现有条件**，不催配 key | a) **默认（推荐）**：内置本地嵌入 + BM25 + 图谱，零 API b) 换更高质量嵌入：本机已有 Ollama / 任意 OpenAI 兼容端点，或云端（换后重建向量库）c) 加 LLM 层：DeepSeek 摘要/精排（需 key，可选）d) 极小仓库先攒内容、暂不 bootstrap |
   | ④ | 增强层配置（**仅决策点 ③ 选了 b/c 时进行；默认无需任何配置**） | 语义层：`EMBED_BASE_URL` + `EMBED_MODEL`（本地端点无需 key）；LLM 层：`DEEPSEEK_API_KEY`；可选：`PRIMARY_HOST` | a) 现在提供（贴入对话或用户自行编辑 `.env`）b) 稍后自填（骨架照装、对应层保持关闭，不影响其余能力） |
   | ⑤ | worktree 协作桥接（**仅目标是 Git 仓库时询问**） | 说明 Git worktree 隔离与主工作树占用声明用途 | a) 启用：复用 init-agent-docs helper，按需为复杂/并行任务建 task worktree b) 不启用：保留普通 agent 工作流，不安装 helper；非 Git 仓库自动记为不适用 |

   每步的用户选择**记录进 `kb-bootstrap-decisions.md`**（定位：**执行记录**——记下每步选项、选择结果）。
6. **所有适用决策点都有结果 → 直接进入 Phase 1**。中断后重触发时沿用 `kb-bootstrap-decisions.md` 中已确认的选择；只在用户明确要求重选时才重新询问，不重复确认已有协作桥接选择。

---

## Phase 1 · 骨架 + 检索 + Office 提取（必装）

**这是「容忍必要体系建立」的核心阶段。** 装完即可自维护。

1. 检测/调用 `init-agent-docs` 打底（已有 `docs/STRUCTURE.md`、`docs/plans/`、`scripts/changelog.py` 或 `.meta/scripts/changelog.py` 时复用不重铺）。覆盖 `AGENTS.md` 前，读取现有协作路由、用户改写的任务范围约定，以及 `kb-bootstrap-decisions.md` 中对此桥接的选择；沿用已确认的决定，不重复询问。先保留已启用协作约定及用户调整，再与本模板合并。
2. 先完成 [agent-docs 布局迁移](agent-docs-layout.md)，再拷贝**全部 KB 脚本**到 `.meta/scripts/`（见 [script-inventory.md](script-inventory.md)——一次拷齐；已有 `changelog.py` 时不再部署等价 `changelog_append.py`；保留 `docs_maintain.py`/`audit.py`/`check_all.py`；各层共用同一套脚本）。用户选择启用协作桥接且目标是 Git 仓库时，从 `init-agent-docs` `assets/scripts/` 安装或复用唯一的 `worktree_task.py`；不另建 helper 副本或 hook。
3. `templates/env.example` → `.env`（默认 `HARNESS_MODE=lite` + `EMBED_PROVIDER=auto` 走内置本地模型，无需再配；要换嵌入端点填 `EMBED_BASE_URL` + `EMBED_MODEL`；要 LLM 层设 `full` 并填 key；另有 `PRIMARY_HOST` + `MAINTAIN_CONCURRENCY` + office 并发/超时 + 阈值）。
4. 安装 Python 依赖：**优先用离线套装**（`assets\scripts\15-install-kb-deps.ps1`，含 wheel + 内置嵌入模型缓存，固定 Python 3.13）；没有套装时才 `pip install -r requirements.txt`。
5. `docs/CONSTITUTION.md`（来自 `refs/constitution-template.md`）+ `docs/TAXONOMY.md`（Phase 0 草案）+ `AGENTS.md`（来自 `refs/agents-template.md`，`bootstrap_status: in_progress`）——`AGENTS.md` 是唯一入口，不生成任何同步副本。仅当目标是 Git 仓库且用户选择启用时，加入或合并可选协作段并安装唯一 helper；否则移除模板中的可选协作段，不装该 helper。替换模板时保留已确认的协作约定、用户自定义任务范围规则及其决定记录，不静默丢弃。
6. 仅在目标仓库有 Git 时，安装 `.githooks/pre-commit`（来自 `refs/pre-commit-template`，统一版）并接线：`git config core.hooksPath .githooks`。没有 Git 时不安装或接线 hook，也不安装协作 helper；Git Bash 缺失时按下方前置缺失交互处理。
7. 按 `TAXONOMY.md` 创建内容目录（如日记/复盘/收件箱等，名称因用户偏好而异）。若用户使用 Obsidian daily-notes 插件，配置 `.obsidian/daily-notes.json` 指向对应目录。

> **安装中的交互引导**：机械步骤遇前置缺失（无 Python / 无 Git Bash / 所选层的端点或 key 未配 / office 依赖装不上）不当场失败，给用户选项：a) 现在补齐 b) 跳过该件继续（明确标注后果，如 hook 不生效、LLM 步骤暂不可跑、office 内容暂不可检索）c) 中止，处理后再续。选择记录进 `kb-bootstrap-decisions.md`。

---

## Phase 1.5 · 宿主护栏（可选，用户确认后装）

把本库已声明的**机械路径规则**硬化到宿主动作侧：隐私目录读禁、从机写禁、CHANGELOG 编辑重定向、compact 哨兵注入、触发词检索提醒。**护栏只拦机械路径规则，不拦判断**；状态侧（pre-commit）仍是地基，本层是加固而非替代，软规则原文保留。

**前提**：Phase 1 已装（`host_guard.py` 随脚本集落位）；Phase 0 隐私目录已经用户确认。

1. **宿主探测**：检测本机实际使用的 agent 宿主（opencode / Claude Code / Codex / 其他），逐个向用户确认后只生成对应适配；一个都不使用则跳过本 Phase。
2. **策略落盘**：`templates/host-write-policy.json` → `.meta/rules/host-write-policy.json`。目标库 `AGENTS.md` 若已有演进版从机禁改清单，以该清单为准转写，不用模板覆盖。
3. **按宿主生成适配**（全部指向同一脚本 `.meta/scripts/host_guard.py`，单一策略源）：
   - **opencode**：`templates/host-guardrails/opencode.json` → 项目根 `opencode.json`（已存在则只合并 `permission` 段）；`templates/host-guardrails/kb-guard.js` → `.opencode/plugins/kb-guard.js`
   - **Claude Code**：`templates/host-guardrails/claude-settings-fragment.json` 合并进 `.claude/settings.json`（保留既有 `permissions.allow` 等全部键，追加 `deny` 与 `hooks`）
   - **Codex**：`templates/host-guardrails/codex-hooks.json` → `.codex/hooks.json`；提示用户需在 Codex `/hooks` 审查信任后生效
   - **其他宿主**：无适配则如实保持软约束，不伪造覆盖
4. **验证**：`python .meta/scripts/host_guard.py compact-context` 输出 SessionStart JSON；`simulate-host` 模拟从机判定；构造隐私路径 payload 实测 deny/allow（用例形态参考 vault 侧 `.meta/tests/test_host_guard.py`）。
5. **登记**：`AGENTS.md` 写明哪些宿主已接线、哪些仍软约束（compact 哨兵段措辞绑定实测结果，实测失败的宿主保持「技术债」原措辞）。

---

## Phase 2 · 自沉淀（必装）

- 仅补齐 `templates/memory-scaffold/` 中缺失文件到 `.meta/memory/`，不得覆盖 Phase 1 已迁并的自由文本、角色与记忆（`MEMORY.md` + `user/` + `feedback/` 骨架；其余子目录按需涌现，不预建。知识库内容由用户笔记目录承担，记忆只存 agent 专属物）。
- `MEMORY.md` 的「当前记忆条目」索引段由 `memory_index.py` 自动维护；拷入后立即跑 `python .meta/scripts/memory_index.py` 生成首版索引。
- 填充 `AGENTS.md` 的「项目记忆」内联段：活跃主题待 vault 使用后自然涌现，bootstrap 阶段写「待补」；用户称呼 / 关键偏好需向用户补问一句后填入（勿留未替换的方括号占位符、勿臆造）。
- Phase 2 的脚本（`dream` / `semantic_lint` / `synthesize` / `knowledge_map` / `bm25_index`）已随 Phase 1 拷入，此处无脚本动作。

---

## Phase 3 · 治理（必装）

- 建 `.meta/converge/{active,done}/` + README charter（来自 `refs/converge-readme-template.md`）——**只此一个治理目录**，不建 `deliberations`/`audit`。
- 生成 `.meta/governed-files.txt`（来自 `refs/governed-files-template.txt`，按目标仓库实际治理边界增删）。
- `.env` 设 `CONVERGE_DIR=.meta/converge`（converge SKILL 产物路径绑定；hook 拦截误落 `.converge/`）。
- 治理叙事统一为单一 plan 生命周期：`起草 plan → converge 收敛 → 按授权执行 → fresh verifier 验收 → 用户确认后归档 docs/plans/completed/（status: done）`；治理文档改动走 ultraverge（详见 `refs/governance-multi-axis.md`）。

---

## Bootstrap 完成判定

1. `python .meta/scripts/maintain.py --full` **退出码 0**（默认：`extract_office` + `embed` + `build_links` + `index` + `graph` + `bm25` + `knowledge_map` + `health` + `dream`；LLM 层另含 `summarize`）。退出码 3 = 语义层缺失/失败，bootstrap 不可判完成。
2. `health-report.md` 与 `.index/manifest.md` 生成；健康报告的「语义层」段无 ⚠️（覆盖率为 0 或配置不一致都算未完成）；若仓库有 office 文档，`.meta/office-extracts/` 有产出。
3. **人审门（交互式）**：agent 引导用户跑一条真实问题检索——正常情况用 `ask.py "query"` 语义检索；若它报告语义层不可用（临时降级 BM25），说明 bootstrap **还不能判完成**，先按提示修复（office 为主的仓库应验证能命中 office 内容），然后给出选项请用户判定检索质量：a) 可接受 → 完成 b) 需调整 → 排查后重试 c) 暂不确认（保持 `in_progress`，原因记录进 plan）。
4. → 把 `AGENTS.md` 的 `bootstrap_status` 改 `completed`、`bootstrap_phase` 改 `phase3`，填 `bootstrap_completed_at: <date>`。

> **只有启用的层才受配置门控**：默认安装无需任何 key，但**语义层必须真的建出向量**（`maintain.py --full` 退出码 0 且 health 语义段无告警）才可判 completed。选了 LLM 层但 key 稍后自填的仓库，`summarize` 必然失败——把那层的开关关掉（`HARNESS_MODE=lite`）即可完成 bootstrap，并在 `kb-bootstrap-decisions.md` 注明原因，key 配好后再开。

> **诚实标注**：`maturity-rubric` 的画像提示全部机械可判；bootstrap 完成判定含一次人审门（用户确认检索质量）——前者是信息呈现、后者决定体系是否就绪。

**此后 Occam 继续生效，新增结构须自证必要。**

---

## 幂等性

每次重跑不破坏已装部分，按下表分类处理：

| 文件类 | 重跑行为 | 理由 |
|--------|---------|------|
| `.meta/scripts/*.py`（含 KB `maintain.py`） | **覆盖**（以 skill bundle 为准） | 脚本是机械产物，以 skill bundle 为准覆盖 |
| 旧 `scripts/` / `.agents/memory/` | 按迁移清单逐项处理；成功后重跑只校验，不重复追加记忆；未知内容保留并报告 | 备份、冲突处理及验证见 `agent-docs-layout.md`；不删除 `.agents/skills` 等其他资产 |
| `refs/` 模板（constitution/agents/pre-commit/converge-readme） | **覆盖** | 模板随 skill 演进 |
| `.env` | **跳过若存在** | 含用户 API key / `PRIMARY_HOST` / 阈值，覆盖会清掉用户配置 |
| `.meta/rules/category-privacy.md` | **跳过若存在** | Phase 0 嗅探结果 + 用户人审，不可覆盖 |
| `.meta/governed-files.txt` | **跳过若存在** | 用户可能已按本仓库边界增删 |
| `docs/TAXONOMY.md` | **跳过若存在** | Phase 0 推断 + 用户确认 |
| `docs/CHANGELOG.md` | **追加不覆盖** | 历史记录 |
| `.meta/rules/host-write-policy.json` | **跳过若存在** | 用户可能已按本仓库边界增删（Phase 1.5） |
| 宿主配置文件（`opencode.json` / `.claude/settings.json` / `.codex/hooks.json`） | **合并式更新** | 保留既有权限/hook 规则，只补缺失段（Phase 1.5） |
| `.opencode/plugins/kb-guard.js` | **覆盖** | 以 skill bundle 为准（Phase 1.5） |
| `.meta/converge/` `.meta/memory/` 内容 | **跳过若存在**（只补缺失的 README/骨架） | 持久型，含收敛证据与记忆 |
| `.meta/office-extracts/` | 由 `extract_office.py` 按 hash 增量管理 | 派生型 |
| `AGENTS.md` | **条件覆盖**（`in_progress` rerun 按 Phase 续装；`completed` 升级保留内联段及已存在的可选协作约定，详见下方升级流程；legacy 旧版兼容直接覆盖为新模板，详见旧版兼容段） | 内联段含用户自由文本不可重建；可选协作约定按现状合并或保留；其余为机械规则随模板；`AGENTS.md` 是唯一入口，不存在同步副本 |

---

## `bootstrap_phase` 状态记录

`bootstrap_phase` 是**断点记录**（非解锁门控），枚举 ∈ `{phase0, phase1, phase2, phase3}`，表示「已完成到哪个 Phase」。全部完成 = `phase3` + `bootstrap_status: completed`。

重复触发本技能时：

0. **先检测仓库根是否存在旧名 `kb-bootstrap-plan.md`** → 存在则重命名为 `kb-bootstrap-decisions.md`（决策记录不丢，仅文件名变更）。
1. 读目标仓库 `AGENTS.md` 的 `bootstrap_status` 与 `bootstrap_phase`，按下方分支处理：
   - `bootstrap_status: in_progress` → 从 `bootstrap_phase` 的下一个 Phase 续装。
   - `bootstrap_status: completed` → 提示「harness 已装，是否重跑维护 / 升级 bundle 脚本？」；若升级 bundle 脚本，`AGENTS.md` 按幂等表「条件覆盖」处理：① 读旧 `AGENTS.md` 的「项目记忆」内联段已填内容并备份；② 识别已安装的可选 worktree 协作段及其用户调整；③ 用新模板覆盖 `AGENTS.md`；④ 把备份内联段填回新模板对应位置，并合并或保留已安装的协作约定（勿重置为占位符或静默移除用户调整）；⑤ 若新模板内联段结构跨版本变更（增删字段/改名），须显式给出迁移映射，不能机械填回。
   - 无 `bootstrap_status` 字段（首次）→ 从 Phase 0 开始。
   - 已存在 `kb-bootstrap-decisions.md` 决策记录（Phase 0 中断过）→ 复述历史决策，给用户选项：沿用 / 重新决策。

---

## 旧版兼容

检测到旧版安装（目标仓库存在 `maintain-lite.py` 或 `.meta/deliberations/`）时，按「旧版迁移」路径处理：

- 检测到旧版脚本 `maintain-lite.py` → 替换为 `maintain.py`，拷入全量脚本。
- `.meta/deliberations/` 或 audit 历史内容原地保留，在其 README 标注「只读证据归档，禁止新增」。
- 治理统一走 `.meta/converge/`。
- `AGENTS.md`：**覆盖前提示用户**——「检测到旧版 AGENTS.md，将被新模板覆盖（含「项目记忆」内联段占位符）；非内联段区域的用户定制将丢失（git 历史可恢复）」；确认后覆盖为新模板，按 Phase 2 流程填充内联段。**覆盖安全依据**：旧版无内联段 → 不丢失内联段自由文本（独立第三场景，非 `in_progress` 语义）；非内联段区域按模板权威原则覆盖（幂等表理由栏）；不走 `completed` 升级流程的 ①备份 ③填回 ④迁移映射。
- **三文件体系清退（旧版安装必做）**：检测到 `CLAUDE.md` / `GEMINI.md` 同步副本、`.meta/scripts/sync_agents.py` 或 pre-commit hook 内嵌的三文件 MD5 检查段时，按以下步骤清退：
  1. 直接删除两副本（内容已并入 `AGENTS.md` 模板；单一入口下保留只会制造两处维护与漂移）；
  2. 删除 `.meta/scripts/sync_agents.py`（治理一致性校验由 `check_gov_consistency.py` 承接）；
  3. hook 换装：用 `refs/pre-commit-template` 统一版**整体覆盖**目标仓库 `.githooks/pre-commit`（不再含 MD5 段），覆盖后 `bash -n .githooks/pre-commit` 自检，并确认 `git config core.hooksPath .githooks` 已接线；
  4. 验证：`python .meta/scripts/check_gov_consistency.py --check-gov-consistency` 应通过（治理边界三方一致；此后 pre-commit 第 9 段自动把关）；
  5. 优先用 `.meta/scripts/changelog.py add`（旧库仅有 `changelog_append.py` 时沿用它）记一条迁移条目（标题建议「三文件体系清退，统一 AGENTS.md 单一入口」，正文列出删除的文件与 hook 变更）；
  6. **习惯废止告知（须向用户明说）**：原「改完 AGENTS.md 跑 sync_agents.py」的动作随脚本删除而废止——`AGENTS.md` 是唯一入口，保存即生效，无任何同步步骤。

---

## 产物清单（bootstrap 完成后）

```text
target-vault/
├── AGENTS.md                                   ← 本技能模板（唯一入口；图书管理员准则 + 并行准则 + 维护管线 + 提交策略）
├── <内容目录>/                               ← 按 TAXONOMY.md 定义（日记/复盘/收件箱/工作流等，名称因用户偏好而异）
├── .env                                      ← 从 env.example 填充（默认内置本地嵌入 + BM25；换端点/加 LLM 层时才改）
├── .githooks/pre-commit                      ← 统一版 hook（plan status + converge 路径 + GOV 提醒 + key/隐私 + MEMORY.md 索引一致性）
├── docs/
│   ├── STRUCTURE.md / plans/ / CURRENT.md    ← init-agent-docs 打底（若装）
│   ├── CONSTITUTION.md                       ← 三元原则 + 持久性四型 + 多轴门控 + converge 生命周期
│   ├── TAXONOMY.md                           ← Phase 0 推断（draft）
│   └── CHANGELOG.md                          ← 首条 bootstrap 记录
├── .meta/
│   ├── scripts/                              ← 全量脚本集（见 refs/script-inventory.md；host_guard.py 随集落位，Phase 1.5 接线启用）
│   ├── office-extracts/                      ← office 文档提取 sidecar（派生型）
│   ├── memory/                               ← MEMORY.md + user/role.md + workflows/ 等（Phase 2）
│   ├── converge/{active,done}/ + README.md   ← 治理（Phase 3，唯一治理目录）
│   ├── governed-files.txt                    ← 治理文档机械 SSOT
│   └── rules/
│       ├── category-privacy.md               ← Phase 0 嗅探结果（模板：templates/category-privacy.md）
│       ├── retrieval.md                      ← ask.py 优先
│       └── host-write-policy.json            ← Phase 1.5（可选）：从机禁改清单机器可读 SSOT
├── opencode.json / .claude/settings.json / .codex/hooks.json / .opencode/plugins/kb-guard.js   ← Phase 1.5（可选）：按宿主探测生成的动作侧护栏适配
└── kb-bootstrap-decisions.md                 ← Phase 0 决策记录（交互引导各步的选择与结果）
```
