# 知识库的 agent-docs 布局迁移

在 init-agent-docs 打底后、KB 脚本拷贝前执行；已有知识库用户明确请求统一布局时，单独执行本流程，不能重跑整个 bootstrap。源仓库是独立 `cenglin123/init-agent-docs` 与 `cenglin123/init-kb-harness-skill`，不是 monorepo 子目录。只改来源，再由 cc-switch 部署 skill；本流程操作的是目标知识库资产，不改宿主部署的 skill 副本。

## 唯一目标布局

| 原目标资产 | 知识库目标 | 职责 |
|---|---|---|
| scripts/changelog.py | .meta/scripts/changelog.py | CHANGELOG 局部读写 |
| scripts/audit.py | .meta/scripts/audit.py | 文档一致性只读审计 |
| scripts/check_all.py | .meta/scripts/check_all.py | 完工机械检查 |
| scripts/maintain.py（文档版） | .meta/scripts/docs_maintain.py | 审计、30 天提醒、上下文；索引委托 memory_index |
| scripts/worktree_task.py（若安装） | .meta/scripts/worktree_task.py | 任务隔离与协作 helper，更新调用引用；已有 hook 调用随路径迁移 |
| .agents/memory/** | .meta/memory/** | 持久记忆；KB 索引为唯一派生入口 |

`worktree_task.py` 是可选协作桥接的一部分：从新版 init-agent-docs 的 `assets/scripts/` 迁入或复用同一 helper，不在 KB bundle 再放一份。其登记位置由 helper 解析到 Git common-dir，同一 clone 的 linked worktree 共用该 registry；不同 clone 或设备不会自动共享。没有 Git 或用户未启用时，不安装此 helper，不创建 worktree 或 registry。主从限制与派生数据的主机整合规则见 `refs/agents-template.md`。

`.meta/scripts/maintain.py` 永远是 KB 全管线入口。通用项目仍保持 init-agent-docs 原布局；这些脚本必须来自支持 `.meta/scripts/` 的新版 agent-docs 源，不能把旧脚本只搬目录：旧 ROOT=parents[1] 会把 `.meta` 当项目根。

## 盘点与执行记录

1. 读取目标库规则、AGENTS/docs/STRUCTURE、当前 hooks、旧 scripts 全部文件及 `.agents/memory/` 全部内容；识别文档版 maintain（不得把用户自定义或 KB 版同名脚本视为文档版）。列出源→目标、目标冲突、定制脚本、外部/软链接、所有活跃调用者。
2. 在 `kb-bootstrap-decisions.md` 记录清单、选择和迁移版本 `agent-docs-meta-v1`。用户已明确授权本迁移时沿用授权；缺少授权且需删除/覆盖旧资产时，展示具体清单后问“迁并并移除旧资产 / 仅迁并保留 / 跳过”。未知内容或越界链接必须单独处理，不能通过一个泛化选项获得整个目录的删除权限。
3. 在 `.meta/migrations/agent-docs-meta-v1/` 备份本次涉及文件、待更新的导航/配置/hook，保存相对路径与 SHA256 清单；不复制密钥或 `.env`，不把含个人信息的日志/路径写进可提交记录。备份在全部验证完成前保留。中断后依记录逐项续做，已有相同内容不再重复追加。

## 脚本与引用

1. 从新版 init-agent-docs 的 `assets/scripts/` 安装 changelog/audit/check_all；maintain 安装为 docs_maintain.py。旧目标有用户定制时先与源对比，把必要定制迁入新版，不能直接覆盖。可选 worktree_task 与自定义维护脚本也逐项迁入 `.meta/scripts/`，检查它们的 ROOT、相邻文件访问和子进程参数；不能假定所有脚本都无需改动。
2. 更新活跃 AGENTS、docs/STRUCTURE、审计清单、README、任务配置和 hook 中的真实目标路径；root `scripts/maintain.py` 改为 KB 主入口，若调用意图是 `--check`/`--memory-index` 则分别改成 docs_maintain.py --check / memory_index.py（参数不能机械传给 KB maintain）。普通文档工具指针改到 `.meta/scripts/`，旧记忆指针改到 `.meta/memory/`。相对 Markdown 链接按所在文件位置重算。已完成计划和 CHANGELOG 的历史记录不改写；不能把 source `assets/scripts/` 替换为目标路径。
3. 拷 KB 管线，保留上述文档工具；已有 changelog.py 则不另部署 changelog_append.py 的等价物。KB maintain 自动以 `--check` 调用 docs_maintain；后者委托 memory_index，不生成第二份 MEMORY 标记段。audit.py 的记忆检查须实际选择 `.meta/memory/`，不能悄悄跳过无 `.agents/memory/` 的库。

## 记忆迁并（全部内容，非只 role.md）

1. `.meta/memory/` 缺失时先补缺失骨架，已有自由文本绝不覆盖。扫描旧 `.agents/memory/` 的全部文件，不只 user/role.md：同路径同字节跳过；不同路径迁入；同路径不同内容先展示两边，保留目标自由文本、去重后合并无冲突内容。存在语义冲突时保留原始来源为独立可检索条目并请用户裁决，不能用“以新为准”静默抛弃旧信息。记录每项来源哈希及结果，重复执行不重复合并。
2. 旧 MEMORY.md 是“自由文本 + 派生索引”，不能整份丢弃：保留索引标记段外有价值内容，更新其中活跃链接；旧派生段不并入新派生段。必要时保留为 `.meta/memory/migrated/agent-docs-memory.md`，不要复制旧索引当第二个 MEMORY 入口。
3. `docs/problems/bugfix/` 原文保留；KB memory_index 会把该目录非 `_` 开头的 Markdown 纳入同一派生索引，原经验检索覆盖不能丢失。重建索引后逐项检查旧记忆、角色和 bugfix 的可发现性。

## 验证后移除旧入口

全部从目标库根运行（使用目标设备实际 Python；新 bootstrap 默认 lite，不为迁移引入 API/生成模型）：

```sh
python .meta/scripts/memory_index.py
python .meta/scripts/memory_index.py --check
python .meta/scripts/changelog.py titles --limit 5
python .meta/scripts/audit.py check
python .meta/scripts/docs_maintain.py --check
python .meta/scripts/check_all.py --quiet
python .meta/scripts/maintain.py --full --no-git --skip-changelog
```

审计/索引失败就修复，不能以仅能 --help 作为路径迁移通过。30 天提醒保留为 WARN，不代替人工裁决；已有库 `--full` 可能改索引/派生物，沿用目标库主从权限和用户维护授权，从机仅做只读校验，写入测试在主机或隔离副本上运行。自定义脚本和 worktree hook 按实际接口增加冒烟；检查 `git config core.hooksPath`、`bash -n .githooks/pre-commit` 与已启用 hook 的真实调用路径。

确认新脚本/导航和记忆完整后，只移除已迁并并验证的旧文件及空子目录。根 scripts 有未知文件就保留并报告未完成项；`.agents` 仅在完全为空时 rmdir，`.agents/skills`、宿主配置和其他用户资产不动。不得 `rm -rf .agents`。迁移成功状态记入 decisions；再跑 audit/index/check_all，并用 `.meta/scripts/changelog.py add` 追加迁移说明。重跑时已无旧入口则仅验证，内容哈希不变、不重复追加记忆或日志。

验收：标准打底的新库根 scripts 不存在；非空 `.agents` 只允许其他既有资产，旧 memory 不存在；所有活跃记忆/维护指针仅用 `.meta/`；KB 全管线与文档审计均保留。用户选择保留/跳过或遇未知内容时必须如实记录未达标，不能声称布局迁移完成。
