# Dream 后台记忆整合设计（mem0 自托管记忆）

| 项 | 值 |
| --- | --- |
| 目标仓库 | `/Users/xuelingkang/Documents/Containers/mem0` |
| 前置基线 | `b0b4e38d`（bi-temporal 事实模型已成稿并实现） |
| 运行时 | Docker Compose 四服务（`server/docker-compose.yaml`：mem0 / postgres / qdrant / dashboard），源码经 volume 挂载 |
| 存储 | Qdrant 1.19.0，集合 `memories_2048`（2048 维，含 `bm25` sparse 槽位）；PostgreSQL `mem0_app` 承载运行记账 |
| 影响面 | 新增后台整合子系统；写入路径只新增一个候选池过滤条件；检索排序零改动 |
| 实现形态 | 仅本地改动（相对 `upstream/main`），不提交上游 PR |

> 本文档为本地设计文档，位于仓库 `docs/design/`，与 Mintlify 文档站（`docs/**/*.mdx`）无关，不参与 `llms.txt` 收录检查。

---

# 1. 项目概述

## 1.1 背景

自托管 mem0 承载用户长期记忆，当前 3532 条（2026-09-17 05:57 UTC 快照，随日常使用持续增长）。这些记忆是**扁平的、彼此孤立的单条事实**：

- 同一主题下的事实散落在几十条独立条目中，没有任何一条表述它们共同指向的结论；
- 事实之间的关联只能靠向量相似度在检索时临时拼接，系统自身不持有任何「信念」。

事实层面的时间演化已由 bi-temporal 事实模型承担（`valid_at` / `invalid_at`，失效不删除）。缺的是**跨会话、跨时间的后台整合**：把一组相关事实精炼成一条更高层的观察（observation），并保留这条观察对源事实的可追溯关系。

## 1.2 目标

1. 定期把同一作用域内彼此相关的既有事实精炼为**观察**条目，写回同一存储，形成可读的信念层。
2. 每条观察携带**源记忆 id 列表与证据计数**，正反两个方向都可追溯。
3. 观察是**新增**，源事实保持原样：文本、hash、入库时间、时间字段都不因整合而改变。
4. 整合落地是**幂等**的：同一集合重跑不产生重复观察；中断后可续跑，不重复调用已处理过的簇。
5. 提供 **dry-run**：先产出候选报告供审阅，不写任何数据。
6. 整轮整合不进入写入路径、不改检索排序：用户 `add` / `search` 的时延与结果集不受整合影响。

## 1.3 范围

包含：

- `mem0/memory/dream.py`（新增）：四阶段流程（Orient → Gather → Consolidate → Prune）与全部纯函数判定。
- `mem0/configs/prompts.py`：新增观察合成提示词与其 user prompt 构造函数。
- `mem0/memory/main.py`：写入路径 Phase 1 候选池排除观察条目（一处过滤条件）；不新增任何 LLM 调用。
- `mem0/vector_stores/qdrant.py`：新增三个 payload 索引字段声明。
- `server/dream_scheduler.py`（新增）：后台周期触发与 lock-file 并发控制。
- `server/routers/dream.py`（新增）：dry-run、手动触发、运行记录、反向追溯四组 HTTP 端点。
- `server/main.py` / `server/models.py` / `server/alembic/versions/`：运行记账表、序列化一等字段、检索参数。
- `tests/memory/test_dream.py`、`tests/test_dream_router.py`（新增）。

职责边界见第 9 节。

---

# 2. 需求分析

## 2.1 功能需求

| 编号 | 需求 |
| --- | --- |
| F1 | 按作用域（`user_id` + `agent_id`）分别整合，观察不跨作用域产生 |
| F2 | 相关事实的判定基于已入库向量，不使用 LLM 做分组 |
| F3 | 每个候选簇一次 LLM 调用，产出至多一条观察；合成不出更高层结论时该簇不产出 |
| F4 | 观察的源事实 id 只能取自该簇成员，非法 id 一律丢弃 |
| F5 | 每条观察携带 `source_memory_ids`、`evidence_count`、`observation_key` |
| F6 | 观察条目与事实条目共存于 `memories_2048`，通过 `memory_kind` 区分 |
| F7 | 同一成员集合重跑命中同一条观察（覆写），条目数不增 |
| F8 | 成员集合演化时，旧观察获得失效标记并指向新观察 |
| F9 | 默认读不返回观察条目；显式请求时纳入 |
| F10 | dry-run 产出候选报告，零数据写入 |
| F11 | 整轮整合由周期后台触发，并发实例互斥 |
| F12 | 单簇失败不中断整轮；整轮中断后可续跑 |
| F13 | 已判定过的簇（含判定为「提炼不出」的）在后续轮次不再重复调用 LLM |

## 2.2 非功能需求

| 项 | 要求 |
| --- | --- |
| 写入路径开销 | 每次 `add()` 的 LLM 调用次数不变（仍为 1a + 1b 两次）；仅新增一个 Qdrant 过滤条件，命中 payload 索引 |
| 读路径开销 | 默认读新增 1 个 Qdrant 过滤条件；不新增排序权重、不新增打分项 |
| 幂等性 | 同一输入重复执行不改变观察条目数与内容 |
| 失败隔离 | 单簇 LLM 异常、超时、返回不可解析内容，均只影响该簇 |
| 资源占用 | 不新增容器、不新增外部服务依赖；沿用既有 numpy |
| 可复现 | 聚类与全部剪枝规则为确定性纯函数，同输入同输出 |
| 可降级 | 整合子系统完全停用（不启动调度线程）时，主链路行为与整合前一致 |

---

# 3. 总体架构设计

## 3.1 四阶段的职责与交割

四阶段严格单向：每一阶段只做一件事，阶段之间以明确的数据结构交接。

```mermaid
flowchart LR
    A["Orient<br/>只读"] -->|scope 清单 + 统计| B["Gather<br/>只读"]
    B -->|候选簇 [{scope, members}]| C["Consolidate<br/>每个簇 1 次 LLM"]
    C -->|观察候选 [{key, text, source_ids}]| D["Prune<br/>纯代码 + 唯一写入点"]
    D -->|新增/覆写 observation<br/>失效标记| E["Qdrant memories_2048"]
    D -->|运行记录与报告| F["Postgres dream_runs"]
```

| 阶段 | 职责 | 输入 | 输出 | 边界 |
| --- | --- | --- | --- | --- |
| Orient | 确定本轮要处理的作用域，并汇总存量口径 | 集合全量 payload 的 `user_id` / `agent_id` / `memory_kind` / `observation_key` | `{scopes: [(user_id, agent_id)], totals: {facts, observations, pending}}` | 只读；不调用 LLM；不做任何写 |
| Gather | 在每个作用域内把相关事实聚成候选簇，并算出每簇的成员指纹 | scope 清单、各作用域成员的向量与 payload、已判定键集合 | `[{scope, members: [member_id], observation_key, skipped_reason}]` | 只读；不调用 LLM；确定性纯函数 |
| Consolidate | 对每个待处理簇合成至多一条观察 | 簇成员（id / 文本 / 入库时间）、既有 `observation_key` 集合 | `[{observation_key, members, text \| null, source_ids, counterexample}]` | 每簇一次 LLM 调用；只产出候选，不写入；不判断保留/失效 |
| Prune | 剪掉不合格候选，落地合格观察，处置被取代的旧观察 | 观察候选、簇成员、既有观察全集、已判定键集合、当前时刻 | 写入结果 `{written, superseded, skipped}` + 状态表行 + 运行记录 | **全流程唯一改变记忆数据的阶段**；源事实零写 |

**交割顺序**：Orient 与 Gather 都不触网（除 Qdrant 读），因此 dry-run 可以完整执行这两阶段。Consolidate 是唯一产生 LLM 成本的阶段。Prune 是唯一改变数据的阶段——dry-run 在此阶段只产出报告不落库。

**簇级流水线**：Consolidate 与 Prune 按簇交替推进（处理完一个簇即对该簇执行 Prune 的判定与落地），而不是等全部簇合成完毕再统一落地。理由是 F12 的续跑要求——中断时已完成簇的判定必须已经持久化，否则重跑要重复付出全部簇的 LLM 成本。阶段职责不变：LLM 调用只发生在 Consolidate 逻辑内，数据写入只发生在 Prune 逻辑内。簇的处理顺序固定为「成员数降序，同规模按 `observation_key` 字典序升序」，保证同输入同结果。

## 3.2 与既有写入管道的接缝

整合子系统**独立于** `_add_to_vector_store` 的分阶段管道，两者只在两处相接：

| 接缝 | 位置 | 本方案改动 |
| --- | --- | --- |
| 写入路径 Phase 1 候选池 | `mem0/memory/main.py` 的 `_add_to_vector_store`（sync / async 各一处） | 候选池过滤条件追加「排除观察条目」，使观察不成为 1a 的去重参照、不成为 1b 的矛盾检测对象 |
| 读路径候选集 | `mem0/memory/main.py` 的 `search` / `get_all` | 默认加「排除观察条目」谓词；显式 `include_observations=true` 时不加 |

写入路径的 LLM 调用数、写入流程、失效处置全部不变。整合的周期任务在独立线程中运行，与请求线程不共享任何锁。

## 3.3 状态归属

整合子系统的**正确性不依赖任何自有状态**：幂等由确定性 point id 保证。成本跳过另有一张轻量状态表，其缺失只导致重复判定（更贵），不导致错误结果。

| 状态 | 载体 | 作用 | 缺失时的退化行为 |
| --- | --- | --- | --- |
| 已落地的观察 | `memories_2048` 的 `observation_key` payload | 成员集合相同的簇不再落地（覆写语义） | 不可能缺失（就是数据本身） |
| 已判定的簇 | Postgres `dream_cluster_states` 表 | 成员集合相同的簇不再重新调用 LLM，包括上一轮判定为「提炼不出」的簇 | 重复调用 LLM，结果不变（幂等） |
| 运行审计 | Postgres `dream_runs` 表 | 运行统计与报告引用 | 无影响 |

PostgreSQL 的两张表都不参与正确性判定——表数据丢失不影响整合结果，只影响成本与可观测性。

「已判定的簇」必须记录**全部**判定结果（含 `no_higher_order_pattern`）而不能只记落地的观察：实测中 96 个簇只有 25 个产出观察，若只以落地观察为水位，余下 71 个簇每轮都会被重新调用，稳态成本将从每日 5–20 次调用涨到每日 96 次。

---

# 4. 数据模型设计

## 4.1 观察条目的 payload 字段

观察条目与事实条目同存于 `memories_2048`，共用 `data` / `hash` / `text_lemmatized` / `created_at` / `updated_at` / `user_id` / `agent_id` 等既有字段，另增 5 个字段（全部为 Qdrant payload 顶层一等字段）：

| 字段 | 类型 | 语义 | 写入时机 |
| --- | --- | --- | --- |
| `memory_kind` | `"observation"` | 条目标记。**事实条目不写该字段**（键缺失即「事实」） | 观察落地时 |
| `observation_key` | 32 位十六进制字符串 | 该观察的成员集合指纹，同一组源事实恒得同一 key | 观察落地时 |
| `source_memory_ids` | 字符串数组 | 支撑这条观察的源记忆 id 列表 | 观察落地时 |
| `evidence_count` | 整数 | 等于 `source_memory_ids` 的长度 | 观察落地时，与上者同源写入 |
| `dream_run_id` | uuid 字符串 | 产出该观察的运行 id，用于运行级审计 | 观察落地时 |

字段形态约定：

- 事实条目不写 `memory_kind`——存量记录因此天然是「事实」，无需回填（见第 8 节）。
- 观察条目的 `hash` 取观察文本的 md5，与该文本的重复写入判定一致。
- 观察条目的 `valid_at` 取该簇成员的**最早生效时间**（`valid_at` 缺失的成员以其 `created_at` 的日期兜底），使 point-in-time 查询在成员覆盖的时间区间内能命中该观察。
- 观察条目的 `invalid_at` / `superseded_by` / `invalid_reason` 沿用 bi-temporal 字段，仅在成员集合演化时写入。

## 4.2 观察的标识与闭合性

| 项 | 定义 |
| --- | --- |
| 成员指纹 `observation_key` | `sha256(scope_user_id \| scope_agent_id \| 成员 id 升序拼接)` 取前 32 位十六进制 |
| Qdrant point id | `uuid5(NAMESPACE_URL, observation_key)`——由指纹确定性派生，同一簇恒得同一 point id |
| 幂等语义 | 同一 point id 重复写入是覆写而非追加，条目总数不增 |
| 演化语义 | 新簇与某条既有观察的成员集合 Jaccard 相似度 ≥ 0.6 且指纹不同 → 视为同一支观察的演化，旧条目写入失效标记 |

同一个源事实可以被多条观察引用（不同簇的成员集合可重叠），因此 `source_memory_ids` 是数组而非唯一指针。

## 4.3 运行与状态表

新增两张 Postgres 表（Alembic 迁移 `007`）。

### 4.3.1 `dream_runs`（审计）

| 列 | 类型 | 语义 |
| --- | --- | --- |
| `id` | uuid 主键 | 运行 id，即观察条目的 `dream_run_id` |
| `mode` | 字符串 | `dry_run` / `live` |
| `started_at` / `finished_at` | 带时区时间戳 | 整轮起止 |
| `status` | 字符串 | `completed` / `failed` / `timeout` / `skipped`（lock 未获取） |
| `scopes` | 整数 | 本轮作用域数 |
| `clusters` | 整数 | 待处理簇数 |
| `llm_calls` | 整数 | 实际发起并返回的 LLM 调用数 |
| `failed_clusters` | 整数 | 失败的簇数 |
| `observations_written` | 整数 | 落地（含覆写）的观察数 |
| `observations_superseded` | 整数 | 写入失效标记的旧观察数 |
| `prompt_tokens` / `completion_tokens` | 整数 | LLM 用量 |
| `duration_seconds` | 浮点 | 整轮时长 |
| `report_path` | 字符串 | 报告文件绝对路径 |

### 4.3.2 `dream_cluster_states`（成本跳过）

| 列 | 类型 | 语义 |
| --- | --- | --- |
| `observation_key` | 字符串主键 | 簇成员集合指纹 |
| `scope_user_id` / `scope_agent_id` | 字符串 | 所属作用域 |
| `decision` | 字符串 | `written` / `no_higher_order_pattern` / `unresolvable_sources` / `insufficient_evidence` / `duplicate_text` |
| `observed_point_id` | uuid，可空 | `decision = written` 时的观察 point id |
| `member_ids_hash` | 字符串 | 与 `observation_key` 同值，保留为显式字段便于排查（两者恒等，不引入第二套指纹） |
| `first_seen_at` / `last_evaluated_at` | 带时区时间戳 | 首次与最近一次判定时刻 |
| `evaluations` | 整数 | 判定次数 |

语义：一行代表「这个成员集合已被判定过」。该表的行只增不删；同一 key 再次判定时更新 `last_evaluated_at`、`evaluations` 与 `decision`（以最新判定为准）。

生命周期：不清理。行数与「历史上出现过的不同成员集合数」同阶，远小于记忆条数。

### 4.3.3 报告文件

报告落在 `/app/history/dream-reports/<run_id>.json`（`mem0_history` 卷，容器内可读）。报告文件不参与判定，仅作审阅与留档；报告目录不自动清理。

---

# 5. 各阶段设计

## 5.1 阶段一：Orient（只读）

### 5.1.1 判定规则

| 规则 | 精确语义 |
| --- | --- |
| O1 作用域枚举 | 作用域 = `(user_id, agent_id)` 二元组。两个字段都存在的记录参与枚举；任一字段缺失的记录本轮跳过并计入 `skipped_records` |
| O2 存量口径 | 汇总 `facts`（无 `memory_kind` 字段的记录数）、`observations`（`memory_kind == "observation"` 的记录数）、`pending`（本轮的候选簇数，由 Gather 回填） |
| O3 已判定集合 | 读取 `dream_cluster_states` 的全部 `observation_key`，与 `memories_2048` 中已落地观察的 `observation_key` 求并集，构成本轮的短路基准 |
| O4 空作用域 | 该作用域内事实数 < 簇最小规模时，本轮跳过（不进入 Gather） |

### 5.1.2 输出

```json
{
  "scopes": [{"user_id": "…", "agent_id": "…", "facts": 3129, "observations": 0}],
  "totals": {"facts": 3532, "observations": 0, "skipped_records": 0},
  "known_evaluated_keys": 0,
  "state_table_available": true
}
```

## 5.2 阶段二：Gather（只读、确定性）

### 5.2.1 聚类方法

作用域内所有成员的向量从 Qdrant 一次取回，归一化后按**贪心种子扩张**分组：

1. 以「该成员与本作用域内所有成员的余弦相似度之和」降序排列，作为处理顺序（中心性优先，确定性）。
2. 取尚未归组的首个成员为种子，收集与种子余弦相似度 ≥ `tau` 的未归组成员作为候选。
3. 候选数 < `min_cluster_size` → 种子单独归组（不成簇）；否则按相似度降序取前 `max_cluster_size` 个成员构成一个簇，整簇标记已归组。
4. 重复至所有成员归组。

### 5.2.2 参数与实测依据

| 参数 | 取值 | 依据（本文档第 6 节实测，作用域 `xue/ying` 3129 条） |
| --- | --- | --- |
| `tau`（余弦相似度阈值） | `0.82` | 0.80 得 132 簇 / 675 成员，0.82 得 96 簇 / 478 成员，0.84 得 75 簇 / 350 成员。0.82 下簇均规模 5.0、簇内主题可辨识（实测样本见 6.2）；更低阈值引入大量异主题合并，更高阈值使簇规模退化到最小值 |
| `min_cluster_size` | `4` | 3 时簇数增至 223（含大量两三条拼凑的小簇），5 时降至 43 且漏掉主题完整的四元素组；4 是本机数据分布的自然下沿 |
| `max_cluster_size` | `15` | 单簇成员越多，单次 LLM 的 prompt 越长且合成越易流于罗列；实测 15 上限下单簇 prompt 峰值 2461 tokens，仍在可控区间 |
| 作用域 | `(user_id, agent_id)` | 本机 3532 条全部带 `agent_id`（4 个取值：ying 3129 / shi 254 / mo 118 / jian 39）。按「只取纯 user_id 记忆」的作用域定义将得到零候选，故本机按二元组划分 |

### 5.2.3 簇指纹与待处理判定

每个成形的簇计算 `observation_key`（定义见 4.2）。

| 规则 | 精确语义 |
| --- | --- |
| G1 已判定短路 | 若该簇的 `observation_key` 在 `dream_cluster_states` 中已有一行（无论 `decision` 为何值），该簇标记 `skipped_reason = "already_evaluated"`，不进入 Consolidate（不消耗 LLM）。这条短路同时覆盖「上一轮已落地观察」与「上一轮判定为提炼不出」两种情形 |
| G2 成员增长 | 成员集合发生变化 → 指纹不同 → 状态表中无该行 → 正常进入 Consolidate（合成新版本，旧版本在 Prune 处置） |
| G3 状态表不可用 | 读取状态表失败时退化为「只按 `memories_2048` 中已落地的 `observation_key` 短路」，本轮成本上升、结果不变 |
| G4 簇上限 | 单轮簇数超过 `max_clusters_per_run`（默认 120）时，按成员数降序取前 N 个处理，其余记入 `deferred`（下一轮继续） |
| G5 无候选 | 作用域内无成形簇时不产生 LLM 调用 |

### 5.2.4 输出

```json
[{"scope": {"user_id": "xue", "agent_id": "ying"},
  "members": ["id1", "id2", "id3", "id4"],
  "observation_key": "9f2c…",
  "skipped_reason": null}]
```

`skipped_reason` 取值：`already_evaluated`（状态表命中）/ `null`（待处理）。被跳过的簇在报告中只计入统计，不产生候选。

## 5.3 阶段三：Consolidate（每簇一次 LLM 调用）

### 5.3.1 提示词

新增 `OBSERVATION_SYNTHESIS_PROMPT`（system）与 `generate_observation_synthesis_prompt(members=…)`（user），与 1a 的 `ADDITIVE_EXTRACTION_PROMPT`、1b 的 `CONTRADICTION_DETECTION_PROMPT` 三者互不共享。

system prompt 全文：

```text
# 角色

你是记忆整合器。你的唯一职责是把一组彼此相关的事实提炼成一条更高层的「观察」（observation）。

你不改写任何一条源事实，不删除任何一条源事实。你只新增一条概括性判断。

# 什么算一条合格的观察

一条观察必须同时满足：

1. **更高层**：它表述的是这组事实共同指向的那个模式、取向或稳定结论，而不是把几条事实重新罗列一遍。
2. **可回溯**：它不引入源事实里没有的信息。任何具体细节（人名、日期、数字、地名）都必须能在源事实里找到出处。
3. **可证伪**：它是一条关于主体的事实性陈述，不是「用户提到了 X」这类关于对话本身的描述。

# 不该产出观察的情况

- 这组事实之间没有共同主题，只是向量相似度凑巧接近 → 返回 {"observation": null}
- 这组事实只是同一件事的多次复述，提炼不出更高层结论 → 返回 {"observation": null}
- 你只能给出「用户讨论过多个话题」这类空洞概括 → 返回 {"observation": null}

# 输入

## Facts

一组相关事实。格式：

[{"id": "事实 uuid", "text": "事实文本", "created_at": "入库时间"}]

# 输出

只返回可由 json.loads() 解析的有效 JSON。不要任何文本、推理、解释或包装。

{
  "observation": {
    "text": "观察文本，一到三句",
    "source_ids": ["支撑这条观察的源事实 uuid"],
    "counterexample": "与这条观察不符的源事实 uuid 列表，没有则空列表"
  }
}

或

{"observation": null}

# 规则

- `source_ids` 只能取自 Facts 列表中真实存在的 id，绝不虚构。
- 观察文本用与源事实相同的语言书写。
- 不给出保留、失效、删除任何记录的建议——处置不属于你的职责。
```

user prompt：`## Facts\n<JSON 数组>\n\n# Output:`，与既有 `generate_contradiction_detection_prompt` 的拼接风格一致。

### 5.3.2 调用契约

| 项 | 值 |
| --- | --- |
| 触发条件 | 簇成员数 ≥ `min_cluster_size` 且未被 G1 已判定短路 |
| 调用粒度 | 一簇一次，串行；不跨簇合并调用 |
| `response_format` | `{"type": "json_object"}` |
| `temperature` | 沿用全局配置（当前 0.2） |
| 输入字段 | 每成员仅传 `id` / `text` / `created_at` 三项，不传向量、不传作用域 |
| 解析失败 | `remove_code_blocks` → `json.loads(strict=False)` → `extract_json` 兜底；仍失败则该簇记 `failed`，继续下一簇 |
| 调用异常 | 该簇记 `failed`，继续下一簇；整轮不中断 |
| 单簇超时 | `per_cluster_timeout_seconds`（默认 60）；超时同异常处理 |

### 5.3.3 输出

```json
{"observation_key": "9f2c…",
 "members": ["id1", "id2", "id3", "id4"],
 "text": "用户长期偏好…",
 "source_ids": ["id1", "id3", "id4"],
 "counterexample": []}
```

`text` 为 `null` 表示该簇提炼不出更高层结论。

## 5.4 阶段四：Prune（唯一改变记忆数据的阶段）

### 5.4.1 剪枝规则（按序执行，不产生写入）

| 规则 | 精确语义 |
| --- | --- |
| P1 空产出 | `text` 为 `null` 或空白 → 丢弃，记 `skipped_reason = "no_higher_order_pattern"` |
| P2 来源合法性 | `source_ids` 中不属于该簇成员的 id 一律剔除；剔除后 `source_ids` 为空 → 丢弃该候选，记 `skipped_reason = "unresolvable_sources"`，log warning |
| P3 证据下限 | 剔除后 `source_ids` 长度 < `min_cluster_size` → 丢弃，记 `skipped_reason = "insufficient_evidence"` |
| P4 文本去重 | 观察文本的 md5 命中既有事实条目的 `hash` 或既有观察条目的 `hash` → 丢弃该候选（同一句话已是记忆，不再新增） |
| P5 计数器同源 | `evidence_count` 一律等于落地时 `source_memory_ids` 的长度，不由 LLM 提供 |
| P6 空候选 | 剪枝后无候选 → 本轮零写入 |

每个进入 Prune 的候选（含被剪枝者）都在 `dream_cluster_states` 中写入或更新一行，`decision` 取该候选的最终判定（`written` 或对应的剪枝原因），`last_evaluated_at` 更新为当前时刻、`evaluations` 加一。这是下一轮 G1 短路的依据——**被剪枝的候选同样必须记入**，否则每轮都会重新付出该簇的 LLM 成本。

**失败的簇不入状态表**：LLM 调用异常、超时或返回不可解析内容的簇记 `failed`，既不写状态表也不写观察，下一轮重新判定。判定失败与判定为「提炼不出」是两回事——后者是有效结论（入表短路），前者不是结论（不入表，下次重试）。

### 5.4.2 落地规则

| 规则 | 精确语义 |
| --- | --- |
| P7 写入内容 | 一次写入 8 个字段：`data`（观察文本）、`hash`、`text_lemmatized`、`memory_kind`、`observation_key`、`source_memory_ids`、`evidence_count`、`dream_run_id`；时间字段与作用域字段同批写入 |
| P8 写入接口 | 与事实条目共用同一写入接口（`vector_store.insert`），使观察条目同样具备稠密向量与 BM25 稀疏向量，可被语义检索与关键词检索命中 |
| P9 标识 | point id 由 `observation_key` 派生（见 4.2）；同一簇重复落地是覆写，条目总数不增 |
| P10 时间字段 | `created_at` = 落地时刻（UTC ISO8601），`valid_at` = 簇成员最早生效时间，`updated_at` = `created_at` |
| P11 演化处置 | 新观察落地后，若存在成员集合 Jaccard 相似度 ≥ `0.6` 且 `observation_key` 不同的既有观察，对旧观察做 payload-only 更新：`invalid_at` = 新观察 `created_at` 的日期部分、`superseded_by` = 新观察 id、`invalid_reason` = `observation_recomputed`。旧观察的文本、hash、时间字段与向量一律不变 |
| P12 单次处置 | 一条旧观察只被处置一次：同一轮内首次命中的新观察生效后即标记为已处置，同轮内后续命中不再覆盖 `superseded_by`；已有 `invalid_at` 的旧观察不再被后续轮次覆盖，其失效标记即为最终值 |
| P13 悬空引用 | `source_memory_ids` 指向的源事实日后被删除时，该字段原样保留、不清洗、不报错；读侧不做跳转解析（与 bi-temporal R8 同构） |
| P14 源事实零写 | 整轮整合对事实条目（无 `memory_kind` 字段的记录）不产生任何 Qdrant 写操作、不写 history 行 |
| P15 审计痕迹 | 运行统计落 `dream_runs` 表；判定结果落 `dream_cluster_states` 表；报告落 `history/dream-reports/<run_id>.json`；观察条目的 payload 字段本身构成可查询的审计痕迹 |

### 5.4.3 新增 `invalid_reason` 取值

沿用 bi-temporal 的 `invalid_reason` 字段，新增一个取值：

| 取值 | 语义 | 写入方 |
| --- | --- | --- |
| `superseded_by_newer_fact` | 既有：被更新的同类事实取代 | bi-temporal 1c（不变） |
| `observation_recomputed` | 观察的成员集合发生变化，该版本已被新版本取代 | Dream P11（新增） |

该字段当前的消费者是读侧序列化（作为字符串原样输出），新增取值不改变任何既有读路径的判定。

## 5.5 读路径的观察可见性

| 路径 | 默认行为 | 显式行为 |
| --- | --- | --- |
| `POST /search`（SDK `search`） | **排除**观察条目 | `include_observations=true` 时纳入，观察与事实同权参与同一套相似度打分 |
| `GET /memories`（管理列表）、`GET /memories/export` | 返回全量（含观察），观察由 `memory_kind` 字段标识 | 无新增参数；导出不丢数据 |
| `GET /memories/{id}` | 一致返回（事实与观察同口径） | 无 |
| `GET /memories/{id}/observations`、`GET /memories/{id}/sources` | 独立查询，不受上述谓词影响 | 无 |

`filters` 与 `include_observations` 的关系：`include_observations=false`（默认）时，检索先加「排除观察」谓词，`filters` 中若再写 `memory_kind` 条件则与它求交（写 `{"memory_kind": "observation"}` 得到空集，写 `NOT` 无额外效果）；`include_observations=true` 时不加排除谓词，`filters` 中的 `memory_kind` 条件按既有 filter DSL 正常生效（`{"memory_kind": "observation"}` 得到只含观察的结果）。

**范围限定在检索路径**：默认排除只加在 `/search`（记忆注入路径），与 memory-decay 特性的作用范围一致；管理面的列表与导出保持全量语义，避免出现「导出的数据集比库存少」的口径不一致。

排序：打分函数零改动——观察不是补充信号、不加权、不降权；`include_observations=true` 只是扩大候选集，不改变任何一条候选的分数计算方式。

默认不返回的判据：既有事实条目没有 `memory_kind` 字段，Qdrant 的 `MatchValue` 条件不匹配缺失字段，取反后全部放行——因此该过滤对存量检索结果零影响（实测见 6.4）。

## 5.6 触发与调度

| 项 | 语义 |
| --- | --- |
| 载体 | `server` 进程内的后台线程，由 FastAPI 的应用生命周期钩子启动与停止；不新增容器、不新增外部调度服务 |
| 周期 | `dream_interval_seconds`（默认 86400，即每日一轮） |
| 首轮延迟 | 进程启动后延迟 `dream_initial_delay_seconds`（默认 1800），避开启动期与启动后的写入高峰 |
| 手动触发 | `POST /dream/run`（`mode=dry_run` 或 `live`），走同一套流程与同一把锁 |
| 并发互斥 | 独占 lock-file `history/dream.lock`，以 `fcntl.flock(LOCK_EX \| LOCK_NB)` 获取；未获取到时本轮状态记 `skipped`，零写入，立即返回 |
| lock 释放 | 线程退出即释放（进程退出时内核自动释放）；lock 文件本身保留在卷上，不删除 |
| 单轮超时 | `dream_run_timeout_seconds`（默认 1800）。超时后中断 Consolidate，已落地的观察保留，未处理的簇留待下一轮 |
| 失败重试 | 单簇失败不重试；整轮失败（Orient/Gather/Prune 抛异常）由下一周期自然重试 |
| 关停 | 进程停止信号触发事件，线程在单簇边界处退出 |
| 降级 | `dream_enabled`（默认 `false`）是总开关：关闭时周期线程不启动，且 `POST /dream/run` 与 `POST /dream/preview` 均返回 409。首次上线需显式开启——整合会写入新数据，不在默认开启的路径上自动发生 |

调度参数一律经既有配置链路（`.env` → `DEFAULT_CONFIG` → 可运行时覆盖），与 `server_state` 的配置合并机制一致。

## 5.7 dry-run

| 项 | 语义 |
| --- | --- |
| 执行范围 | 完整执行 Orient → Gather → Consolidate → Prune（Prune 只产出判定结果，不落地） |
| 数据写入 | 零：不写 Qdrant、不写 history、不写 `dream_runs`、不写 `dream_cluster_states`、不写任何缓存文件；报告落盘到 `history/dream-reports/` 是 dry-run 唯一的持久化产出。dry-run 的判定结果**不进入**状态表，因此 dry-run 之后紧接的实跑仍会完整执行该轮 |
| LLM 调用 | 与实跑**完全一致**（成本口径可比）；报告中的 token 与调用数照实记录 |
| 锁 | 与实跑共用同一把锁（dry-run 也持有），避免与实跑交错 |
| 总开关 | 受 `dream_enabled` 约束：关闭时返回 409（与 `POST /dream/run` 一致） |
| 报告内容 | 见 5.8 |
| 与实跑的差异 | 仅有两点：不执行 5.4.2 的全部写入动作；`dream_runs` 不落行。判定规则、剪枝规则、LLM 调用、统计口径完全相同 |
| 报告与实跑的一致性 | 报告中的 `would_write` 列表与实跑实际写入的 point id 集合必须逐条相等（同一输入下） |

## 5.8 dry-run 报告格式

落盘路径 `/app/history/dream-reports/<run_id>.json`，同时作为 `POST /dream/preview` 的响应体返回。

```json
{
  "run_id": "0f3a…",
  "mode": "dry_run",
  "started_at": "2026-09-17T06:00:00+00:00",
  "finished_at": "2026-09-17T06:04:45+00:00",
  "duration_seconds": 285.0,
  "scopes": [
    {"user_id": "xue", "agent_id": "ying", "facts": 3129, "observations": 0,
     "candidates": 96, "skipped_clusters": 0}
  ],
  "totals": {
    "facts": 3532, "observations": 0, "clusters": 96,
    "llm_calls": 96, "failed_clusters": 0,
    "prompt_tokens": 111516, "completion_tokens": 38633
  },
  "candidates": [
    {
      "observation_key": "9f2c…",
      "members": ["id1", "id2", "id3", "id4"],
      "text": "用户长期偏好单一来源规则……",
      "source_memory_ids": ["id1", "id3", "id4"],
      "evidence_count": 3,
      "counterexample": [],
      "decision": "would_write",
      "skip_reason": null,
      "would_write": {"point_id": "…", "memory_kind": "observation"}
    },
    {
      "observation_key": "ab41…",
      "members": ["id5", "id6", "id7", "id8"],
      "text": null,
      "source_memory_ids": [],
      "evidence_count": 0,
      "decision": "skipped",
      "skip_reason": "no_higher_order_pattern"
    }
  ],
  "supersede_preview": [
    {"observation_key": "旧key…", "superseded_by": "新point_id", "invalid_reason": "observation_recomputed"}
  ],
  "errors": []
}
```

`decision` 取值：`would_write` / `skipped`。`skip_reason` 取值：`no_higher_order_pattern` / `unresolvable_sources` / `insufficient_evidence` / `duplicate_text` / `already_evaluated`。同一 `observation_key` 的 `decision`/`skip_reason` 与 Prune 写入 `dream_cluster_states` 的判定一致。

## 5.9 HTTP 接口

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| `POST` | `/dream/preview` | 执行一轮 dry-run，返回报告（同落盘） |
| `POST` | `/dream/run` | 执行一轮实跑（`mode=live`）；未启用或锁未获取时返回 409 |
| `GET` | `/dream/runs` | 运行记录列表（分页，按 `started_at` 倒序） |
| `GET` | `/dream/runs/{run_id}` | 单轮统计与报告路径 |
| `GET` | `/memories/{memory_id}/observations` | 反向追溯：返回 `source_memory_ids` 含该 id 的全部观察（含已失效版本） |
| `GET` | `/memories/{observation_id}/sources` | 正向追溯：返回该观察的全部源事实条目 |

后两个端点复用既有 `/memories/{id}` 的序列化口径，观察的四个字段与 bi-temporal 四个字段都在顶层。

## 5.10 检索参数

`SearchRequest` 新增 `include_observations: Optional[bool]`（默认 `false`），透传至 SDK `search` / `get_all`。SDK 侧新增同名参数，默认 `False`。

---

# 6. 实测依据

以下数据全部在目标仓库当前状态（Docker 四服务运行中）真实执行取得，命令与原始输出见附录 A。

## 6.1 存量口径

| 项 | 实测值 |
| --- | --- |
| `memories_2048` 总数 | 3532（2026-09-17 05:57 UTC 首次 count）／3545（06:01 UTC 复核）—— 差额来自并行运行的其它 agent 的正常写入，非本方案动作 |
| `user_id` 分布 | `xue` 全覆盖（唯一） |
| `agent_id` 分布（3545 快照） | `ying` 3129 / `shi` 259 / `mo` 118 / `jian` 39（合计 3545） |
| 文本长度（字符） | 中位 155，P75 225，P90 305，P99 470，最大 826，均 174.8 |
| 全部记录具备 `created_at` | 是（缺失计数 0） |
| 全部记录具备 `memory_kind` | 否（0 条） |

## 6.2 聚类参数实测

作用域 `xue/ying`（3129 条），贪心种子扩张，`min_cluster_size=4`、`max_cluster_size=15`：

| `tau` | 簇数 | 覆盖成员 | 簇均规模 | 最大簇 | P50 | P90 |
| --- | --- | --- | --- | --- | --- | --- |
| 0.80 | 132 | 675 | 5.11 | 15 | 5 | 7 |
| **0.82** | **96** | **478** | **4.98** | **14** | **4** | **6** |
| 0.84 | 75 | 350 | 4.67 | 9 | 4 | 6 |
| 0.86 | 42 | 192 | 4.57 | 8 | 4 | 6 |

`tau=0.82` 下的簇样本（`xue/ying`，最大簇 14 成员）：成员全部围绕同一个主题（`hermes update` 的 release-pin 方案演进），簇内可辨识出重复表述、方案细化、决策变更三类内容——正是需要被精炼为一条观察的形态。

`tau=0.80` 下出现跨主题合并（把不同项目的同类表述并簇），故取 0.82。

## 6.3 LLM 成本实测

对 `xue/ying` 的 96 个簇（`tau=0.82`）逐簇真实调用 `deepseek-v4.1-flash`：

| 项 | 实测值 |
| --- | --- |
| 簇数 | 96 |
| 成功返回 | 96（失败 0） |
| 产出观察 | 25 |
| 返回 `null` | 71 |
| 整轮时长 | 284.9 s |
| prompt tokens 合计 | 111,516 |
| completion tokens 合计 | 38,633（其中 reasoning 33,717） |
| 每簇均值 | prompt 1,162 / completion 402 / 时长 2.97 s |
| 单簇耗时区间 | 0.49 s – 14.82 s |
| 单簇 prompt 区间 | 905 – 2,461（与簇规模正相关） |

观察产出率 26%（25/96）——即约四分之三的相似簇经 LLM 判定后不构成更高层结论。这个比例决定了实际写入量远小于簇数。

embedding 调用（观察文本，2048 维，阿里云 MaaS）：

| 项 | 实测值 |
| --- | --- |
| 单条 | 0.57 s |
| 20 条批 | 1.04 s |
| 单次请求上限 | 20 条（21 条返回 400 `batch size is invalid, it should not be larger than 20`） |

## 6.4 检索与索引实测

| 验证项 | 结果 |
| --- | --- |
| `{"NOT": [{"memory_kind": "observation"}]}` 过滤 | 3 条探针记录中返回 2 条（键缺失的 1 条被放行，`memory_kind="fact"` 的 1 条放行，`=observation` 的 1 条被排除） |
| 全量默认读谓词对无 `memory_kind` 记录 | `total = 3545`、`must_not observation = 3545`、`must observation = 0`、`3545 + 0 = 3545`（三者自洽，证明该谓词对存量记录全部放行） |
| keyword 数组字段过滤 `source_memory_ids` | 值为 `["s1","s2","s3"]` 与 `["s3","s4"]` 的两条记录，按 `s3` 查询返回两条 |
| `observation_key` keyword 精确命中 | 返回唯一 1 条 |
| 3 个新 keyword 索引建立在 `payload_schema` 中可见 | 是 |
| `/search` 端到端传入 `NOT [{"memory_kind":"observation"}]` | 生效（返回事实条目，观察被排除；`AND` 形式只返回观察） |
| `/search` 端到端返回观察条目的字段 | 顶层含 4 个 bi-temporal 字段；`memory_kind` / `observation_key` / `source_memory_ids` / `evidence_count` 当前落在 `metadata`（原因见下） |

**已知缺口**：`memory_kind` 等观察字段当前会落入 `/search` 与 `GET /memories/{id}` 响应的 `metadata`，因为 `server/main.py` 的 `_RESERVED_PAYLOAD_KEYS` 未包含它们。本方案第 5.5 节要求它们成为一等字段，故 `_RESERVED_PAYLOAD_KEYS`、`_serialize_memory`、`EXPORT_CSV_COLUMNS` 三处需与 bi-temporal 四字段同等对待。

## 6.5 幂等与规模实测

| 验证项 | 结果 |
| --- | --- |
| 同一 point id 重复写入 | 集合计数 3545 → 3546 → 3546（第二次为覆写，不新增） |
| `uuid5(NAMESPACE_URL, key)` 稳定性 | 同 key 两次派生结果相同（`d3fd67d5-…`） |
| 不同 id + 同 `observation_key` | 集合中出现 2 条（证明幂等**不能**依赖 key 过滤，必须依赖确定性 point id） |
| 全量向量取回 | 3540 × 2048 float32 取回 1.1 s，矩阵占用约 29 MB |
| 全量聚类计算 | 四个作用域合计 3.0 s（含贪心扩张） |
| lock-file 跨进程互斥（容器内 `/app/history`） | A 获取后 B 阻塞（errno 11 EAGAIN）；A 释放后 B 获取 |
| lock-file 同进程异 fd 互斥 | 阻塞（errno 11）——同进程重复触发同样被挡 |
| `/app/history` 可写 | 挂载为独立 ext4 卷（61 G，已用 17 G）；`/app` 本身为只读 |
| 容器内 numpy | 2.5.2（既有依赖，无需新增） |
| 调度库 | 容器内无 `apscheduler` / `schedule` / `filelock`（本方案不引入） |

## 6.6 成本估算

### 6.6.1 单轮调用数与 token

| 场景 | LLM 调用数 | prompt tokens | completion tokens | 单轮时长 | 数据来源 |
| --- | --- | --- | --- | --- | --- |
| 首次全量（3540 条，96 簇） | 96 | 111,516 | 38,633 | 285 s | **实测** |
| 稳态日增（约 200 条/日，状态表生效） | 5 – 20 | 6,000 – 25,000 | 2,000 – 9,000 | 15 – 60 s | 推算（见下） |
| 稳态日增（状态表失效的退化情形） | 96 | 111,516 | 38,633 | 285 s | 同上限口径 |
| 稳态日增 + 成员增长重算（上限） | ≤ 96 | ≤ 111,516 | ≤ 38,633 | ≤ 285 s | 上限口径 |

状态表带来的成本量级差异是 96 → 5–20（约 5–20 倍），这是 4.3.2 存在的直接理由：实测 96 簇中只有 25 簇产出观察，若只以落地观察为水位，余下 71 簇每轮都会被重新调用。

稳态推算依据：

- 近 18 天写入 3532 条，日均 196 条（实测：08-24 至 09-17 逐日计数，见附录 A）。
- 每 36.9 条产生 1 个簇（3532 / 96），日增 196 条 → 约 5.3 个新簇。
- 成员增长导致指纹变化会额外触发重算；按「新簇 + 20% 同簇演化」估 5 – 20 次/日。
- 每簇均值取实测值（prompt 1,162 / completion 402 / 2.97 s）。

### 6.6.2 与写入路径的成本对比

`add()` 的 LLM 成本不受本方案影响：当前每次写入为 2 次 LLM 调用（1a 提取 + 1b 矛盾检测），本方案不新增第 3 次。整合的成本发生在独立线程（每日一轮），与用户请求的时延无关。

### 6.6.3 资源占用

| 项 | 峰值 |
| --- | --- |
| 向量矩阵内存 | 3540 × 2048 float32 ≈ 29 MB（全量取回；分块计算相似度不展开 N×N 矩阵） |
| 聚类 CPU | 3 s（全量，单线程 numpy） |
| 新增容器 | 0 |
| 新增外部服务 | 0 |
| 容器内存水位 | 当前 mem0 259.8 MiB / 8 GiB、qdrant 309.4 MiB / 8 GiB（实测 `docker stats`），整合线程增量在 100 MiB 量级内 |

---

# 7. 文件改动点与新增文件清单

## 7.1 新增文件

| 路径 | 内容 |
| --- | --- |
| `mem0/memory/dream.py` | 四阶段实现：Orient 汇总、Gather 聚类（纯函数）、Consolidate 调用编排、Prune 剪枝与落地；全部判定规则为模块级纯函数 |
| `server/dream_scheduler.py` | 后台线程生命周期、周期计时、lock-file 获取/释放、超时与关停 |
| `server/routers/dream.py` | 5.9 节的六个端点 |
| `server/alembic/versions/007_create_dream_tables.py` | `dream_runs` 与 `dream_cluster_states` 两张表迁移 |
| `tests/memory/test_dream.py` | 四阶段单测（聚类确定性、剪枝规则、幂等、演化处置、失败隔离） |
| `tests/test_dream_router.py` | 端点测试（dry-run 零写入、锁 409、反向追溯） |

## 7.2 改动文件

| 文件 | 改动点 |
| --- | --- |
| `mem0/configs/prompts.py` | 新增 `OBSERVATION_SYNTHESIS_PROMPT` 与 `generate_observation_synthesis_prompt`；新增参数常量 `DREAM_TAU` / `DREAM_MIN_CLUSTER_SIZE` / `DREAM_MAX_CLUSTER_SIZE` / `INVALID_REASON_OBSERVATION_RECOMPUTED` |
| `mem0/memory/main.py` | `_add_to_vector_store`（sync + async）的 Phase 1 候选池过滤追加「排除 `memory_kind == "observation"`」；`BI_TEMPORAL_PAYLOAD_KEYS` 追加 4 个观察字段；`search` / `get_all`（sync + async）新增 `include_observations` 参数与对应谓词 |
| `mem0/vector_stores/qdrant.py` | `_create_filter_indexes` 的 keyword 字段清单追加 `memory_kind` / `observation_key` / `source_memory_ids` |
| `server/main.py` | 应用生命周期钩子挂载/停止调度线程；`_RESERVED_PAYLOAD_KEYS` 追加 4 个观察字段；`_serialize_memory` 输出这 4 个字段；`SearchRequest` 新增 `include_observations`；`EXPORT_CSV_COLUMNS` 追加 4 列；`include_router(dream_router)` |
| `server/models.py` | 新增 `DreamRun` 模型 |
| `server/docker-compose.yaml` | mem0 服务环境变量追加 `DREAM_ENABLED` / `DREAM_INTERVAL_SECONDS` / `DREAM_INITIAL_DELAY_SECONDS` / `DREAM_RUN_TIMEOUT_SECONDS` / `DREAM_REPORT_DIR` |
| `server/.env.example` | 同上变量的占位说明 |

## 7.3 与上游 mem0 的冲突面

| 文件 | 本地既有改动（相对 upstream） | 本方案新增改动 | 冲突风险 |
| --- | --- | --- | --- |
| `mem0/configs/prompts.py` | +306 / -331（中文化 + ADD-only 改造） | 新增一段提示词与构造函数（纯追加） | 中。追加内容不与上游同名符号冲突 |
| `mem0/memory/main.py` | +6 / -5 | Phase 1 一处过滤、`BI_TEMPORAL_PAYLOAD_KEYS` 一处追加、`search`/`get_all` 参数 | 中。均为小改，但文件是上游 V3 管道核心 |
| `mem0/vector_stores/qdrant.py` | +28 / -2 | keyword 字段清单追加 3 项 | 低 |
| `server/main.py` | +128 / -16 | 生命周期钩子、序列化、参数、路由 | 中 |
| 其余 | 无 | 新增文件 | 低 |

`mem0/memory/dream.py` 与 `server/dream_scheduler.py`、`server/routers/dream.py` 是全新文件，不与上游冲突。

---

# 8. 存量记录兼容性结论

**结论：存量记录不需要回填，也不需要迁移。**

依据（均为本机实测）：

1. **事实条目不写 `memory_kind`，默认读谓词对缺失字段天然放行**。默认读为 `NOT[{"memory_kind": {"eq": "observation"}}]`；Qdrant 的 `MatchValue` 条件不匹配字段缺失的记录，取反后缺失字段的记录被保留。实测：对 `memories_2048` 执行该谓词，count = 3545，与集合总数相等。
2. **观察字段对既有读路径是未知键，不影响序列化安全**。`_serialize_memory` 对未知 payload 键的既有行为是放入 `metadata`；本方案把它们提升为一等字段后，既有记录的这些键仍为缺失，输出为 `null`，与 bi-temporal 四字段的初始形态一致。
3. **既有记录不参与任何整合写操作**。Prune 的写入对象只有两类：新的观察条目（新增 point id）、被取代的旧观察条目（payload-only 更新）。事实条目的判定路径（P14）不产出任何写操作。

后续行为：新写入的事实仍按 bi-temporal 管道落库（不带 `memory_kind`）；整合产出的观察带 `memory_kind = "observation"`。两条路径共用同一集合、同一套索引与同一套读谓词。

---

# 9. 职责边界

| 邻近特性 | 归属 | 本方案的处置 |
| --- | --- | --- |
| 事实失效（新旧事实矛盾） | bi-temporal 1b / 1c | 不涉及。整合不判定事实矛盾、不写事实的 `invalid_at`。整合产出的观察不进入 1b 的候选池（Phase 1 已排除），因此写入路径不会对观察做失效处置 |
| 检索期衰减（Ebbinghaus 衰减 + 访问强化） | memory-decay 特性 | 不涉及。整合不读写 `last_accessed` / `access_count`，不改打分函数、不引入时间权重、不调整 `top_k` 或阈值。整合对检索的影响仅是候选集上一个可开关的过滤条件 |
| 图记忆（Graphiti 旁路图检索） | graph-memory 特性 | 不涉及。整合不引入图结构、不新增图库服务、不做图遍历；簇的成员关系只在整合内部以数组形式存在，不落成可遍历的图 |
| 记忆删除与合并 | 既有 `DELETE` 路径 | 不涉及。整合不删除任何记录；观察的演化以失效标记表达，不是删除 |
| 观察条目的写入通道 | 本方案 | 唯一。任何其他路径（用户 `add`、管理面 `PUT`）产生的记录都不带 `memory_kind`，因此不会与观察的幂等标识混淆 |

不重复的判据：三个特性的写入字段集合两两不相交——bi-temporal 写 `valid_at` / `invalid_at` / `superseded_by` / `invalid_reason`（限于事实），decay 写 `last_accessed` / `access_count`，本方案写 `memory_kind` / `observation_key` / `source_memory_ids` / `evidence_count` / `dream_run_id`。唯一的字段重叠是 `invalid_at` / `superseded_by` / `invalid_reason`：本方案对**观察条目**使用（限定条件 `memory_kind == "observation"`），bi-temporal 对**事实条目**使用（限定条件 `memory_kind` 缺失），两者的作用对象由 `memory_kind` 判定不相交。

---

# 10. 验收标准

判定一律以**实现后的实际执行输出**为准，与具体 commit 对照，不认「工作区当下状态」。全部端到端验证使用隔离作用域（`user_id` 前缀 `test_dream_*`）。

## 10.1 四阶段定义与判定规则

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-1] | Orient 是只读阶段：单独执行 Orient 前后，`memories_2048` 的 count 与全部 payload 的 sha256 摘要不变 | 前后各取一次 `points/count` 与全量 payload 摘要并 diff |
| [AC-2] | Gather 是确定性纯函数：同一成员集合、同一参数连续执行两次，返回的簇列表（每簇成员 id 升序）逐簇相等 | 单测：同输入调用两次，`json.dumps(sort_keys=True)` 相等 |
| [AC-3] | Gather 不调用 LLM：函数签名不含 LLM/client 参数，且执行期间 LLM 调用计数为 0 | 单测：注入计数桩，断言为 0 |
| [AC-4] | Consolidate 每簇恰好一次 LLM 调用：调用数等于待处理簇数；system 消息内容等于 `OBSERVATION_SYNTHESIS_PROMPT`，且与 1a、1b 的 system 内容两两不同 | 单测：mock LLM 记录调用序列并断言 |
| [AC-5] | Consolidate 输入只含成员的 `id` / `text` / `created_at` 三项：注入了向量或作用域字段的入参构造被断言失败 | 单测：断言 user prompt 的 JSON 中每条事实的键集合恰为三项 |
| [AC-6] | Prune 的 P2 幻觉防线：`source_ids` 中不属于该簇成员的 id 被剔除；剔除后为空时该候选被判 `unresolvable_sources` 且无写入 | 单测：构造含非法 id 的候选，断言判定结果与零写入 |
| [AC-7] | Prune 的 P3 证据下限：剔除后 `source_ids` 长度 < `min_cluster_size` 时判 `insufficient_evidence` | 单测 |
| [AC-8] | Prune 的 P1：`text` 为 `null` 时判 `no_higher_order_pattern` 且无写入 | 单测 |

## 10.2 证据链与双向追溯

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-9] | 正向追溯：任一观察条目的 `GET /memories/{obs_id}` 响应顶层含 `source_memory_ids` 与 `evidence_count`，两者长度一致，且列表中每个 id 都能被 `GET /memories/{id}` 取到 | 实跑后的观察条目逐条执行 |
| [AC-10] | 反向追溯：`GET /memories/{memory_id}/observations` 返回的观察集合，等于「`source_memory_ids` 含该 id」的全集（用 Qdrant keyword 数组过滤独立复算并比对） | 实跑后抽样 ≥5 条源事实执行 |
| [AC-11] | 观察字段是一等字段：`GET /memories/{obs_id}` 与 `POST /search?include_observations=true` 结果的顶层含 `memory_kind` / `observation_key` / `source_memory_ids` / `evidence_count` / `dream_run_id`，且 `metadata` 不含其中任何一个 | 读取响应 JSON，检查键集合 |
| [AC-12] | `evidence_count` 与源列表同源：逐条断言 `evidence_count == len(source_memory_ids)` | 实跑后全量断言，不允许例外 |

## 10.3 幂等与断点续跑

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-13] | 幂等（同输入重复执行）：在状态表为空、观察集合为空的初始条件下对同一输入连跑两轮实跑，两轮落地的 point id 集合完全相同；第二轮相对第一轮的 `memories_2048` count 增量为 0（覆写而非追加），`observation_key` 集合不变 | 隔离作用域内连跑两轮实跑，前后各取 count 与 key 集合 |
| [AC-14] | point id 确定性：同一 `observation_key` 两次派生的 point id 相等 | 单测：`uuid5(NAMESPACE_URL, key)` 两次调用相等 |
| [AC-15] | 断点续跑：人为中断第一轮（处理完 N 个簇后终止进程），重跑第二轮的 LLM 调用数 = 剩余待处理簇数（不含已产出 key 的簇） | 记录两轮 `llm_calls`，断言 `first + second == total_without_interrupt` |
| [AC-16] | 已判定短路不消耗成本：对同一输入连跑两轮，第二轮 `llm_calls` = 0，且 `dream_cluster_states` 中全部簇的 `decision` 与首轮一致、`evaluations` 各自加一；首轮返回 `no_higher_order_pattern` 的簇在状态表中也有一行（被剪枝的簇同样入表） | 实跑统计与状态表查询 |

## 10.4 并发与失败隔离

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-17] | lock 生效（跨进程）：两个实例先后启动，只有先到的执行；后到的 `dream_runs.status = "skipped"` 且零写入（Qdrant count 与 payload 摘要不变） | 并发启动两个进程，比对记录与 count |
| [AC-18] | lock 生效（同进程）：同一进程内重复触发兼容路径同样被挡 | 单测：同进程两次获取，第二次失败 |
| [AC-19] | 失败隔离：mock LLM 对第 k 簇抛异常，该簇记 `failed`，其余簇照常产出，整轮 `status = "completed"`，`failed_clusters = 1`；且该簇在 `dream_cluster_states` 中**无行**（下轮重试），而其余簇均有行 | 单测 + 状态表查询 |
| [AC-20] | 解析失败隔离：mock LLM 对某簇返回不可解析内容，该簇记 `failed`，不产生写入，其余簇不受影响 | 单测 |
| [AC-21] | 单轮超时：mock 单簇耗时超过 `per_cluster_timeout_seconds` 时该簇记 `failed`；整轮超过 `dream_run_timeout_seconds` 时 `status = "timeout"`，已落地观察保留 | 单测：注入超时 |

## 10.5 源事实零写

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-22] | 实跑前后，全部事实条目（`memory_kind` 缺失的记录）逐条 `(id, data, hash, created_at, updated_at, valid_at, invalid_at, superseded_by, invalid_reason)` 的 sha256 摘要不变 | 前后各取一次全量摘要并 diff |
| [AC-23] | 实跑不产生事实条目的新增：`count(must_not memory_kind=observation)` 前后相等 | 前后各取一次 |
| [AC-24] | 实跑不为事实写 history 行：`history.db` 的 `history` 表行数增量为 0 | `sqlite3 history.db "select count(*) from history"` 前后比对 |

## 10.6 dry-run 行为边界

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-25] | dry-run 零数据写入：前后 `memories_2048` 的 count 相等；`count(memory_kind=observation)` 前后均为 0；`dream_runs` 表行数增量为 0；`dream_cluster_states` 表行数增量为 0；`history` 表行数增量为 0 | 五项前后比对 |
| [AC-26] | dry-run 产出可读报告：报告为合法 JSON，含 `candidates[]`，每项含 `observation_key` / `members` / `text` / `source_memory_ids` / `evidence_count` / `decision` / `skip_reason` | 解析报告并断言字段 |
| [AC-27] | dry-run 与实跑判定一致：在状态表为空、观察集合为空的初始条件下，先跑 dry-run 再跑实跑，dry-run 报告中 `decision == "would_write"` 的 `observation_key` 集合与实跑实际落地的 `observation_key` 集合逐条相等 | 先 dry-run 再实跑并 diff |
| [AC-28] | dry-run 与实跑成本口径一致：同一输入下两者 `llm_calls` 相等 | 比对报告与 run 记录 |
| [AC-29] | dry-run 报告落盘：`docker exec mem0-dev-mem0-1 cat /app/history/dream-reports/<run_id>.json` 可读出文件，且其 JSON 内容与 `POST /dream/preview` 响应体逐字段相等 | `docker exec` 读文件并与响应体 diff |

## 10.7 检索与排序边界

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-30] | 默认检索不含观察：在隔离作用域内构造固定事实集（≥20 条）与 ≥3 条观察后，`POST /search`（默认参数，≥5 条 query，`top_k=20`）返回的 id 列表与顺序，与「构造同样的事实集但不构造观察」时逐条一致 | 两种构造各跑一次并 diff（同一作用域、同一 query） |
| [AC-31] | 显式纳入生效：`include_observations=true` 的结果集合 = 默认结果集合 ∪ 观察命中集合 | 两次查询结果做集合运算比对 |
| [AC-32] | 打分零改动：同一 query 在 `include_observations=false` 时的每条 `score` 与「无观察」构造下的逐条相等（浮点精确相等） | 两种构造各跑一次并逐条比对 |
| [AC-33] | 排序函数无观察分支：`score_and_rank` 的入参构造与实现中不出现 `memory_kind` / `observation` 相关符号 | 搜索源码断言 |
| [AC-34] | 不与 Decay 争字段：整合代码路径不出现 `last_accessed` / `access_count` 的写操作 | 搜索源码断言 |
| [AC-35] | 不引入图依赖：`server/requirements.txt` 与 `pyproject.toml` 的新增依赖数均为 0；容器内不出现新的图库服务 | 依赖文件前后 diff 与 `docker ps` 对比 |

## 10.8 索引、存量与降级

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-36] | 三个新索引已建立：`GET /collections/memories_2048` 的 `payload_schema` 含 `memory_kind` / `observation_key` / `source_memory_ids`，且均为 `keyword` | 直接查 Qdrant REST |
| [AC-37] | 存量无感：`count(must_not memory_kind=observation)` 等于集合总数；`count(memory_kind=observation)` 在整合前为 0 | 直接查 Qdrant |
| [AC-38] | 降级可用：`dream_enabled=false` 时，`POST /dream/run` 与 `POST /dream/preview` 均返回 409，`dream_runs` 表无新增行，且主链路 `add` / `search` 的端到端行为与整合前一致 | 关开关后执行端到端与表查询 |
| [AC-39] | 观察条目可被两种检索命中：对某条观察的原文做关键词查询，BM25 路径能返回该条目（证明稀疏向量已写入） | 隔离作用域内构造查询 |
| [AC-40] | 观察条目的 point 计数与写入数一致：`count(memory_kind=observation)` 增量等于 `observations_written` 减去覆写数 | 实跑前后比对 |

## 10.9 验证纪律

| 编号 | 验收内容 |
| --- | --- |
| [AC-41] | 全部端到端验证使用隔离作用域（`test_dream_*` 前缀）；验证完毕后删除隔离记录，`memories_2048` 的 count 回到基线值 |
| [AC-42] | 存量 3500+ 条真实记忆全程无删除、无改写、无失效操作 |
| [AC-43] | `make lint`（ruff，line length 120）对 `mem0/`、`server/` 改动零告警 |
| [AC-44] | 实现完成后 `bash patches/generate-patch.sh` 成功刷新 `patches/mem0-local.patch`，且该 patch 覆盖本次全部改动文件 |
| [AC-45] | 成本报告实测：`POST /dream/preview` 返回的 `llm_calls` / `prompt_tokens` / `completion_tokens` / `duration_seconds` 四项均为非零实测值，且与该轮真实 LLM 调用计数一致 |

## 10.10 方案文档自述

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-46] | 正文只描述「是什么 / 做什么」：不出现实现步骤、命令序列、文件操作顺序 | 通读断言 |
| [AC-47] | 无占位符：正文不含 `TBD` / `TODO` / `…待定` | 文本搜索 |
| [AC-48] | 参数取值均有依据：5.2.2 的每个参数都能在第 6 节找到对应实测数据或明确的取舍理由 | 逐参数核对 |

## 10.11 实现卡拆分建议

互不依赖、可并行的三组：

| 组 | 内容 | 依赖 |
| --- | --- | --- |
| A 内核 | `mem0/memory/dream.py` 四阶段、提示词、索引字段、`BI_TEMPORAL_PAYLOAD_KEYS` 追加、读取参数 | 无（本方案即其输入） |
| B 服务面 | `server/dream_scheduler.py`、`server/routers/dream.py`、`dream_runs` + `dream_cluster_states` 迁移、生命周期钩子、序列化与导出列 | 依赖 A 暴露的阶段函数签名；签名在本方案第 5 节已固定，故 B 可与 A 并行开发、在合并时对接 |
| C 写入路径接缝 | `_add_to_vector_store` 的 Phase 1 过滤（sync + async） | 无（独立于 A/B，改动量最小） |

合并顺序建议：A → C → B（B 的端到端验证需要 A 与 C 就位）。三组触碰的文件无重叠（A：`mem0/memory/dream.py` + `mem0/configs/prompts.py` + `mem0/vector_stores/qdrant.py` + `mem0/memory/main.py` 的读路径；C：`mem0/memory/main.py` 的写路径；B：`server/`）。

A 与 C 同时改 `mem0/memory/main.py`，但落点不同（A 改 `search`/`get_all` 与常量声明，C 改 `_add_to_vector_store`），可并行；若由同一执行者串行完成则顺序为 C → A。

---

# 11. 附录

## 附录 A：实测命令与原始输出

以下为第 6 节数据的执行证据（均在目标仓库当前状态、Docker 四服务运行中执行）。

### A.1 存量口径（6.1）

```bash
curl -s localhost:6333/collections/memories_2048/points/count -H 'Content-Type: application/json' -d '{"exact":true}'
# {"result":{"count":3532},"status":"ok","time":0.000823245}
```

全量 scroll 统计 `user_id` / `agent_id` / 文本长度（同一时段的两个快照，差额为并行 agent 的正常写入）：

```text
total 3532
by user: [('xue', 3532)]
combos: [(('xue', 'ying'), 3129), (('xue', 'shi'), 246), (('xue', 'mo'), 118), (('xue', 'jian'), 39)]
n 3540 min 9 p25 105 median 155 p75 225 p90 305 p99 470 max 826 mean 174.8
```

清理探针后的干净复核（2026-09-17 06:01:52 UTC，无任何探针记录）：

```text
total              = 3545
must_not observation = 3545 (== total: True )
must observation     = 0
sum(no_obs, only_obs) = 3545
by agent           = {'ying': 3129, 'shi': 259, 'mo': 118, 'jian': 39} sum = 3545
```

逐日写入分布：

```text
2026-08-24 373 / 08-25 158 / 08-26 289 / 08-27 291 / 08-28 288 / 08-31 524
2026-09-01 215 / 09-02 176 / 09-03 183 / 09-04 133 / 09-07 198 / 09-08 99
2026-09-09 96 / 09-10 232 / 09-11 58 / 09-14 56 / 09-16 76 / 09-17 87
```

### A.2 聚类参数（6.2）

贪心种子扩张，作用域划分 + 参数扫描：

```text
scopes {'ying': 3129, 'mo': 118, 'shi': 254, 'jian': 39} M (3540, 2048)
tau=0.80 clusters=132 members=675 mean=5.11 max=15 p50=5 p90=7 min=4
tau=0.82 clusters=96  members=478 mean=4.98 max=14 p50=4 p90=6 min=4
tau=0.84 clusters=75  members=350 mean=4.67 max=9  p50=4 p90=6 min=4
tau=0.86 clusters=42  members=192 mean=4.57 max=8  p50=4 p90=6 min=4
```

`tau=0.82` 最大簇（14 成员）成员样本：

```text
"助手提出方案 A（改动最小）：修改 hermes_cli/update_cmd.py 的 _cmd_update_impl——5404 行 branch 解析直…"
"助手推荐的改法 A（最小侵入）：在 hermes_cli/update_cmd.py 的目标解析与 checkout 段新增 release 模式开关（--re…"
"实现方案：只在阶段 A 的 branch 解析处加一个 release 分支，阶段 B 一行不动。5 步实施计划：1) 5404 行前加 release 模式解…"
"用户改变此前将改动 commit 到 main 的做法，决定改动不提交到 main 分支，main 保持上游原版干净；创建 release-pin 分支承载改动…"
```

### A.3 LLM 成本（6.3）

96 簇逐簇真实调用：

```text
clusters=96 ok=25 null=71 err=0 elapsed_s=284.9
prompt_tokens=111516 completion_tokens=38633 reasoning_tokens=33717
per_cluster: prompt=1162 completion=402 seconds=2.97
sizes [14,12,12,9,8,8,8,7,7,6,6,6, …]
```

单簇耗时区间（`per` 明细摘要）：最小 0.49 s（4 成员）、最大 14.82 s（6 成员，completion 2,469 tokens）；最大 prompt 2,461 tokens（14 成员簇）。

产出样本（14 成员簇）：

```json
{"observation": {"text": "用户的核心诉求是让 `hermes update` 更新到最新的 release tag，而不是跟随 main 分支的滚动最新；为此多轮讨论始终收敛到同一套最小侵入方案——只在 hermes_cli/update_cmd.py 的阶段 A（branch/tag 解析与 fetch）增加 release 模式开关（--release 或 HERMES_UPDATE_RELEASE=1），阶段 B（依赖安装、构建）与默认行为完全不变。",
 "source_ids": ["3b567c50-…","1e226ca3-…","20c5cee2-…","7642f6d1-…","ce4fa28d-…","28538305-…","1dccdb85-…","3badbcb5-…","0d438b36-…","11f5cd04-…","babfcd86-…","0482a83a-…"],
 "counterexample": []}}
```

embedding 上限：

```text
batch=20 dimensions=2048 -> dims=2048 elapsed=1.04s
batch=21 FAILED: BadRequestError("… batch size is invalid, it should not be larger than 20.: input.contents")
no dimensions -> dims=1024
```

### A.4 检索与索引（6.4）

3 条探针记录（1 条 `memory_kind=observation`、1 条 `memory_kind=fact`、1 条键缺失）上的过滤结果：

```text
must_not observation: [1, 3]      # 键缺失 + fact
must observation:     [2]         # observation
count all: 3 / count must_not: 2
```

keyword 数组与精确命中：

```text
filter source_memory_ids=s3 -> [{id:10,payload:{source_memory_ids:[s1,s2,s3]…}}, {id:11,…[s3,s4]…}]
filter observation_key=k-aaa -> [10]
count kind=observation: 2 -> dup upsert 后 3（不同 id 同 key 会重复）
```

集合元信息：

```text
"points_count": 3532, "indexed_vectors_count": 2491
payload_schema: run_id/actor_id/valid_at/invalid_at/created_at/user_id/agent_id 均存在
count(must_not memory_kind=observation) = 3545（= 集合总数）
```

端到端（`POST /search`，隔离 `user_id=test_dream_probe`）：

```text
A default        [probe] 观察样本… / [probe] 普通事实样本…     （默认返回两条）
B no-obs         [probe] 普通事实样本…                          （NOT 过滤生效）
C only-obs       [probe] 观察样本…                              （AND 过滤生效）
```

单条读取返回的字段形态（证明观察字段当前落在 `metadata`）：

```json
{"id": "ffffffff-…-0001", "memory": "[probe] 观察：…", "hash": "probehash1",
 "metadata": {"memory_kind": "observation", "observation_key": "probe-key-1",
              "source_memory_ids": ["s1","s2","s3"], "evidence_count": 3},
 "valid_at": null, "invalid_at": null, "superseded_by": null, "invalid_reason": null}
```

### A.5 幂等与规模（6.5）

```text
count before=3545 after_first=3546 after_rerun=3546      # 同 point id 重写不增
payload now: 第二版观察（重跑覆写）| evidence_count 2      # 覆写生效
idempotent_id_reuse: True
same key, different id -> duplicates: 2                   # 证明幂等必须靠 point id
uuid5 stable: d3fd67d5-c24b-5035-8b79-5f08413b4449 (两次相同)
fetched 3540 in 1.1s / matrix (3540, 2048) 1.3s
elapsed 3.0s（四作用域全量聚类）
```

lock-file（容器内 `/app/history`）：

```text
A acquired
B blocked errno=11
B acquired (UNEXPECTED)   # ← 这是 A 释放锁之后的预期行为
lock file removed: True
```

同进程异 fd：

```text
A acquired
B blocked in same process, errno=11 (EAGAIN)
after A released: B acquired (expected)
```

环境与依赖：

```text
docker stats: mem0 259.8MiB/7.737GiB, qdrant 309.4MiB/7.737GiB, postgres 66.5MiB, dashboard 105MiB
lima docker: 2 CPU / 8GiB / 64GiB
/app 挂载: virtiofs (ro)；/app/history 挂载: /dev/vda1 ext4 (rw, 61G, 用 17G)
numpy 2.5.2（容器内既有）
调度库 importlib.util.find_spec → ['apscheduler','schedule','filelock'] 全部为 None
```

### A.6 探针清理

`dream_probe_tmp` / `dream_probe2` / `dream_probe3` 三个临时集合已删除；`memories_2048` 中的全部探针记录（`ffffffff-…`）已删除，清理后 count 回到 3545。当前集合列表：

```text
['bt_ac23_48c7b96d', 'bt_probe_b54b5a59', 'memories', 'memories_2048']
```

## 附录 B：术语表

| 术语 | 含义 |
| --- | --- |
| observation（观察） | 由一组相关事实精炼出的更高层信念条目；与源事实并存，不取代源事实 |
| 源事实 | 被某条观察引用的既有记忆条目 |
| 成员指纹 `observation_key` | 簇成员集合的 sha256 摘要；同一组源事实恒得同一 key |
| 簇（cluster） | Gather 阶段按向量相似度聚出的相关事实集合，是观察的输入单元 |
| 证据链 | 观察 → `source_memory_ids` → 源事实的双向可追溯关系 |
| 演化 | 同一支观察的成员集合发生变化，新版本取代旧版本（旧版本获得失效标记） |
| dry-run | 完整执行四阶段但零数据写入的运行模式，产出候选报告 |

## 附录 C：参考文献与形态来源

| 来源 | 本方案的采纳点 |
| --- | --- |
| Hindsight observations（refined not overwritten + 精确引文 + proof count） | 观察是新增而非覆盖、携带源记忆 id 列表与计数（F3/F5） |
| Nomos（Orient → Gather → Consolidate → Prune 四阶段 + lock-file） | 四阶段划分与阶段职责、lock-file 并发互斥（3.1、5.6） |
| RecMem（lazy consolidation 的时机论证） | 整合在独立周期任务中执行、不进写入路径（3.2、5.6） |
| mem0 上游 Dream / Synthesis（`docs/platform/features/dream.mdx`、`docs/api-reference/dream/*`） | 端点形态参照（preview / runs / sources 三分组）；**偏离点**：上游按纯 `user_id` 作用域且只处理启用后的新记忆，本机记忆全部带 `agent_id`，故作用域改为 `(user_id, agent_id)` 二元组；上游不区分观察与事实（pattern memory 与普通记忆同权返回），本方案以 `memory_kind` 区分并默认不返回观察 |
| bi-temporal 事实模型设计（`docs/design/bitemporal-fact-model.md`） | 条款同构：payload-only 更新、失效不删除、悬空指针不清洗、`invalid_reason` 枚举扩展、读侧一等字段提升 |
