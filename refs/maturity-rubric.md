# Maturity Rubric · 仓库规模信号参考

> 全部量化、机械可判。**不再决定"装不装"**——Phase 1/2/3 全量必装（见 SKILL.md）。
> 本 rubric 的用途：① Phase 0 向用户呈现仓库画像；② 调整参数阈值；③ 小库风险提示。
> 输入信号来自 Phase 0 体检扫描 + health_report 产出。

## 输入信号（机械读取）

| 信号 | 来源 | 说明 |
|------|------|------|
| `notes_count` | 扫描 `*.md`（排除 `.meta/`/`.index/`/`.obsidian/` 等） | 笔记总数 |
| `office_docs_count` | 扫描 OFFICE_EXTRACT_EXTS 扩展名（排除同上 + 隐私目录） | office 文档数（提取后进入检索） |
| `office_legacy_count` | 扫描 `.doc/.xls/.ppt` | 老格式数（不可提取，需转存提示） |
| `orphan_ratio` | `活跃孤儿 / notes_count`（无 `[[wikilink]]` 反链的笔记占比） | 关联密度 |
| `has_host_field` | 扫描 `.md` frontmatter 含 `host:` 字段的文件数 | 多机协作信号 |
| `git_author_count` | `git log --format='%ae' | sort -u | wc -l` | 多贡献者信号 |
| `has_governance_docs` | 是否已存在 CONSTITUTION.md / converge 痕迹 | 已治理信号 |

## 画像与提示规则（命中即向用户呈现）

```
IF notes_count + office_docs_count < 10:
    → 提示："极小仓库，harness 自重可能超过内容本身，建议先攒内容再 bootstrap"
      （用户仍可选择继续——全量安装，但 LLM 步骤产出的价值有限）

IF office_docs_count > notes_count:
    → 提示："office 文档为主的仓库——检索主要依赖 extract_office 提取质量；
      表格/图表/图片内文字不保真（见 SKILL.md 诚实标注）"

IF office_legacy_count > 0:
    → 提示："检测到 N 个 .doc/.xls/.ppt 老格式，需转存 .docx/.xlsx/.pptx 才能纳入检索"

IF orphan_ratio > 0.3:
    → 提示："孤儿占比高，bootstrap 后建议跑 ask.py --orphans 并按 health-report 建议补链"

IF has_host_field ≥ 2 OR git_author_count ≥ 2:
    → 提示："检测到多机/多贡献者信号——PRIMARY_HOST 必填，并确认单主机维护假设是否成立"

IF has_governance_docs:
    → 提示："检测到既有治理文档，Phase 3 安装前需核对边界，避免覆盖"
```

## 参数阈值建议（写入 .env）

| 规模 | INBOX_ALERT_THRESHOLD | ORPHAN_ALERT_THRESHOLD | SPARSE_CATEGORY_MIN |
|------|----------------------:|-----------------------:|--------------------:|
| < 100 篇 | 5 | 10 | 2 |
| 100-500 篇 | 5 | 20 | 3 |
| > 500 篇 | 10 | 40 | 3 |

## 输出格式（呈现于 Phase 0 决策点 ③ + 记录进 kb-bootstrap-plan.md）

```markdown
## 仓库画像

- notes_count: <N> · office_docs_count: <N>（legacy: <N>）
- orphan_ratio: <0.xx>
- has_host_field: <N> · git_author_count: <N>

**安装范围**: Phase 1 + 2 + 3（全量必装）
**提示**: <命中规则的提示语，逐条>
**参数阈值**: <按规模档建议，用户确认后写 .env>
```

## 边界诚实

- 提示**总是建议、非门控**——最终由用户在交互决策点确认（含是否继续 bootstrap）
- 全量安装对极小仓库仍成立（脚本对空目录/零数据优雅退化），代价只是少量磁盘与一次跑通耗时
