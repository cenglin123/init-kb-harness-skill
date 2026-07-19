---
type: charter
generated_at: <安装日期>
source: init-kb-harness
---

# Converge — 本地适配层

> 机制、角色、prompt、state schema、预算、收敛判定与 Archive Contract 以全局
> `~/.agents/skills/converge/SKILL.md` 及其 refs 为唯一权威源。
> 本文只记录本仓库的路径绑定与生命周期位置，不复制全局机制内容。

## 在 plan 生命周期中的位置

`起草 plan → converge 到可执行 → 按授权执行 → fresh verifier 验收 → 用户确认后 done`

- converge 只负责**执行前收敛**，不存执行后验收。
- 用户说"评议 / deliberate / 收敛"时进入 converge 流程。
- 执行后"审计 / 复审 / 检查对齐"回到对应 active plan 的 `review`，由 fresh verifier
  按 `docs/plans/README.md`（若装了 init-agent-docs 的 plan 合同）验收；验收证据写入 plan。
- 本仓库**不设** deliberations / audit 独立目录——单一 plan 生命周期已覆盖两者职能。

## 本地路径

```text
.meta/converge/
├─ active/<YYYYMMDD-slug>/   # 正在收敛
└─ done/<YYYYMMDD-slug>/     # 收敛证据归档
```

产物路径经 `.env:CONVERGE_DIR=.meta/converge` 绑定；pre-commit hook 拦截误落
`.converge/` 的产物。`active/` 与 `done/` 均纳入 git 跟踪（收敛证据是持久型资产，
不进 .gitignore）。

## 治理文档风险档

治理文档清单以 `.meta/governed-files.txt` 为机械 SSOT（每行一个 glob，pre-commit
hook 消费）。治理文档修改走 **ultraverge**：首轮至少 3 个独立 reviewer、至少 2 个
lineage、至少 1 个 fresh-context 子代理；conceptual/architectural 阻断进入原生循环；
执行前完成设计审查。实施后另由 fresh verifier 验收。
