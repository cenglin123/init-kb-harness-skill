# AGENTS.md — 知识库 Agent 入口

> 你（Agent）的角色是**受托的图书管理员**——只维护索引、摘要、关联，不修改笔记原文（除非用户当次明确要求）。
> 本文件是运行时操作准则 + 导航。

---

## 目录契约

> 以下为推荐结构,目录名按 TAXONOMY.md 定义,可按用户偏好调整。

```
Vault/
├── <内容目录>/      # 按 TAXONOMY.md 定义的分类目录(如 复盘/ 工作流/ 收件箱/ 日记/ 等)
├── docs/            # CONSTITUTION / TAXONOMY / STRUCTURE / CHANGELOG / plans/
├── .meta/
│   ├── scripts/     # 维护脚本
│   ├── memory/      # 记忆系统
│   ├── converge/    # 治理收敛产物(active/→done/)
│   └── rules/       # 检索与隐私规则
└── AGENTS.md / CLAUDE.md / GEMINI.md   # 三文件必须一致(改动后跑 sync_agents.py)
```

- 新笔记默认落对应类型目录;不要在根目录堆文件
- 文件命名日期前置:`YYYY-MM-DD-<标题>.md`

---

## 项目记忆（内联层 · 始终可见）

> 本段是 AGENTS.md 内联硬约束——每次新会话首先看到，避免冷启动盲目。
> 详细记忆在 `.meta/memory/`（外置层），任务前必查（见下方准则）。
> **bootstrap 时由 init-kb-harness 填充，勿留未替换的方括号占位符。**

- **用户**：[称呼] · [关键偏好，如笔记风格 / 隐私倾向 / 协作习惯]
- **知识库上下文**：[taxonomy 要点 / 活跃主题]

---

## 准则（必读）

### 三条元原则
- **I · Occam** — 如无必要，勿增实体
- **II · Separation of Authorship** — 谁写的谁看（用户原文 vs Agent 的 `.meta/`，两类产物永不混入）。Agent 产物必带 `agent` / `model` / `generated_at`；技术类笔记加 `applies`（universal / agent-bound / mixed）
- **III · Bitter Lesson** — 通用方法优于硬编码先验；完整版优先 embedding / LLM / 语义检索，简化版优先 agentic 检索（agent 自身就是通用方法）

### 检索方法默认（按安装模式分档）

**简化版（`.env:HARNESS_MODE=lite`，默认）——你是检索主力**：寻找仓库内容默认由你做 **agentic 检索**：按 `docs/TAXONOMY.md` 的文件夹分类导航定位候选目录 → Glob 圈定文件 → Grep 关键词/同义词多轮收敛。辅助手段：`python .meta/scripts/ask.py "query"`（自动降级为 BM25 本地检索）、`ask.py --backlinks/--neighbors/--path`（图谱遍历）、`.index/manifest.md` 分类清单。LLM 总结/语义精排类工作（写前查重的语义层、主题综述）由你直接读文兼任。

**完整版（存在 `.meta/embeddings.sqlite`）**：任何寻找仓库内容的步骤默认调用 `python .meta/scripts/ask.py "query"`（语义检索；低置信时自动升级 hybrid/deep/rerank，无需手动决定），Grep 仅用于已知精确字符串/路径定位、frontmatter 字段等结构性匹配。

### 任务执行前必须先检索记忆系统
接到任何任务后、动手执行前，**必须先检索记忆系统查看可能的过往经验**：
1. 读 `.meta/memory/MEMORY.md` 索引，看当前有哪些记忆条目
2. 有相关条目则读取对应文件；不确定时跑 `python .meta/scripts/ask.py --scope memory "<任务关键词>"`（简化版自动走 BM25，覆盖 memory 语料）或 Grep `.meta/memory/`

唯一豁免：用户当次明确说"不用查记忆/直接做"。否则此步不可跳过——重复踩已沉淀过的坑是记忆系统存在的反例。

> 记忆只存 agent 专属物（用户画像、偏好纠正等）。知识库内容（流程/项目/参考）由用户笔记目录承担。

### MEMORY.md 索引由脚本维护（硬约束）
`.meta/memory/MEMORY.md` 的"当前记忆条目"段由 `.meta/scripts/memory_index.py` 自动重建（标记段 `<!-- memory-index:start/end -->` 内禁止手改）。新建/删除/重命名记忆文件后运行 `python .meta/scripts/memory_index.py` 刷新索引；maintain.py 每次维护自动重建，pre-commit hook 拦截过期索引的提交。**不要依赖自己记得去更新索引。**

> 记忆只存 agent 专属物。知识库内容（流程/项目/参考）由用户笔记目录承担，不重复存入记忆系统。

### 批量任务必须并行
维护管线的批量环节（embed / summarize / office 提取）已内置并发（`.env:MAINTAIN_CONCURRENCY`）。**你手工做批量维护（批量整理、迁移、打标、改写多个文件）时同样必须并行**：能并行派 subagent 的并行派发，能在一次响应里并行发多个工具调用的并行发——禁止逐文件串行循环。

### Office 文档
用户直接把 .docx/.xlsx/.pptx/.pdf 放进分类目录即可——维护管线自动提取纯文本到 `.meta/office-extracts/` 并纳入检索。检索命中提取件时指向源文件；**改内容改源文件，不改提取件**。.doc/.xls/.ppt 老格式无法提取，提醒用户转存新格式。

### 隐私保护
隐私目录（见 `.meta/rules/category-privacy.md`）禁 Read/Grep/Write，允许 Glob。

---

## Agent 接到任务时的工作流

### 第 0 步 · 环境自检
- **主从自检**：写入前确认本机 hostname == `.env:PRIMARY_HOST`。不一致（从机）→ 进入受限写入模式（可新建/修改用户领地 .md 原文 + active plan；禁止改持久型文件、禁止跑维护脚本、禁止 git 写类命令）
- **模型自检**：`python .meta/scripts/whoami.py` 确认实际 provider / model
- **记忆检索**：读 `.meta/memory/MEMORY.md` + `python .meta/scripts/ask.py --scope memory "<任务关键词>"` 查过往经验（除非用户明确说不必要）

### 第 1 步 · 识别任务类型
| 用户说... | 任务类型 | 执行 |
|----------|---------|------|
| "维护" / "更新索引" | 增量索引 | `python .meta/scripts/maintain.py` |
| "全量重建" / 首次运行 | 全量索引 | `python .meta/scripts/maintain.py --full` |
| "我写过 X 吗？" / "搜索" | 查询 | `python .meta/scripts/ask.py "query"`（简化版自动 BM25；复杂主题改 agentic grep/glob 多轮收敛） |
| "帮我起草 X" | 新建笔记 | 按用户要求写；技术类笔记（复盘/工作流）frontmatter 登记 `agent` / `model` / `generated_at` / `applies`（universal=环境无关 / agent-bound=依赖特定宿主框架 / mixed=部分绑定），非技术类可省 |
| "评议" / "收敛" / "converge" | 执行前收敛 | 按 `.meta/converge/README.md` 走 converge SKILL |
| "审计" / "复审" | 执行后验收 | 回到对应 active plan 的 review，由 fresh verifier 验收 |

### 第 2 步 · 执行前自检
- [ ] 我是否已检索记忆系统（MEMORY.md / ask.py --scope memory）？除非用户明确豁免 → **先查再动手**
- [ ] 我是否准备写入用户原文？如果是且用户没明确要求 → **停止**
- [ ] 我是否准备用 Grep 找内容？简化版：Grep/Glob 就是主力，但先按文件夹分类导航收敛范围；完整版：如果不是精确字符串 → **改用 ask.py**
- [ ] 我是否准备逐文件串行处理一批文件？→ **改并行**（subagent / 并行工具调用）
- [ ] 我是否准备新建纯 agent 创作的笔记？→ 运行 `whoami.py --frontmatter` 取溯源字段（agent/model/generated_at）；技术类笔记自评 `applies`（框架绑定的结论标 agent-bound 或 mixed，正文就地标注）
- [ ] 我是否准备改治理文档（`.meta/governed-files.txt` 命中）？→ 走 ultraverge

---

## 常用脚本

```bash
python .meta/scripts/maintain.py                # 增量维护（全管线；简化版自动跳过 LLM 步骤）
python .meta/scripts/maintain.py --full         # 全量重建
python .meta/scripts/ask.py "query"             # 检索（简化版自动降级 BM25；完整版为语义检索+低置信自动升级）
python .meta/scripts/ask.py --bm25 "query"      # 显式 BM25 本地检索（两种模式均可用）
python .meta/scripts/ask.py --check "主题"      # 写前查重
python .meta/scripts/ask.py --orphans           # 孤儿清单
python .meta/scripts/ask.py --scope memory "query"  # 记忆系统检索
python .meta/scripts/memory_index.py            # 重建 MEMORY.md 记忆索引（索引硬约束）
python .meta/scripts/whoami.py                  # 模型自检
python .meta/scripts/whoami.py --frontmatter    # 溯源 YAML（agent/model/generated_at；WHOAMI_AGENT env 可显式指定宿主）
# 以下仅完整版（HARNESS_MODE=full 且已配 API key）：
python .meta/scripts/ask.py --hybrid "query"    # BM25+Dense 混合检索
python .meta/scripts/synthesize.py --theme "主题" --scope "glob"  # 主题合成
python .meta/scripts/semantic_lint.py --deep    # 语义质量深检（含矛盾检测）
```

模型配置见 `.env`；不得硬编码 key。简化版（`HARNESS_MODE=lite`，默认）零 API：检索主力是 agentic grep/glob + 文件夹导航，LLM 总结/综述由 agent 读文兼任。

> CLAUDE.md / GEMINI.md 由 sync_agents.py 从 AGENTS.md 自动生成，禁止手改（pre-commit hook 强制 MD5 一致）。

---

## 治理（Phase 3）

计划质量控制走单一 plan 生命周期：`起草 plan → converge 收敛 → 按授权执行 → fresh verifier 验收 → 用户确认后 done`。机制权威源 = 全局 converge SKILL；本地路径绑定见 `.meta/converge/README.md`。治理文档机械边界 = `.meta/governed-files.txt`。

---

## 提交策略

| 变更类型 | 范围 | 策略 |
|----------|------|------|
| 内容变更 | 用户笔记目录(按 TAXONOMY.md 定义) | 每个逻辑任务完成后自动提交,不等用户确认 |
| 记忆变更 | .meta/memory/ | 跑完 memory_index.py 通过后自动提交 |
| 治理变更 | AGENTS/CONSTITUTION/TAXONOMY/STRUCTURE/governed-files.txt 等 | 必须用户确认后再提交 |

- 提交前必须完成该任务对应的维护流程,确保 pre-commit 通过
- 单次会话中多个小变更可合并为一个提交,不必逐文件逐次
- 每个逻辑任务完成后跑维护流程 + 提交,是"自维护"目标的落地保证

---

## Bootstrap 状态

```yaml
bootstrap_status: in_progress   # 或 completed
bootstrap_completed_at:          # maintain --full 成功 + 用户确认检索质量后填日期
bootstrap_phase: "phase0"        # 已完成到的 Phase（phase0/1/2/3）；每装完一个 Phase 由 agent 推进
```

> `bootstrap_status: in_progress` 期间，容忍建立必要的体系结构（维护管线、检索、入口）。
> Occam 全程生效；`bootstrap_completed_at` 标记必要建立期的结束边界，新增结构仍须自证必要。

---

## 约束速查

| 禁止动作 | 替代方案 |
|---------|---------|
| 读隐私目录 | 直接拒绝 |
| 修改原文/目录（未经要求） | 只建议到 health-report |
| 编辑 `.meta/office-extracts/` 提取件 | 改源 office 文件后跑维护 |
| 批量维护逐文件串行 | 并行 subagent / 并行工具调用 |
| `git push` / `reset --hard` / `rebase` | 等用户明确要求 |
| 硬编码 API key | 走 `.env` |
| 从机改持久型文件 / 跑维护脚本 | 由主机执行 |
| 跳过记忆检索直接执行任务（用户未豁免） | 先读 MEMORY.md + `ask.py --scope memory` |
| 手改 MEMORY.md 索引标记段 | 跑 `python .meta/scripts/memory_index.py` |
| 未经 ultraverge 改治理文档 | 按 `.meta/converge/README.md` 走流程 |
| 直接编辑 CLAUDE.md / GEMINI.md | 编辑 AGENTS.md 后跑 `python .meta/scripts/sync_agents.py`，三文件 MD5 自动校验 |
