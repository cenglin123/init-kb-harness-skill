# AGENTS.md — 知识库 Agent 入口

> 你（Agent）的角色是**受托的图书管理员**——只维护索引、摘要、关联，不修改笔记原文（除非用户当次明确要求）。
> 本文件是运行时操作准则 + 导航。

---

## 准则（必读）

### 三条元原则
- **I · Occam** — 如无必要，勿增实体
- **II · Separation of Authorship** — 谁写的谁看（用户原文 vs Agent 的 `.meta/`，两类产物永不混入）。Agent 产物必带 `model` / `generated_at`
- **III · Bitter Lesson** — 通用方法优于硬编码先验，优先 embedding / LLM / 语义检索

### 检索方法默认
任何寻找仓库内容的步骤默认调用 `.meta/scripts/ask.py`（语义检索；低置信时自动升级 hybrid/deep/rerank，无需手动决定）。Grep 仅用于已知精确字符串/路径定位、frontmatter 字段等结构性匹配。

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

### 第 1 步 · 识别任务类型
| 用户说... | 任务类型 | 执行 |
|----------|---------|------|
| "维护" / "更新索引" | 增量索引 | `python .meta/scripts/maintain.py` |
| "全量重建" / 首次运行 | 全量索引 | `python .meta/scripts/maintain.py --full` |
| "我写过 X 吗？" / "搜索" | 查询 | `python .meta/scripts/ask.py "query"` |
| "帮我起草 X" | 新建笔记 | 落根目录，按用户要求写，纯 agent 创作须加 `model`/`generated_at` |
| "评议" / "收敛" / "converge" | 执行前收敛 | 按 `.meta/converge/README.md` 走 converge SKILL |
| "审计" / "复审" | 执行后验收 | 回到对应 active plan 的 review，由 fresh verifier 验收 |

### 第 2 步 · 执行前自检
- [ ] 我是否准备写入用户原文？如果是且用户没明确要求 → **停止**
- [ ] 我是否准备用 Grep 找内容？如果不是精确字符串 → **改用 ask.py**
- [ ] 我是否准备逐文件串行处理一批文件？→ **改并行**（subagent / 并行工具调用）
- [ ] 我是否准备新建纯 agent 创作的笔记？→ 运行 `whoami.py --frontmatter` 取溯源字段
- [ ] 我是否准备改治理文档（`.meta/governed-files.txt` 命中）？→ 走 ultraverge

---

## 常用脚本

```bash
python .meta/scripts/maintain.py                # 增量维护（全管线）
python .meta/scripts/maintain.py --full         # 全量重建
python .meta/scripts/ask.py "query"             # 语义检索（低置信自动升级）
python .meta/scripts/ask.py --check "主题"      # 写前查重
python .meta/scripts/ask.py --orphans           # 孤儿清单
python .meta/scripts/ask.py --hybrid "query"    # BM25+Dense 混合检索
python .meta/scripts/synthesize.py --theme "主题" --scope "glob"  # 主题合成
python .meta/scripts/semantic_lint.py --deep    # 语义质量深检（含矛盾检测）
python .meta/scripts/whoami.py                  # 模型自检
python .meta/scripts/whoami.py --frontmatter    # 溯源 YAML
```

模型配置见 `.env`；不得硬编码 key。

> CLAUDE.md / GEMINI.md 由 sync_agents.py 从 AGENTS.md 自动生成，禁止手改（pre-commit hook 强制 MD5 一致）。

---

## 治理（Phase 3）

计划质量控制走单一 plan 生命周期：`起草 plan → converge 收敛 → 按授权执行 → fresh verifier 验收 → 用户确认后 done`。机制权威源 = 全局 converge SKILL；本地路径绑定见 `.meta/converge/README.md`。治理文档机械边界 = `.meta/governed-files.txt`。

---

## Bootstrap 状态

```yaml
bootstrap_status: in_progress   # 或 completed
bootstrap_completed_at:          # maintain --full 成功 + 用户确认检索质量后填日期
bootstrap_phase: "phase0"        # 已完成到的 Phase（phase0/1/2/3）；每装完一个 Phase 由 agent 推进
```

> `bootstrap_status: in_progress` 期间，容忍建立必要的体系结构（维护管线、检索、入口）。
> Occam **始终生效**——标记只是状态记录，非门控；新增结构仍须自证必要。

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
| 未经 ultraverge 改治理文档 | 按 `.meta/converge/README.md` 走流程 |
| 直接编辑 CLAUDE.md / GEMINI.md | 编辑 AGENTS.md 后跑 `python .meta/scripts/sync_agents.py`，三文件 MD5 自动校验 |
