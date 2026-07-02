# Memory 指针

> 记忆系统索引。各 Agent 启动时读本文件了解有哪些可用记忆。
> 实际记忆内容分布在下方各子目录。

---

## 目录结构

```
.meta/memory/
├── MEMORY.md              # 本文件（索引）
├── user/
│   └── role.md            # 用户画像（用户直接编辑；Agent 追加不覆盖）
├── feedback/              # 反馈记忆（用户偏好/纠正，带衰减）
├── project/               # 项目记忆（进行中项目的上下文）
├── reference/             # 外部参考（论文/文章摘要等）
└── workflows/             # 可复用任务流程
    └── README.md
```

## 记忆规则（写入 frontmatter）

- **触发记录**：用户说"记住 X" / agent 发现反复出现的偏好或决策模式
- **不记录**：可从代码/git log/笔记原文推导的事实；一次性会话细节；与日常协作无关的临时任务状态
- **更新时必须更新 frontmatter 的 `last_updated` / `generated_at`**
- **衰减**：dream.py（Phase 2）定期扫描，长期未确认/未触达的记忆降级或归档

## 活性状态（dream.py 维护）

每条记忆 frontmatter 可选：
- `status: active | dormant | archived`
- `confirmations: N`（被引用/确认次数）
- `last_used: YYYY-MM-DD`

dream.py 按 half-life 衰减：workflow 半衰期 90 天、feedback dormant 180 天 / archive 365 天。

---

## 当前记忆条目

> Agent 维护时自动更新本段。新建记忆后在对应分类下追加一行。

### feedback/
- （待沉淀）

### project/
- （待沉淀）

### reference/
- （待沉淀）

### workflows/
- （Phase 2 安装后由 skill 写入可复用 workflow 模板）
