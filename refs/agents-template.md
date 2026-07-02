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
任何寻找仓库内容的步骤默认调用 `.meta/scripts/ask.py`（语义检索）。Grep 仅用于已知精确字符串/路径定位、frontmatter 字段等结构性匹配。

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
| "维护" / "更新索引" | 增量索引 | `python .meta/scripts/maintain-lite.py` |
| "全量重建" / 首次运行 | 全量索引 | `python .meta/scripts/maintain-lite.py --full` |
| "我写过 X 吗？" / "搜索" | 查询 | `python .meta/scripts/ask.py "query"` |
| "帮我起草 X" | 新建笔记 | 落根目录，按用户要求写，纯 agent 创作须加 `model`/`generated_at` |

### 第 2 步 · 执行前自检
- [ ] 我是否准备写入用户原文？如果是且用户没明确要求 → **停止**
- [ ] 我是否准备用 Grep 找内容？如果不是精确字符串 → **改用 ask.py**
- [ ] 我是否准备新建纯 agent 创作的笔记？→ 运行 `whoami.py --frontmatter` 取溯源字段

---

## 常用脚本

```bash
python .meta/scripts/maintain-lite.py           # 增量维护
python .meta/scripts/maintain-lite.py --full    # 全量重建
python .meta/scripts/ask.py "query"             # 语义检索
python .meta/scripts/ask.py --orphans           # 孤儿清单
python .meta/scripts/whoami.py                  # 模型自检
python .meta/scripts/whoami.py --frontmatter    # 溯源 YAML
```

模型配置见 `.env`；不得硬编码 key。

> CLAUDE.md / GEMINI.md 由 sync_agents.py 从 AGENTS.md 自动生成，禁止手改（pre-commit hook 强制 MD5 一致）。

---

## Bootstrap 状态

```yaml
bootstrap_status: in_progress   # 或 completed
bootstrap_completed_at:          # maintain-lite --full 成功 + 用户确认后填日期
bootstrap_phase: "1a"            # 已安装的 Phase（1a / 1a+1b / +2 / +3）
```

> `bootstrap_status: in_progress` 期间，容忍建立必要的体系结构（维护管线、检索、入口）。
> Occam **始终生效**——标记只是状态记录，非门控；新增结构仍须自证必要。

---

## 约束速查

| 禁止动作 | 替代方案 |
|---------|---------|
| 读隐私目录 | 直接拒绝 |
| 修改原文/目录（未经要求） | 只建议到 health-report |
| `git push` / `reset --hard` / `rebase` | 等用户明确要求 |
| 硬编码 API key | 走 `.env` |
| 从机改持久型文件 / 跑维护脚本 | 由主机执行 |
| 直接编辑 CLAUDE.md / GEMINI.md | 编辑 AGENTS.md 后跑 `python .meta/scripts/sync_agents.py`，三文件 MD5 自动校验 |
