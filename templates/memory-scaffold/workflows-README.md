# Workflows · 可复用任务流程

> 每个 workflow 是一个标准化的任务执行模板。Agent 识别任务类型后套用对应 workflow。
> 维护时按 frequency + last_used 衰减（dream.py）。

## 命名

`obsidian-<task>.md`（如 `obsidian-maintenance.md`、`obsidian-query.md`）

## Frontmatter

```yaml
---
type: workflow
task: maintenance | query | synthesis | semantic-lint | ...
frequency: 0.0        # 最近 30 天触发频次（maintain 自动更新）
last_used:            # 最近触发日期
steps: N              # 步骤数
---
```

## 触发衰减（dream.py）

- 半衰期 90 天，2× 半衰期（180 天）且 frequency < 0.05 → 标记降级候选
- 长期未触发的 workflow 经用户确认后归档

## 初始 workflow（Phase 2 安装时写入）

- `obsidian-maintenance.md` — 维护管线标准流程
- `obsidian-query.md` — 查询/检索流程
- `obsidian-synthesis.md` — 主题合成流程（按需）
- `obsidian-semantic-lint.md` — 语义 Lint 流程（按需）
