# Governance Multi-Axis · 治理体系多轴概念指针

> Phase 3（治理）安装时引用。本文件不重复定义治理体系，只给指针与安装说明。

## 总称

**知识库治理体系**——涵盖受门控的文件、脚本、派生数据保护与门控机制的整体。

> **消歧**：本体系指**受门控的文件 / 门控轴集合**，区别于 converge **判断动作模块**
> （执行前收敛 + 执行后 fresh verifier 验收的单一 plan 生命周期）。

## 计划质量控制：单一 plan 生命周期

`起草 plan → converge 到可执行 → 按授权执行 → fresh verifier 验收 → 用户确认后 done`

- **执行前**：converge（机制权威源 = 全局 `~/.agents/skills/converge/SKILL.md`；本地只绑路径）
- **执行后**：fresh verifier 独立验收，证据写入 plan

## 门控轴（开放可扩展，当前 3 条）

### ①编辑门控轴（按"改动触发的审批强度"）

| 类 | 门控 | 判据 |
|----|------|------|
| **治理文档** | 改 → ultraverge（≥3 独立 reviewer、≥2 lineage、≥1 fresh-context） | 对 Agent 行为有规范性约束力；机械边界 = `.meta/governed-files.txt`（SSOT）。plan 文件本身不入 SSOT——它走 converge 生命周期管控，不走 ultraverge 门 |
| **持久型文件** | 改 → 人工审议（修宪程序） | 手动维护、丢失不可重建（见 CONSTITUTION 持久型清单） |

### ②从机访问轴（按"哪台 host 可写"）

| 类 | 门控 | 判据 |
|----|------|------|
| **从机禁改清单** | 从机写 → 拒绝 | 非 PRIMARY_HOST 不可写（维护层 + 持久型文件） |

### ③隐私读轴（按"内容敏感度限制读取"）

| 类 | 门控 | 判据 |
|----|------|------|
| **隐私目录** | 读 → 禁 Read/Grep | Phase 0 嗅探 + category-privacy.md 定义 |

## Phase 3 安装（必装）

Phase 3 随 bootstrap 必装（不再按多用户信号按需触发）。安装步骤：

1. 引用独立 converge SKILL（`~/.agents/skills/converge/`）作为收敛/评议机制源——**不复刻其内容**，它有独立宪法与自举治理，本仓库只引用
2. 建 `.meta/converge/{active,done}/` + README charter（来自 `refs/converge-readme-template.md`）
3. 生成 `.meta/governed-files.txt`（来自 `refs/governed-files-template.txt`，按目标仓库实际治理边界增删）
4. `.env` 设 `CONVERGE_DIR=.meta/converge`（converge 产物路径绑定；hook 拦截误落 `.converge/`）
5. pre-commit hook 用统一模板（`refs/pre-commit-template`，已含 plan status / converge 路径纠正 / GOV 变更提醒）
6. CONSTITUTION 含治理体系条款（`refs/constitution-template.md` 第三部）

> 若目标仓库已存在治理文档（CONSTITUTION / converge 痕迹），安装前先核对边界，避免覆盖既有制度。
