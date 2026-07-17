# Maturity Rubric · Phase 解锁判定规则

> 全部量化、机械可判。无"长大""多 agent 需求"等主观词（消除自指悖论）。
> 输入信号来自 Phase 0 体检扫描 + health_report 产出。

## 输入信号（机械读取）

| 信号 | 来源 | 说明 |
|------|------|------|
| `notes_count` | 扫描 `*.md`（排除 `.meta/`/`.index/`/`.obsidian/` 等） | 笔记总数 |
| `orphan_ratio` | `活跃孤儿 / notes_count`（无 `[[wikilink]]` 反链的笔记占比） | 关联密度 |
| `has_host_field` | 扫描 `.md` frontmatter 含 `host:` 字段的文件数 | 多机协作信号 |
| `git_author_count` | `git log --format='%ae' | sort -u | wc -l` | 多贡献者信号 |
| `has_governance_docs` | 是否已存在 CONSTITUTION.md / converge 痕迹 | 已治理信号 |

## 判定规则（按优先级，命中即建议）

```
IF notes_count < 30:
    → 建议仅 Phase 1a（最小骨架）
    → 提示："仓库较小，建议先攒内容再追加 Phase 1b/2/3"

ELIF 30 ≤ notes_count ≤ 200:
    → 建议 Phase 1a + 1b（完整骨架）

ELIF notes_count > 200 OR orphan_ratio > 0.3:
    → 建议 Phase 1a + 1b + Phase 2（记忆/自沉淀）

IF has_host_field ≥ 2 OR git_author_count ≥ 2:
    → 追加建议 Phase 3（治理/多 agent 协作）

IF has_governance_docs:
    → 提示："检测到既有治理文档，Phase 3 安装前需核对边界，避免覆盖"
```

## 输出格式（呈现于安装范围决策点 + 记录进 kb-bootstrap-plan.md）

```markdown
## Phase 建议

- notes_count: <N>
- orphan_ratio: <0.xx>
- has_host_field: <N>
- git_author_count: <N>

**建议安装**: Phase 1a [+ 1b] [+ 2] [+ 3]
**理由**: <命中规则的一句话>
**用户选择**（交互确认后回填）: 按建议安装 / 仅 Phase 1a / 自选组合 ______
```

## 边界诚实

- `notes_count < 10`：提示"极小仓库，bootstrap harness 的自重可能超过内容本身，建议先攒内容"
- 信号冲突（如 notes>200 但 has_host_field=0）：按更高 Phase 建议（保守追加，用户可裁减）
- 判定结果**总是建议、非强制**——最终由用户在交互决策点中选择（选项见 SKILL.md §Phase 0 决策表），选择结果记录进 kb-bootstrap-plan.md
