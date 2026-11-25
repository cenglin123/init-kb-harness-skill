# 脚本清单（全量安装，一次拷齐）

Phase 1 将 `scripts/` 下全部 25 个 KB 脚本一次拷入目标仓库 `.meta/scripts/`（已有 `init-agent-docs` 的 `changelog.py` 时不再部署等价 `changelog_append.py`；各层共用同一套脚本）。

---

## 管线入口与检索

| 脚本 | 功能 | 依赖 | CLI |
|------|------|------|-----|
| `maintain.py` | 维护主入口（全管线编排，见文件头）；语义层默认启用（内置模型），LLM 层看 `HARNESS_MODE=full` + key；语义层缺失/失败时退出码 3 | 编排下方各脚本 | `--full`（重建本地派生件，不强制重嵌入）/ `--no-git` / `--semantic-lint` / `--skip-changelog` |
| `ask.py` | 语义检索 / 查重 / 孤儿 / 反链 / 图遍历；**语义层运行时不可用（库空/维度不符/端点断/索引损坏）时自动降级**：默认查询与 `--check` 走 BM25（含 `--scope` 过滤），两者都不可用时退出码 2；`--deep`/`--rerank` 需 LLM 层；有嵌入库时**低置信自动升级**（top-1 < 阈值自动 hybrid/deep/rerank 合并，统计写 `.meta/escalation-stats.jsonl`，阈值 `.env:ESCALATION_THRESHOLD` 可调） | `embeddings.sqlite` + `graph.json` + bm25 索引；embeddings 端点（查询）、DeepSeek（`--rerank`/`--deep`） | `"query"` / `--check` / `--orphans` / `--backlinks` / `--neighbors` / `--path` / `--deep` / `--bm25` / `--hybrid` / `--rerank` / `--scope` / `--decay` / `--save` |
| `whoami.py` | 模型/provider 自检（两层识别：harness 路由 codex/claude-code/opencode → 内部 provider）；`--frontmatter` 输出溯源 YAML（`agent`/`model`/`generated_at`，`WHOAMI_AGENT` env 可显式指定宿主） | 本地只读，零 API | 无参 / `--frontmatter` |

---

## 提取与索引层

| 脚本 | 功能 | 依赖 | CLI |
|------|------|------|-----|
| `extract_office.py` | office 文档（`.docx`/`.xlsx`/`.pptx`/`.pdf`）纯文本提取 → `.meta/office-extracts/` sidecar；增量按 hash；孤儿清理；老格式登记 `_legacy-formats.md`；**子进程并行**，单文件超过 `OFFICE_EXTRACT_TIMEOUT_S`（默认 120s）即终止跳过并登记 `_timeouts.md` | `python-docx`/`openpyxl`/`python-pptx`/`pypdf`（缺装时对应格式跳过并提示） | `--full` |
| `embed.py` | 笔记 + office 提取件 + memory → `embeddings.sqlite`；**默认内置本地模型（fastembed），可切任意 OpenAI 兼容 `/embeddings`**；HTTP 按 `EMBED_BATCH` 切请求批并发、内置扁平批量；两者都按 `EMBED_FILE_BATCH`/`EMBED_CHUNK_BATCH` 分批并**立即写库**；先探针后写库；配置变更**拒绝增量并提示 `--full`**，`--full` 先写临时表再原子换表；大文件闸门：超阈值且无 `allow` 决定的文件硬性跳过（`llm_skip` 决定同时清除历史向量） | HTTP 端点 或 fastembed | `--full` |
| `summarize.py` | 每篇生成摘要 / tag sidecar；**跨文件并发 API**（LLM 层；`HARNESS_MODE=full` 时由 maintain 调用）；大文件闸门同 `embed.py` | DeepSeek | `--full` |
| `build_index.py` | `.index/manifest.md` + 分类清单 + `topics.md`（office 文档以 📎 标记纳入） | 本地 | 无参 |
| `calibrate_thresholds.py` | 只读采样当前嵌入库的相似度分布，输出建议的 `LINKS_SIM_THRESHOLD` / `LINKS_HIGH_THRESHOLD` / `SEMANTIC_THRESHOLD` / `ESCALATION_THRESHOLD`（换模型或换语料后跑一次） | 本地（需 `embeddings.sqlite`） | `--limit` / `--json` |
| `build_links.py` | `.meta/links/` 语义关联 sidecar（纯余弦 + wikilink，**无 LLM**）；属语义层，按 `content_hash` + 模型 + 阈值增量；graph 的 semantic 边读它（`--orphans` 只看用户手写链接） | 本地（需 `embeddings.sqlite`） | `--full` |
| `build_graph.py` | 全局图谱 `graph.json`（wikilink + semantic 边）+ 断链报告 | 本地（semantic 边来自 `build_links` 产物；语义层关闭时只有 wiki/md 边） | 无参 |
| `bm25_index.py` | BM25 稀疏倒排索引（自实现算法、零 API、零嵌入库依赖；**增量构建**——词频缓存只重算变化文件，并行分词，lite/full 均可用）→ `.meta/bm25_index.pkl.gz`（+ 文档词频缓存 `bm25_doc_cache.pkl.gz`；`--rebuild` 忽略缓存全量重建） | 本地 | `--build` / `--rebuild` / `--query "xxx" --top-k N` |

---

## 派生报告与自沉淀层

| 脚本 | 功能 | 依赖 | CLI |
|------|------|------|-----|
| `knowledge_map.py` | 消费 `graph.json` → `.meta/knowledge-map.md`（god nodes / 跨域连接 / 社区 / 偏差信号） | 本地零 API | 无参 |
| `health_report.py` | `.meta/health-report.md`（孤儿/收件箱/稀疏分类 + **语义层覆盖率/模型/新鲜度/挂起与超时** 告警，阈值 env 可调） | 本地（收件箱建议需 LLM 层，未启用时只列文件） | 无参 |
| `dream.py` | 记忆活性扫描 + 衰减预警 + 唤醒检测 → `.meta/dream-report.md`（所有建议均为提案不自动执行） | 本地零 API（git + frontmatter） | `--full`（预留） |
| `semantic_lint.py` | P0 断链 / P1 孤儿概念·过时标记（本地启发式）/ P2 矛盾检测（DeepSeek，**LLM 层**）→ `.meta/semantic-lint-report.md` | P2 需 DeepSeek | 默认 `--quick` / `--deep` |
| `synthesize.py` | 主题合成（多篇聚合生成综述）→ `.meta/syntheses/`（**LLM 层**；未启用时由 agent 直接读文兼任） | DeepSeek | `--theme` / `--scope`（glob，逗号分隔）/ `--prompt` / `--max-notes` |

---

## 支撑脚本

| 脚本 | 功能 |
|------|------|
| `common.py` | 公共库：env 加载 / API client（含跨线程全局限速）/ 扫描与排除 / office 与并发配置 / 链接解析 / git 包装 |
| `check_gov_consistency.py` | 治理边界一致性校验（SSOT / AGENTS 明线 / hook `GOV_PATTERNS` 三方比对；bootstrap 同批 staged 时豁免）；pre-commit 第 9 段在治理边界文件变更时自动触发 |
| `memory_index.py` | `MEMORY.md` 记忆索引自动重建（硬约束，索引标记段禁止手改）；`--check` 供 pre-commit 校验 |
| `detect_renames.py` | 重命名检测（git + hash 双通道），迁移伴生元数据 |
| `check_sidecar_sources.py` | 校验/修复 `.meta/{summaries,links,tags}/` sidecar 的 `source` 字段 |
| `host_guard.py` | 宿主护栏动态门禁（**Phase 1.5 选用**，不配 hook 则为无害闲置）：pre-tool 判定（隐私读禁/从机写禁/编辑重定向）、prompt 触发词提醒、compact-context 注入文本、`simulate-host` dry-run；策略源 `.meta/rules/host-write-policy.json` |
| `inbox_scan.py` | 收件箱扫描 + LLM 归类建议（`health_report.py` 调用） |
| `changelog_append.py` | 旧库兼容追加器；已有 `init-agent-docs` `changelog.py` 时不部署 |
| `check_plan_status.py` | `docs/plans` 路径（`active/`、`completed/`、`not_planned/`，兼容 `done/`）与 frontmatter `status` 一致性（pre-commit 调用） |

## 来自 `init-agent-docs` 的重定位脚本

| 脚本 | 功能 |
|------|------|
| `docs_maintain.py` / `audit.py` / `check_all.py` / `changelog.py` | 从 `init-agent-docs` 源按 [agent-docs-layout.md](agent-docs-layout.md) 重定位至 `.meta/scripts/`；负责文档审计、定期提醒、完工检查与 CHANGELOG；`docs_maintain.py` 委托唯一 `memory_index.py` |

> `maintain.py` 的 workflow frequency 步骤依赖 `search_sessions.py`（vault 特化脚本，bundle 不含）——缺失时自动跳过并告警。
