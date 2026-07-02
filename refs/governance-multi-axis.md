# Governance Multi-Axis · 治理体系多轴概念指针

> Phase 3（治理）安装时引用。本文件不重复定义治理体系，只给指针与最小安装说明。

## 总称

**知识库治理体系**——涵盖受门控的文件、脚本、派生数据保护与门控机制的整体。

> **消歧**：本体系指**受门控的文件 / 门控轴集合**，区别于 converge / deliberate / audit 三个**判断动作治理模块**（若安装）。

## 门控轴（开放可扩展，当前 3 条）

### ①编辑门控轴（按"改动触发的审批强度"）

| 类 | 门控 | 判据 |
|----|------|------|
| **治理文档** | 改 → 多 agent 评议（≥3 Reviewer + 收敛 + 设计审查） | 对 Agent 行为有规范性约束力（语义判据为准，hook 正则为近似实现） |
| **持久型文件** | 改 → 人工审议（修宪程序） | 手动维护、丢失不可重建（见 CONSTITUTION 持久型清单） |

### ②从机访问轴（按"哪台 host 可写"）

| 类 | 门控 | 判据 |
|----|------|------|
| **从机禁改清单** | 从机写 → 拒绝 | 非 PRIMARY_HOST 不可写（维护层 + 持久型文件） |

### ③隐私读轴（按"内容敏感度限制读取"）

| 类 | 门控 | 判据 |
|----|------|------|
| **隐私目录** | 读 → 禁 Read/Grep | Phase 0 嗅探 + category-privacy.md 定义 |

## Phase 3 最小安装

Phase 3 默认不装。触发条件（maturity-rubric）：`has_host_field ≥ 2` 或 `git_author_count ≥ 2`。

安装时：
1. 引用独立 converge SKILL（`~/.agents/skills/converge/`）作为收敛/评议机制源
2. 在目标仓库建 `.meta/converge/` `.meta/deliberations/` `.meta/audit/` 三目录（仅 README charter）
3. pre-commit hook 升级到完整版（加 GOV 文档变更检测、plan status 检查、converge 路径纠正）
4. CONSTITUTION 升级到完整版（六部 + 第五点五部详述）

> 不复刻 converge SKILL 内容——它有独立宪法与自举治理，本仓库只引用。
