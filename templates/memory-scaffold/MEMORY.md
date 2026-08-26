# Memory 指针

> 记忆系统索引。各 Agent 启动时读本文件了解有哪些可用记忆。
> 记忆是 agent 的私有笔记，存放用户画像、偏好纠正等 agent 专属物。
> 知识库内容（可复用流程、项目上下文、外部参考等）由用户笔记目录承担，不重复存入记忆。

---

## 目录结构

```
.meta/memory/
├── MEMORY.md              # 本文件（索引）
├── user/
│   └── role.md            # 用户画像（用户直接编辑；Agent 追加不覆盖）
└── feedback/              # 反馈记忆（用户偏好/纠正）
```

> 其余子目录按需涌现（如特定领域的 agent 观察），不预建。内容类信息（流程/项目/参考）存用户笔记目录。

## 记忆规则（写入 frontmatter）

- **触发记录**：用户说"记住 X" / agent 发现反复出现的偏好或决策模式
- **不记录**：可从笔记原文/git log 推导的事实；一次性会话细节；可由知识库内容目录承担的信息
- **更新时必须更新 frontmatter 的 `last_updated` / `generated_at`**
- **衰减**：dream.py 定期扫描，长期未确认/未触达的记忆降级或归档

## 活性状态（dream.py 维护）

每条记忆 frontmatter 可选：
- `status: active | dormant | archived`
- `confirmations: N`（被引用/确认次数）
- `last_used: YYYY-MM-DD`

dream.py 按 frontmatter `type` 字段匹配衰减策略，按 half-life 衰减。

---

## 当前记忆条目

> **本段由 `.meta/scripts/memory_index.py` 自动维护（硬约束），禁止手改。**
> 每次 `maintain.py` 运行时重建；pre-commit hook 校验索引与磁盘实际内容一致。
> 新建/删除/重命名记忆文件后，直接运行 `python .meta/scripts/memory_index.py` 刷新。

<!-- memory-index:start -->
（暂无）
<!-- memory-index:end -->
