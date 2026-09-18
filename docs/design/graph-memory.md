# graph-memory 设计（mem0 自托管记忆：Graphiti 旁路图检索）

| 项 | 值 |
| --- | --- |
| 目标仓库 | `/Users/xuelingkang/Documents/Containers/mem0`（`main`） |
| 基线 commit | `63fa9116`（memory-decay 文档收口后） |
| 依赖机制 | bi-temporal 事实模型、Ebbinghaus 衰减 + 访问强化，均已交付并核验通过 |
| 前置文档 | `docs/design/bitemporal-fact-model.md`、`docs/design/memory-decay.md` |
| 生效面 | 写入路径（事实入图）与检索排序层（`/search` 路径）；事实主存仍在 Qdrant |
| 存储 | Qdrant 1.19.0 集合 `memories_2048`（3612 点 / 向量 2048 维 + `bm25` sparse）；新增 FalkorDB 图库（图键按作用域隔离） |
| 影响面 | SDK 新增图同步模块、SDK 打分器与检索路径、SDK 配置层、REST 配置透传与 `explain` 输出、新增图桥服务与两个 compose 服务 |
| 实现形态 | 仅本地改动（相对 `upstream/main`），不提交上游 PR |
| 资源结论 | **就地可跑**，VM 维持 2 CPU / 8GiB（第 3 节） |

> **2026-09-17 修订（核验整改卡 `t_362c77d2`，基于独立核验卡 `t_e6669818` 的未通过项）。**
> 四处与实测不符的判据/表述已按实测改写：`[AC-12]` 后半句的占比上界写进了前提（§11.3）、
> `[AC-34]` 拆成「健康路径确定 + 降级路径显式」两条（§11.3）、§10 的 E6 换成实测尾部分位并
> 据此把 `timeout_seconds` 默认值由 0.4 抬到 1.0（§6.5、§10）、两项未申报的实现偏离如实
> 记录（§7）。同时补齐两处观测面：图派发计数器的服务侧只读端点（§8、§7）与检索响应里的
> `graph_status`（§6.4、§8）——超时此前与「无命中」在响应上完全同形，无法诊断。

> 本文档为本地设计文档，位于仓库 `docs/design/`，与 Mintlify 文档站（`docs/**/*.mdx`）无关，不参与 `llms.txt` 收录检查。

---

# 1. 项目概述

## 1.1 背景

`/search` 当前按混合打分排序：语义相似度 + BM25 + 实体加权，再乘时间因子（衰减）：

```
combined   = min((semantic + bm25 + entity_boost) / max_possible, 1.0)
final      = combined × decay_weight
```

四个信号都只回答「这条记忆与查询文本有多像」。事实之间的**关系结构**（谁属于谁、谁依赖谁、同一实体的其余事实）不参与检索：查询命中「学科网基础应用中心」时，图中与之相连的「影/墨/鉴」「umbrellaedge 仓」等事实拿不到任何加分。

上游 mem0 在 v3 管道落地时移除了 `mem0/graphs/`（本地已确认该目录不存在），实体抽取仅保留用于排序加权。云平台 v3 的图记忆是内置实现，本地自托管版没有对应能力。

本方案为自托管 mem0 引入**旁路图检索**：事实入图（实体 + 关系 + 时间），图遍历结果作为**补充信号**参与既有融合。

## 1.2 目标

1. 新事实入库后同步为图中的 episode，图中的实体与关系由 Graphiti 抽取维护。
2. 检索时并行做一次图检索，命中的事实按「关联到的记忆」折算为补充加分，参与既有加性融合。
3. **图不可用时主链路不受影响**：写入照常、检索照常，图分支静默跳过（第 6.4 节逐条）。
4. 同步是异步的：入图不阻塞主写入响应，失败可重试。
5. 图与事实主存通过**构造性 join key**（episode uuid = memory id）双向可追溯，图上不另存映射表。
6. 与衰减的时间因子共存：两个机制作用面不同（关联维 vs 时间维），复合顺序固定，各自有界（第 6.3 节）。
7. 全部能力由一个开关控制；关闭时检索与写入行为与引入本机制之前逐位一致。

## 1.3 范围

包含：

- `server/graph-bridge/`：新增旁路图桥服务（FastAPI 薄壳，直接对接 graphiti-core 与 FalkorDB）。
- `server/docker-compose.yaml`：新增 `falkordb` 与 `graph-bridge` 两个服务。
- `mem0/memory/graph_sync.py`：新增图同步模块（派发队列、串行 worker、重试、幂等判据、熔断）。
- `mem0/utils/scoring.py`：打分器接受图加分，分母随启用信号增长。
- `mem0/memory/main.py`：写入路径派发入图、检索路径调用图检索并计算加分（sync / async 两份实现），并把本次图分支状态随每条结果发布。
- `mem0/configs/base.py`：新增图检索配置段。
- `server/main.py`：配置默认值透传、`score_details` 序列化。
- `server/routers/graph.py`（新增）：图派发计数器的只读端点（`GET /graph/stats`）。
- `server/docker-compose.yaml` / `server/.env.example`：图能力相关的环境变量透传。
- `tests/`：新增单测与端到端验证。

---

# 2. 需求分析

## 2.1 功能需求

| 编号 | 需求 |
| --- | --- |
| F1 | 每条新写入的记忆生成一个图 episode，episode uuid 等于该记忆的 memory id |
| F2 | 入图是异步的：`POST /memories` 的响应不等图完成，入图失败不回滚事实写入 |
| F3 | 入图失败可重试；同一记忆重复入图在图侧只有 1 个 episode |
| F4 | 检索时按查询文本做一次图检索，返回事实及其关联的记忆 id 列表 |
| F5 | 图命中折算为补充加分，参与既有加性融合，且分母随启用信号增长 |
| F6 | 图加分有上限，且只对候选池内已有条目生效（不引入候选、不淘汰候选） |
| F7 | 图检索在超时预算内完成；超时/失败/畸形数据一律按「本次无图信号」处理 |
| F8 | 图侧标为失效（`invalid_at` 非空）的事实默认不参与加分，与主通道的失效语义一致 |
| F9 | 开关关闭时不产生图调用、不改变评分与返回 |
| F10 | `explain=true` 时图信号的分量在 `score_details` 中可见 |

## 2.2 非功能需求

| 项 | 要求 |
| --- | --- |
| 主写入在线路径 | 图派发为内存队列投递，O(1) 且不抛异常；图不可用时写入响应时延与关闭态同量级 |
| 主检索在线路径 | 图分支有硬超时预算（默认 1.0s；实测图桥 `/search` n=100 的 p50 0.163 / p90 0.445 / p95 0.514 / p99 0.812 / max 0.842s，见 §10 E17）；超时即放弃该次图信号，响应最多被延长到预算上限，且该次状态在响应里标为 `timeout` |
| 并发模型 | 入图单 worker 串行（Graphiti 要求同一分区的 episode 顺序摄入）；图库连接按作用域缓存复用 |
| 新增 LLM 调用 | 仅在图侧（写入路径），主检索路径零 LLM 调用 |
| 幂等性 | 同一 memory id 的 episode 在图侧唯一；重复派发不产生重复边 |
| 可观测 | 派发/成功/失败/丢弃/熔断计数（`GET /graph/stats`）、图分支状态（每条检索结果的 `graph_status`）、图库节点边数（图桥 `/stats`）、图检索时延与命中数均可查 |
| 资源 | 新增常驻内存 ≤ 215MiB（实测基数，第 3 节）；新容器均设内存上限，防止挤占主链路 |
| 隔离 | 图分区键由作用域派生，按 `user_id` 天然隔离；测试数据可按分区整体删除 |
| 可复现 | 图信号计算为纯函数（给定图检索结果与候选池，输出确定），不依赖 LLM |

---

# 3. 资源评估结论（硬前置）

## 3.1 现状用量（实测，2026-09-17）

VM（lima `docker`，vz / aarch64）：

| 项 | 值 | 来源 |
| --- | --- | --- |
| 配置 | 2 vCPU / 7922MiB / 61G 磁盘 | `limactl list`、`free -m`、`df -h` |
| 运行时用量 | `used` 981MiB，`available` 6941MiB，swap 总量 0 | `free -m`（四服务在线时） |
| 磁盘 | 20G 已用 / 42G 可用 | `df -h /` |

四个既有服务常驻（`docker stats --no-stream`）：

| 容器 | 常驻内存 |
| --- | --- |
| mem0-dev-mem0-1 | 278.4MiB |
| mem0-dev-qdrant-1 | 247.0MiB |
| mem0-dev-mem0-dashboard-1 | 106.5MiB |
| mem0-dev-postgres-1 | 67.0MiB |
| **合计** | **≈ 699MiB** |

## 3.2 新增组件的资源实测

| 组件 | 形态 | 实测/估算 | 依据 |
| --- | --- | --- | --- |
| FalkorDB | 独立容器，Redis 协议 6379 | 空载 **67.5MiB**；目标规模数据集 **8.25MiB**（RSS 37.46MiB 不变） | E3、E4 |
| 图桥服务（graphiti-core 进程） | 独立容器 | **145.3MiB**（官方同栈镜像 idle）；本机进程跑 5 条 episode 后峰值 **155MB** | E5、E6 |
| 图库数据（3500 条事实规模） | 内存型 | 8150 实体节点 + 3545 边 → `used_memory` 7.00MiB → **10.53MiB** | E4 |
| 镜像磁盘 | — | falkordb 展开 570MB、图桥镜像展开 582MB（Hub 拉取 arm64 206.7MB / 192.0MB） | E15 |

对照后端 Neo4j（同机空载实测）：容器 **328MiB → 507.1MiB**，默认 `server.memory.pagecache.size = 512MiB`，JVM 堆另计（E14）。

## 3.3 结论

**就地可跑。** 新增常驻预算 ≤ 215MiB（FalkorDB 68MiB + 图桥 145MiB）：

- 占 VM 总内存 7922MiB 的 **2.7%**；
- 相对运行时 `available` 6941MiB 的 **3.1%**，落地后余量 ≥ 4.7GiB；
- 磁盘新增 ≤ 1.2GB，相对 42G 可用余量占 **2.9%**；
- 无需停 VM、无需改 `limactl` 配置；VM 保持 2 CPU / 8GiB。

落地方式对资源的影响：

| 手段 | 配置 | 作用 |
| --- | --- | --- |
| 选 FalkorDB 为图库载体 | 见 4.3 | 目标规模数据集 8.25MiB，常驻 68MiB（Neo4j 同机空载 507MiB） |
| 容器内存上限 | `graph-bridge` `mem_limit: 512m`、`falkordb` `mem_limit: 384m` | 图侧异常膨胀时被 cgroup 截断，主链路不受牵连 |
| 入图并发上限 | 单 worker 串行 + `SEMAPHORE_LIMIT=1` | 图侧 LLM 调用不与 mem0 主链路的 LLM 调用争抢；2 vCPU 下 `docker stats` 观测图桥容器 CPU 在 0.2% 量级（E5） |
| 分步启动 | 先 `falkordb` → 观察 → 再 `graph-bridge` | 每次只新增一个容器，便于定位资源变化（E3/E5 即为分步观测数据） |

写入侧的 LLM 成本由事实写入量决定：实测一条中文事实的图摄入 **14.24–36.65s**（中位 20.52s，LLM 主导，E6）。当前事实写入频率为近 7 天 520 条（74.3 条/天，见 memory-decay 文档 E10），折算图侧 LLM 负载约 **25–45 分钟/天**，由单 worker 串行吸收，远低于 2 vCPU 的容量。

存量 3500+ 条事实按 20s/条串行回填约需 19 小时 LLM 占用，本阶段按增量同步交付（F1 只覆盖新写入）；存量回填作为独立后续卡（第 12 节），其触发点与限速方案在第 5.5 节预留。

---

# 4. 部署形态与图库选型

## 4.1 形态选择

| 形态 | 形态描述 | 与主链路的关系 | 代价 | 本方案结论 |
| --- | --- | --- | --- | --- |
| S1 进程内嵌 | graphiti-core 作为 mem0 进程的依赖，直连 FalkorDB 容器 | 同进程、同资源池 | 无独立进程；图侧异常与内存增长落在 mem0 进程内，无法单独设上限 | 最小可行 |
| **S2 旁路图桥（采用）** | 独立薄壳服务持有 graphiti-core 与三个客户端，对接 FalkorDB；mem0 侧经 HTTP 调用 | 独立容器、独立生命周期 | 多一个 ≤ 512m 的容器与一次 HTTP 跳（毫秒级） | **采用** |
| S3 异步旁路 worker | 独立进程读 Qdrant 新增点 → 写图 → 回写 Qdrant 映射字段 | 两个写入者写同一集合 | 事实主存出现第二个写入者，与「Qdrant 唯一主存」冲突 | 备选 |

采用 S2 的依据：

1. **图侧故障不外溢**：容器级 `mem_limit` 与独立重启是主链路安全底线的物理保障；S1 的图侧 OOM 与 mem0 共享同一内存池。
2. **客户端可完全按本机通道构造**：图桥自行装配 LLM 与 embedder 客户端（当前通道由 E18 实测选定），图桥不修改 graphiti-core 一行源码，第三方库保持上游状态。
3. **探测与升级面收敛**：图桥的 `/health`、`/stats`、`/graph/{group_id}` 三个端点同时充当运维面与测试面（第 8 节、第 11.6 节）。
4. **官方镜像的路径已排除**（4.2 三处硬缺口，均为实测），自建薄壳的增量成本已不可避免；既然自建，就取进程隔离与内存上限。

## 4.2 官方 `zepai/graphiti` 镜像的实测缺口

以 `zepai/graphiti:0.30.2`（镜像内 graphiti-core 版本 0.30.2，与 PyPI 最新一致）实测：

| 编号 | 现象 | 实测输出 | 对本方案的影响 |
| --- | --- | --- | --- |
| G1 | 镜像不含 FalkorDB 驱动 | `ImportError: falkordb is required for FalkorDriver` → 应用启动失败 | FalkorDB 载体需自建镜像（`--build-arg INSTALL_FALKORDB=true`） |
| G2 | embedder 模型不可配置 | `EMBEDDING_MODEL_NAME` 在 `server/graph_service/config.py` 中声明但未接线；实际用默认 `text-embedding-3-small` → `422 ... API密钥未配置该模型` | 本机 embedder 通道（`qwen3.7-text-embedding`）无法接入；需自建服务或打补丁 |
| G3 | REST 面无法建立 join key | `/messages` 接收 `message.uuid`，但 `add_episode` 要求该 uuid 的 episode 已存在（`NodeNotFoundError`），而 REST 面没有创建 episode 的端点；`/messages` 响应仅 `{"message","success"}`，不回传 episode uuid | 第 5.4 节的构造性映射无法经由 REST 建立 |

镜像内容与 local clone 一致（`/app/graph_service` + `/app/.venv` 内 graphiti-core 0.30.2）；Neo4j 路径可启动（`/healthcheck` → `{"status":"healthy"}`），`/search` 因 G2 失败。

## 4.3 图库后端选型

graphiti-core 的 `GraphProvider` 枚举覆盖 NEO4J / FALKORDB / KUZU / NEPTUNE（无 Memgraph 驱动）。

| 维度 | **FalkorDB（采用）** | Neo4j | Kuzu | Neptune |
| --- | --- | --- | --- | --- |
| 进程形态 | 独立容器，Redis 协议 6379 | 独立容器，Bolt | 嵌入式（无官方镜像） | AWS 托管服务 |
| 本机空载实测 | 容器 **67.5MiB** | 容器 **328MiB → 507.1MiB**，`pagecache 512MiB` | — | — |
| 目标规模（8150 节点 + 3545 边） | `used_memory` **10.53MiB**，数据集 8.25MiB | 页面缓存 512MiB 起，堆另计 | 列存磁盘型，带 buffer manager | 另需 OpenSearch Serverless |
| 官方最低内存 | 文档未给下限（内存型引擎，容量受 RAM 约束） | **2GB 最低 / 16GB 推荐** | 文档未给下限 | 托管侧 |
| 镜像 | `falkordb/falkordb:latest` 展开 570MB（Hub arm64 206.7MB） | `neo4j:5.26-community` 展开 660MB | 无官方镜像 | 无本地镜像 |
| 鉴权 | 本地实例默认无鉴权 → 仅内网可达 | 用户名/密码 | 文件权限 | IAM |
| graphiti-core 支持 | `FalkorDriver`，`graphiti-core[falkordb]` 官方 extra | `Neo4jDriver` | 驱动代码内抛 `DeprecationWarning`：上游 Kuzu 项目已归档、迁移至 Neo4j/FalkorDB | `NeptuneDriver` |
| 本机余量下的承载 | 68MiB 常驻 + 8.25MiB 数据集 | 507MiB 常驻 + 512MiB pagecache（占 VM 总内存 12.9%） | 载体不可持续 | 依赖外部云服务 |
| 选型结论 | **采用**：为 8.25MiB 的目标规模付出 68MiB 常驻，2 vCPU/8GiB 下余量充足 | 对照：同等数据量下多付 440MiB 常驻与 512MiB 页缓存 | 对照：上游归档 | 对照：不适用本机自托管 |

补充：FalkorDB 亦是 `FalkorDB/mem0-falkordb` 插件所用的图库（本方案不依赖该插件，仅作生态一致性说明）。

## 4.4 服务拓扑与数据流

```
                     ┌──────────────────────────────┐
   POST /memories ──▶│  mem0 (既有容器)              │
                     │   ├─ Qdrant 写入（主存，不变） │
                     │   └─ graph_sync 派发 ──┐      │
                     └────────────────────────┼──────┘
                                              │ HTTP（内网，异步队列）
                     ┌────────────────────────▼──────┐
   POST /search  ───▶│  graph-bridge (新增，≤512m)    │
     （图分支，       │   ├─ /episodes  入图           │
       1.0s 预算）    │   ├─ /search    图检索         │
                     │   ├─ /health    /stats         │
                     │   └─ graphiti-core 0.30.2      │
                     └────────────────────────┬──────┘
                                              │ Redis 协议
                     ┌────────────────────────▼──────┐
                     │  falkordb (新增，≤384m)        │
                     │   图键 = 作用域派生（每 user 一图）│
                     └───────────────────────────────┘
```

数据流三条：

| 流 | 触发 | 数据 | 失败语义 |
| --- | --- | --- | --- |
| 事实入图 | `POST /memories` 成功写 Qdrant 后 | `{uuid: memory_id, group_id, text, reference_time, source_description}` | 队列重试 → 丢弃并计数；Qdrant 事实保留 |
| 检索融合 | `POST /search` 候选集构建后 | `{query, group_ids: [作用域键], max_facts}` → `facts[{uuid, name, fact, valid_at, invalid_at, episodes[]}]` | 超时/异常 → 本次无图信号 |
| 运维观测 | 人工/探针 | `/health`、`/stats`、`/graph/{group_id}`（删除） | 无影响 |

分区键（`group_id`）由作用域派生，与写入派发使用同一个纯函数（单一来源）：`user_id` 优先，其次 `agent_id`，再次 `run_id`，形如 `mem0:<value>`；因此隔离 `user_id` 的验证数据落在独立图键，验证后可整体删除（第 11.6 节）。

---

# 5. 写入同步机制

## 5.1 派发点与派发形态

派发点在事实写入成功之后（`Memory._add_to_vector_store` 与 `AsyncMemory._add_to_vector_store` 两份实现的收尾处），派发内容为 `(memory_id, text, created_at, 作用域)`：

- 队列为**进程内有界队列**（默认容量 1000），满时丢弃最旧任务并累加 `graph_dropped` 计数；
- 投递是 O(1) 的内存操作，被 `try/except` 包裹，任何异常记计数器后返回，不改变写入结果；
- worker 为**单线程 + 独立事件循环**：串行调用图桥 `/episodes`，与 FastAPI 的请求线程池互不占用（与衰减的强化写入派发同构）。

## 5.2 重试与熔断

| 参数 | 默认 | 语义 |
| --- | --- | --- |
| `max_retries` | 3 | 单条 episode 的最大重试次数 |
| `retry_backoff_seconds` | 5.0 | 线性退避基数（第 n 次重试等待 `n × 基数`） |
| `circuit_breaker_failures` | 5 | 连续失败达到该值进入冷却 |
| `circuit_cooldown_seconds` | 60 | 冷却窗口内派发直接丢弃并计数，冷却结束后放行一条探测 |
| `queue_size` | 1000 | 队列容量 |

图桥在 `add_episode` 完成（含图写入落库）之后才返回 2xx；非 2xx、连接失败、超时一律视为失败。

## 5.3 幂等

图桥的 `/episodes` 在入图前做一次前置判定：该 `group_id` 下存在 `uuid` 相同的 Episodic 节点且其 `entity_edges` 非空 → 直接返回 `already_synced`，不重复抽取。判据可在图上直接观测（E12 实测 `size(e.entity_edges) = 3`）。

该前置判定是必要的：实测同一 uuid 重放 `add_episode` 会重新抽取，边数 4 → 6、实体数 5（去重作用在节点层，边不合并），因此「重放即幂等」在图侧成立的条件是**先确认未成功**（E12）。

## 5.4 join key（构造性映射）

图侧不另存映射表：**episode uuid 直接取 memory id**。

- 写入：图桥先创建 `EpisodicNode(uuid = memory_id)` 并落库，再以同一 uuid 调 `add_episode`。图桥内部持有 graphiti-core，uuid 由入参控制。
- 读取：Graphiti 的每条关系边带 `episodes: list[str]`，即产生该事实的 episode uuid 列表；由于 episode uuid 就是 memory id，**图检索结果直接携带对应的 memory id**。

实测（E11）：5 条中文事实入图后，`/search` 命中的每条事实的 `episodes` 均等于对应 memory id；检索结果与库内实体/关系（11 实体 + 8 边）逐条可读。

两类 uuid 语义一致：mem0 的 memory id 为 uuid4 字符串（Qdrant 点 id 实测形如 `000393e2-27ad-478c-8b99-9039573897ac`），满足 Graphiti 的 uuid 字段要求。

FalkorDB 后端的一处机制约束（E13）：`group_id` 即图键（database）名，driver 在 `group_id ≠ driver.database` 时会克隆 driver 指向该图键。因此图桥构造 driver 时以作用域键为 database，使读写落在同一图键。

## 5.5 存量与回填

本阶段同步范围为**新写入的事实**；图内 episode 数等于实施后新增事实数，与存量 3500+ 条无关（第 11.2 节可判定）。

存量回填不在本阶段范围，其接口形态在图桥侧预留：`POST /backfill {group_id, limit, rate}` —— 按 Qdrant 游标顺序取事实、限速入图、可中断续跑。触发条件为「新写入的图覆盖度不足以支撑检索增益」的观测结论（第 12 节列为独立卡）。

---

# 6. 检索融合与降级路径

## 6.1 图检索调用

位置：`_search_vector_store` 的候选集（`candidates`，语义检索过 `threshold` 之后）构建完成、进入 `score_and_rank` 之前。图分支与既有 BM25 / 实体加分的取数并列，互不依赖。

调用：`POST {endpoint}/search {group_ids: [作用域键], query, max_facts}`，在**硬超时预算**内（默认 1.0s；实测分位见 §10 E17）同步取回；超时或异常返回空结果，并把该次状态带走。

图侧结果的采用规则：

| 规则 | 内容 |
| --- | --- |
| 失效事实 | `invalid_at` 非空的事实默认不参与加分（与主通道失效语义一致，可由配置放开） |
| 映射范围 | 事实 `episodes` 中的 id 只有在**本次候选池内**才产生加分；池外 id 忽略 |
| 条数 | 采用 `max_facts` 条（默认 10），按图检索返回顺序定权 |
| 本次状态 | 每次调用带一个状态值（`ok` / `skipped` / `timeout` / `error` / `disabled`），随检索结果的 `graph_status` 发布（§6.4） |

## 6.2 融合公式

对每条候选记忆 `m`：

```
rank_k(f)   = 图检索返回的第 k 条事实（k 从 1 起）
w(f)        = 1 / (1 + 0.5 × (k − 1))                      # 有界、单调递减，w ∈ (0, 1]
graph_boost(m) = W_g × max{ w(f) | m ∈ episodes(f), f 被采用 }   # W_g 默认 0.5
```

打分器：

```
has_graph    = 候选池中至少一条候选的 graph_boost > 0
max_possible = 1.0 + (has_bm25 ? 1.0 : 0) + (has_entity ? 0.5 : 0) + (has_graph ? 0.5 : 0)
combined     = min((semantic + bm25 + entity_boost + graph_boost) / max_possible, 1.0)
final        = combined × decay_weight
```

要点：

1. **加性、非乘性**：图信号进入分子，与 BM25 / 实体加分同层次；`threshold` 仍作用在原始语义分上（在加性融合之前），因此图信号不引入低于阈值的候选，也不淘汰任何候选。
2. **分母条件增长**：`max_possible` 仅在候选池内确有候选获得图加分时 +0.5（沿用 `has_bm25` / `has_entity` 的判断形态）；无图信号时公式与取值逐位回到本机制之前。
3. **有界**：`W_g = 0.5` 与实体加入上限同量级；四信号全开时图信号对最终分的贡献上限为 `0.5 / 3.0 = 16.7%`。
4. **复合顺序固定**：先加性融合（含图），再乘时间因子。图信号不参与 `decay_weight` 的计算，`decay_weight` 也不改变图信号。
5. **纯函数**：给定「图检索结果 + 候选池」，`graph_boost` 输出确定，不依赖 LLM 与网络重放。
6. **可解释字段**：`explain=true` 时 `score_details` 新增 `graph_boost`（本条候选的图加分）与 `graph_facts`（命中该候选的事实条数），既有键语义不变。

## 6.3 与 Decay 的共存方式

| 项 | 衰减时间因子 | 图信号 |
| --- | --- | --- |
| 作用维度 | 时间（龄 + 召回足迹） | 关联（实体与关系路径） |
| 组合位置 | 乘性系数，作用于 `combined` 之后 | 加性分量，进入 `combined` 分子 |
| 取值方向 | 只下调，区间 `[0.90, 1.00]` | 只上调，区间 `[0, 0.5]` |
| 触发条件 | 候选被检索返回 | 候选对应的记忆被图检索命中 |
| 关闭态 | 系数恒为 1 | 加分为空，分母不减 |

**联合保序判据（充分条件）**：候选对 A、B 满足

```
s_A ≥ (s_B + W_g) / FLOOR          # W_g = 0.5，FLOOR = 0.90
```

则图信号与时间因子共同作用下 A 恒排在 B 之前。推导：图信号单侧最多加 `W_g`，时间因子单侧最多下调到 `FLOOR`，故

```
final_A = (s_A + g_A)/M × d_A ≥ s_A/M × FLOOR
final_B = (s_B + g_B)/M × d_B ≤ (s_B + W_g)/M × 1.0
```

当 `s_A × 0.90 ≥ s_B + 0.5` 时有 `final_A ≥ final_B`。例：`s_B = 0.30` 时 `s_A ≥ 0.889` 即免疫两种机制的重排。

两机制互不夺取主导权的机制级理由：各自单独作用时，可改变分值的幅度都远小于 1（0.5 与 0.10），而语义分之间的差距由语料决定；联合作用的上界由上述不等式给出，`threshold` 前置过滤使「低相似度但高关联」的候选无法凭图加分越过相似度门槛进入候选池。

## 6.4 降级路径（逐条）

| 故障 | 主链路（写入 / 检索）行为 | 图侧行为 |
| --- | --- | --- |
| 图桥进程停止 | `POST /memories` 正常返回，事实落 Qdrant；`POST /search` 正常返回，结果等于关闭态 | 派发失败 → 重试 → 丢弃并计数；连续失败触发熔断 |
| 图库（FalkorDB）停止 | 同上 | 图桥返回 5xx，按失败处理；队列积压至容量上限后丢弃 |
| 图桥响应超过超时预算 | 检索在预算内放弃图分支，响应时间不增加 | 在途请求被放弃，不阻塞后续 |
| 图桥返回畸形数据（缺字段 / 空列表 / 无法映射的 id） | 忽略无法映射的条目，返回正常 | — |
| 图桥重启窗口（健康检查失败） | 主链路不等待、不重试阻塞 | 熔断冷却期内派发直接丢弃并计数 |
| 图侧内存超限被 cgroup 截断 | 主链路不受影响（独立容器） | 容器重启，恢复后队列继续按序入图 |
| 网络分区（VM 内部网络异常） | 同「图桥进程停止」 | 同 |

统一原则：**入图失败不回滚事实写入；检索缺图信号即按无信号打分；图侧的每一次降级都是显式状态——状态值、计数器与日志共同构成可见面，而不是被静默吞掉。**

状态值的判定面（`graph_status`，随每条检索结果发布；图能力关闭时也发布，取值为 `disabled`）：

| 状态 | 含义 | 对打分的影响 |
| --- | --- | --- |
| `ok` | 图桥在预算内返回了结果（命中数可能为 0） | 命中即加分，未命中即 0（分母不增长） |
| `timeout` | 图桥未在预算内返回 | 本次无图信号 |
| `error` | 图桥在预算内失败（连接失败 / 非 2xx / 畸形响应） | 本次无图信号 |
| `skipped` | 能力开启，但无作用域键或候选池为空，未发起调用 | 本次无图信号 |
| `disabled` | 能力关闭，未发起任何图调用 | 无图分量，分数与关闭态逐位一致 |

降级路径下「结果等于关闭态」的准确含义是 **id 序与分数**等于关闭态（`[AC-20]`/`[AC-21]` 的判定面）；`graph_status` 正是两者唯一的差别，也是这次降级可被诊断的依据——7.5% 的静默降级在调用方视角曾是「同一查询时而带图增强时而不带」且无从查因。

## 6.5 开关与回退

配置段落在 SDK 配置层（`MemoryConfig` 新增 `graph` 段），默认由环境变量驱动，服务侧沿用既有 `DEFAULT_CONFIG` 通道：

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `enabled` | `false` | 图能力总开关；关闭时不派发、不查询、不加分 |
| `endpoint` | `http://graph-bridge:8000` | 图桥地址 |
| `weight` | 0.5 | `W_g`，图加分上限 |
| `max_facts` | 10 | 每次图检索取用的事实条数 |
| `timeout_seconds` | 1.0 | 图检索硬超时预算（0.4 → 1.0：实测尾部见 §10 E6/E17） |
| `include_invalidated` | `false` | 是否采用 `invalid_at` 非空的事实 |
| `queue_size` | 1000 | 派发队列容量 |
| `max_retries` | 3 | 单条 episode 最大重试次数 |
| `circuit_breaker_failures` / `circuit_cooldown_seconds` | 5 / 60 | 熔断阈值与冷却窗口 |

回退语义：关闭开关后不产生任何图调用与图写入；`max_possible` 与 `final_score` 回到无图形态；`score_details` 中不出现图分量键，`explain` 输出与引入本机制之前逐位一致；每条结果只多一个只读字段 `graph_status = "disabled"`（不参与打分，也不进入 `score_details`），使「能力关闭」与「图没答上来」在响应上不再同形。

---

# 7. 各文件改动点与新增部署文件

| 文件 | 层 | 改动 |
| --- | --- | --- |
| `server/graph-bridge/app.py`（新增） | 图桥 | FastAPI 薄壳：装配 graphiti-core 客户端（LLM 用 `OpenAIGenericClient` 的 `/chat/completions` 通道，embedder 用 OpenAI 兼容 embedder 客户端）、作用域→图键映射、driver 缓存、`/episodes`（含幂等前置）、`/search`、`/stats`、`/health`、`/graph/{group_id}` |
| `server/graph-bridge/requirements.txt`、`Dockerfile`、`README.md`（新增） | 图桥 | 依赖固定 `graphiti-core[falkordb]==0.30.2`；镜像基于 `python:3.12-slim` |
| `server/docker-compose.yaml` | 部署 | 新增 `falkordb`（`mem_limit: 384m`，仅内网）与 `graph-bridge`（`mem_limit: 512m`，仅内网，`depends_on` falkordb）；`mem0` 服务透传 `MEM0_GRAPH_*` 并 `depends_on` graph-bridge（软依赖） |
| `server/.env.example` | 部署 | 新增图能力环境变量（默认 `MEM0_GRAPH_ENABLED=false`）。**实际做法**：`server/.env` 未新增变量——它未纳入版本控制（`.gitignore`），运行行为由 compose 的 `${MEM0_GRAPH_ENABLED:-false}` 等默认值兜底；`.env.example` 是模板与文档面 |
| `server/routers/graph.py`（新增） | REST | `GET /graph/stats`：图派发计数与熔断状态的只读端点（`[AC-9]`/`[AC-25]` 的判定面，§8） |
| `mem0/memory/graph_sync.py`（新增） | 同步 | 作用域→图键纯函数、有界队列、单 worker（线程 + 事件循环）、HTTP 客户端、重试与熔断、计数器、图分支状态值 |
| `mem0/configs/base.py` | 配置 | `MemoryConfig` 新增 `graph` 配置段与其默认值 |
| `mem0/utils/scoring.py` | 打分 | `score_and_rank` 接受 `graph_boosts`；`max_possible` 条件增长；`score_details` 输出版本分量 |
| `mem0/memory/main.py` | 逻辑 | 写入路径两份实现的收尾派发；`_search_vector_store` 两份实现（sync/async）调用图检索并折算加分、传入打分器，并把 `graph_status` 发布到每条结果 |
| `server/main.py` | REST | `DEFAULT_CONFIG` 增加 `graph` 段（读环境变量）；`score_details` 透传新键；挂载图观测路由 |
| `tests/memory/test_graph_sync.py`（新增） | 测试 | 图键派生、队列容量与丢弃、重试与熔断、幂等前置判据、降级与状态值 |
| `tests/memory/test_graph_search.py`（新增） | 测试 | 图加分折算、失效事实过滤、候选池外 id 忽略、关闭态一致，以及打分器的新增参数与分母条件增长的断言（§6.2）。**实际做法**：打分断言落在本文件，`tests/utils/test_scoring.py` 本阶段零改动——落点与既有的打分器用例（`tests/utils/test_scoring.py` 只覆盖通用打分）同层，但与本文原先的清单不一致 |
| `tests/test_graph_router.py`（新增） | 测试 | 图计数器只读端点：读数透传、关闭态/未派发态零读数、读数不创建派发器 |
| `docs/design/graph-memory-evidence/bridge_search_latency.{py,txt}`（新增） | 证据 | 图桥 `/search` 尾部分位实测的脚本与原始输出（§10 E17） |

接口契约保持：`/search`、`/memories`、`/memories/{id}`、`/memories/export` 的请求参数与既有响应字段全部保持；`explain` 只增键不改键。Qdrant 集合、payload schema、既有索引不变（join key 在图上，不需要新增 payload 字段）。

---

# 8. 可观测性

| 观测项 | 方式 | 期望 |
| --- | --- | --- |
| 派发与丢弃 | `GET /graph/stats`（mem0 侧的只读端点，5 项计数 + 熔断状态 + 队列长度） | 常态 `dispatched ≈ synced`；图不可用时 `dropped` 上升，`synced` 停增 |
| 图分支状态 | `POST /search` 每条结果的 `graph_status` | 取 `ok` / `skipped` / `timeout` / `error` / `disabled`；`timeout` 与 `error` 的比例是「图没答上来」的量化面 |
| 熔断状态 | `GET /graph/stats` 的 `circuit_open` / `consecutive_failures` + 日志（进入/退出冷却） | 图不可用 60s 内进入冷却，恢复后自动放行 |
| 图规模 | 图桥 `GET /stats?group_id=` | `episodes` 单调上升并与新增事实数同阶；`entity_nodes` / `entity_edges` 随之增长 |
| 图检索时延与命中 | 图桥访问日志（时延 + `max_facts` 命中数）+ 检索侧计时 + `GET /graph/stats` 的 `timeout_seconds` | 单次 ≤ 超时预算（默认 1.0s，§10 E17）；命中数 > 0 的比例随时间上升 |
| 图信号参与度 | `POST /search?explain=true` 的 `score_details` | 含 `graph_boost` / `graph_facts`；图命中为 0 时 `graph_boost = 0.0` |
| 资源 | `docker stats` + VM `free -m` | 图桥 ≤ 512MiB、FalkorDB ≤ 384MiB；VM `available` ≥ 4GiB；swap 保持 0 |
| 图键隔离 | 图桥 `GET /graphs` / FalkorDB `INFO memory` | 每个作用域一个图键；隔离 `user_id` 的键可单独删除 |

---

# 9. 与上游的冲突面

| 文件 | 本地既有改动 | 本方案新增改动 | 冲突风险 |
| --- | --- | --- | --- |
| `mem0/memory/main.py` | 既有改动（bi-temporal、衰减、中文提示词） | 派发调用、图检索分支、两份实现 | **高**（上游 V3 管道核心） |
| `mem0/utils/scoring.py` | 衰减分量 | 图分量与分母条件增长 | 中 |
| `mem0/configs/base.py` | 衰减配置段 | 图配置段 | 低 |
| `server/main.py` | 分页、独立 provider、导出、衰减配置 | 图配置透传与 `score_details` | 低 |
| `server/docker-compose.yaml` | 衰减开关透传 | 两个新服务与图环境变量 | 低 |
| `server/graph-bridge/` | 无 | 全新目录 | 无（上游无对应文件） |
| `tests/` | 衰减测试 | 图同步与图检索测试 | 低 |

图侧代码全部落在新目录与被改文件的新增段落中；graphiti-core 以 PyPI 依赖引入并锁版本，仓库内不含其源码副本。实现完成后重跑 `bash patches/generate-patch.sh` 刷新 `patches/mem0-local.patch`。

---

# 10. 实测证据

全部数据采集于 2026-09-17，主机 macOS（vz），VM `docker`（aarch64，2 vCPU / 7922MiB），mem0 四服务在线，Qdrant 集合 `memories_2048`（3612 点）。探针脚本与原始输出留存于 `docs/design/graph-memory-evidence/`（`graphiti_e2e.py` / `llm_probe.py` / `joinkey_probe.py` / `falkor_scale.py` / `bridge_search_latency.py` 及对应 `.txt` 原始输出、`vm_resource.txt`、`official_image_probe.txt`），可按脚本头部注释复跑。

| 编号 | 证据 | 结论 |
| --- | --- | --- |
| E1 | `free -m`：7922 总 / 981 used / 6941 available，swap 0；`df -h`：61G 总 / 20G 用 / 42G 余 | 3.1 基线；第 3.3 节余量计算输入 |
| E2 | `docker stats --no-stream`：mem0 278.4MiB、qdrant 247.0MiB、dashboard 106.5MiB、postgres 67.0MiB | 现有常驻合计 ≈ 699MiB |
| E3 | `docker stats`：`falkordb/falkordb:latest` 空载 62.43 → 67.5MiB（分步启动观测） | 图库常驻预算 |
| E4 | 规模化压测：8150 实体节点 + 3545 边写入后 `INFO memory`：`used_memory` 7.00MiB → 10.53MiB、`used_memory_dataset` 8.25MiB、`used_memory_rss` 37.46MiB 不变；`count(n)=8150`、`count(e)=3545` | 目标规模图数据的内存模型（3.2/4.3） |
| E5 | `docker stats`：`zepai/graphiti:0.30.2` 容器 idle 145.3MiB、CPU 0.23% | 图服务常驻与 CPU 预算 |
| E6 | 端到端（LLM 走 `/responses` 通道，即 E7 当时选定的形态；该通道后因上游 provider 路由变化而失效，见 E18）：5 条中文事实 → 11 实体 + 8 边；摄入时延 min/med/max = 14.24 / 20.52 / 36.65s；进程峰值 RSS 155MB。**`/search` 时延以 E17 的专门采样为准**——本条当时记录的「0.12–0.23s」只覆盖了轻载样本、未覆盖尾部，核验复跑 40 次即得 `p95 0.520 / max 0.678s`（超 0.4s 3/40），故 0.4s 预算不成立 | 时延预算、LLM 负载 |
| E7 | 结构化输出探测（`graphiti-core` 三种客户端 × 本机网关 `deepseek-v4.1-flash`）：`OpenAIGenericClient(json_schema)` → 422 `This response_format type is unavailable now`；`OpenAIGenericClient(json_object)` 短提示通过、长提示下出现 `EdgeDuplicate` 校验失败（`Extra data: line 1 column 847`）；`OpenAIClient(responses.parse)` → 通过 | 图桥按 `/responses` 通道装配 LLM 客户端。**该结论已被 E18 推翻**：当时成立的是「三条通道里 `/responses` 是唯一长提示可用者」，而非 `/responses` 通道本身可靠 |
| E8 | 官方镜像内 `import falkordb` → `None`（不存在），`neo4j` → 存在；带 `db_backend=falkordb` 启动 → `ImportError: falkordb is required for FalkorDriver` | G1 |
| E9 | 官方镜像 + Neo4j 后端：`/healthcheck` → `{"status":"healthy"}`；`/messages` → 202；`/search` → `422 ... API密钥未配置该模型`（embedder 实际用默认 `text-embedding-3-small`） | G2 |
| E10 | 镜像内 `graph_service` 源码：`AddMessagesRequest` 无 uuid 回传字段；`/messages` 响应仅 `{"message", "success"}` | G3 |
| E11 | join key 实测：预建 `EpisodicNode(uuid=<memory_id>)` → `add_episode(uuid=<同一 id>)` → 返回 episode uuid 与 memory id 相同；`/search` 命中 5 条事实，`episodes` 均为该 memory id；库内 4 条边同 | 5.4 构造性映射成立 |
| E12 | 幂等实测：同 uuid 重放 `add_episode` 成功（不报错），实体节点 5、关系边 4 → 6 | 5.3 幂等判定必须前置；`size(e.entity_edges)` 可作已同步判据（实测 = 3） |
| E13 | 图键机制：driver 指向 `default_db` 而 `group_id` 为其它值时，预建 episode 在 `add_episode` 内读不到（`NodeNotFoundError`）；把 driver 的 database 设为同一 `group_id` 后通过 | 4.4/5.4 的图键约定 |
| E14 | Neo4j 对照：`neo4j:5.26-community` 空载 328MiB（启动 33s）→ 507.1MiB；`dbms.listConfig()`：`pagecache.size = 512.00MiB` | 4.3 对照数据 |
| E15 | 镜像体积：本机展开 `falkordb/falkordb:latest` 570MB、`zepai/graphiti:0.30.2` 582MB、`neo4j:5.26-community` 660MB；Docker Hub arm64 拉取体积 FalkorDB 206.7MB、Graphiti 192.0MB | 3.2 磁盘预算 |
| E16 | `graphiti_core` 版本：PyPI 最新 `graphiti-core` 0.30.2（requires_python `>=3.10,<4`），镜像内为同一版本；`Graphiti` 构造器接受 `graph_driver` / `llm_client` / `embedder` / `cross_encoder` | 依赖锁版本与客户端装配方式 |
| E17 | 图桥 `/search` 尾部分位（核验整改卡实测，`bridge_search_latency.py`）：第 1 轮 n=60 → `p50 0.131 / p90 0.258 / p95 0.343 / p99 0.425 / max 0.472s`；第 2 轮（图键补入实体与关系边）n=100 → `p50 0.163 / p90 0.445 / p95 0.514 / p99 0.812 / max 0.842s`，其中超 0.4s 共 14/100（14.0%），超 0.6s 4/100，超 0.8s 2/100，无超 1.0s | `timeout_seconds` 默认值取 1.0 的依据：两轮 max 均在预算内，且对 p99/max 仍有 ~19% 裕度；0.4s 只覆盖到约 p85，属「正常请求会落进超时区」 |
| E18 | 通道重测（上游 provider 路由变更后）：`OpenAIClient(responses.parse)` → 网关 422 `所有模型提供商均请求失败: ... 400 Bad Request from POST https://api.aimindsky.com/v1/responses，The request failed because it is missing messages parameter`（表现为 `/episodes` 500、`graph_synced` 恒为 0）；改 `OpenAIGenericClient(json_schema)` → 422 `This response_format type is unavailable now`；改 `OpenAIGenericClient(json_object)` → 通过（单条隔离事实 `/episodes` 200 `synced`，9.5s；端到端 `graph_synced` 7→8 且 `graph_failed` 无新增，图键 1 episode / 3 实体 / 2 边） | 图桥按 `OpenAIGenericClient(structured_output_mode="json_object")` 装配；E7 的通道结论作废。E7 记录的 `json_object` 长提示 `EdgeDuplicate` 校验失败在本轮端到端（含长抽取提示）未复现 |

---

# 11. 验收标准

判定一律以**实现后的实际执行输出**为准，与具体 commit 对照。端到端验证统一使用隔离 `user_id`（`test_graph_*` 前缀）与由此派生的独立图键，验证后删除图键并清理 Qdrant 测试点。

## 11.1 图服务与资源

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-1] | 图桥与图库作为两个独立容器运行，且不发布宿主端口：`docker compose ps` 显示两者 `running`；`docker port` 对两容器输出为空；`mem0` 容器内 `curl http://graph-bridge:8000/health` 返回 200 且 `backend` 字段为 `falkordb` | 部署后容器命令输出 |
| [AC-2] | 资源占用落在评估区间内：连续 30 分钟每 5 分钟采样 `docker stats --no-stream`，图桥峰值 ≤ 512MiB、FalkorDB 峰值 ≤ 384MiB；VM `free -m` 的 `available` ≥ 4096MiB；`swap` 总量保持 0 | 采样日志与评估区间（第 3.3 节）对照 |
| [AC-3] | 图库内存随规模线性且 ≤ 评估上限：写入 ≥ 3000 条事实对应的图数据后，FalkorDB `INFO memory` 的 `used_memory_dataset` ≤ 100MiB | 直接查 FalkorDB `INFO memory` |
| [AC-4] | 分步启动可观测：先启 `falkordb` 记录一次 `docker stats` 与 `free -m`，再启 `graph-bridge` 记录一次；两次记录的增量与第 3.2 节的单组件预算（68MiB / 145MiB）同量级（偏差 ≤ 2 倍） | 两次采样输出 |

## 11.2 写入同步

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-5] | 新事实入图并可追回：隔离 `user_id` 写入 1 条事实 → 2 分钟内该图键 `/stats` 的 `episodes` ≥ 1，且图检索能查到该事实，其 `episodes` 含该 memory id | 端到端写入 + 图桥 `/search` |
| [AC-6] | 入图不阻塞主写入：图桥正常运行与停止两种状态下，各连续 5 次 `POST /memories`（每次 1 条事实），两次的响应时延 p50 相对差 ≤ 50%（写入本身含多次 LLM 调用，判定看派发是否引入量级差异而非微秒级差异），且两种状态下事实均写入成功 | 两次计时输出对比 |
| [AC-7] | 幂等：同一 memory id 触发两次同步，图内该 uuid 的 Episodic 节点恰好 1 个；第二次调用返回 `already_synced`；该 uuid 关联的边数与首次完成后相比不增加 | 图键内查询 + 图桥响应字段 |
| [AC-8] | 失败可重试：停止图桥 30s，期间写入 3 条事实；恢复图桥后 3 分钟内 3 条事实全部可在图中查到 | 停止/恢复操作 + `/stats` 与图检索 |
| [AC-9] | 队列有界且可观测：把 `queue_size` 设为 5 并持续阻断图桥，写入 20 条事实 → 进程 RSS 增量 < 50MiB，且 `graph_dropped` 计数器 > 0；恢复后可查到的 episode 数 ≤ 5 | `GET /graph/stats` 的 `graph_dropped` 读数（服务侧只读端点，§8、§7）+ 图桥 `/stats` |
| [AC-10] | 存量不受影响：图内 episode 数等于实施后新增事实数（与 3500+ 存量无关）；Qdrant `points_count` 只按期间新增量增长；`GET /memories?limit=1` 的 `total` 不回退 | Qdrant 与 REST 计数对比 |

## 11.3 检索融合

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-11] | 图信号真实参与打分：隔离 `user_id` 构造两条近似候选（B 被图检索命中、A 未被命中）→ 开启图能力后 B 的 `final_score` 高于关闭态同名条目的 `final_score`，且 `explain` 中 B 的 `graph_boost > 0`、A 的 `graph_boost = 0.0` | 两态各跑一次 `POST /search?explain=true` 逐条比对 |
| [AC-12] | 图信号有上限：任一候选的 `graph_boost ≤ weight`（默认 0.5）；且 `graph_boost / max_possible_score ≤ W_g / (1.0 + has_bm25 + has_entity + W_g)`——**上界随实际启用的信号数变化**，四信号全开（分母 3.0）时收为 0.167，只启用图信号时（分母 1.5）为 0.333。判据把前提写进公式，不再是「无条件 ≤ 0.167」 | 读取响应 JSON 的 `score_details`，代入选定的 `W_g` 与本次分母 |
| [AC-13] | 不引入、不淘汰候选：`top_k` 取等于候选池规模时，开启与关闭图能力两种状态下返回的 id **集合完全相同**，仅顺序可能不同；开启态不存在 `threshold` 以下的候选进入结果 | 两态对比 id 集合 |
| [AC-14] | 保序判据成立：对真实语料 20 条 query × `top_k=50` 扫描，不存在 `s_A ≥ (s_B + 0.5)/0.90` 却被翻转的候选对（A 在前） | 用 `explain` 的 `semantic_score` 全对扫描 |
| [AC-15] | 与时间因子复合正确：开启两机制时 `explain` 同时含 `graph_boost` 与 `decay_weight` / `retention` / `memory_strength_days` / `elapsed_days` / `access_count`；单独关闭其中之一，另一机制的分量与行为不变 | 三种开关组合各跑一次比对 |
| [AC-16] | 失效事实默认不采纳：在图侧把某条边标为 `invalid_at` 非空后，该边对应的记忆不再获得 `graph_boost`；把 `include_invalidated` 置为 true 后重新获得 | 图侧改边 + 两态检索 |
| [AC-17] | 候选池外 id 被忽略：图检索命中的事实关联到不属于本次候选池（或不属于该 `user_id`）的记忆时，该记忆不获得加分、`max_possible` 不因此增长 | 隔离双 `user_id` 构造 + `explain` 比对 |
| [AC-18] | 存量记忆可获得图信号：一条存量事实与新写入事实共享实体时，图检索命中带来该存量记忆的 `graph_boost > 0` | 端到端 + `explain` |
| [AC-19] | 关闭态逐位一致：`enabled=false` 时对 20 条固定 query × `top_k=50`，返回的 (id 序 + score) 与实现前基线逐条相等，`score_details` 不含图分量键 | 基线对比脚本 |

## 11.4 降级路径

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-20] | 图桥停止：`POST /memories` 与 `POST /search` 均返回 2xx；检索结果的 id 序与分数与关闭态逐条相等，且每条结果的 `graph_status` 标为 `error`（图桥不可达）或 `timeout`（未在预算内返回）；被跳过的派发计入 `graph_dropped` | 停容器 + 两态比对 + `GET /graph/stats` 读数 |
| [AC-21] | 图库停止（图桥在）：行为同 [AC-20]，结果与关闭态逐条相等 | 停 FalkorDB + 两态比对 |
| [AC-22] | 超时预算生效：把 `timeout_seconds` 设为 0.001 后连续 10 次 `POST /search`，响应时延 p95 与关闭态差 ≤ 5%，结果与关闭态一致 | 两态计时与结果比对 |
| [AC-23] | 畸形数据不致命：图桥返回空列表、缺字段、伪造 memory id 三种响应时，`POST /search` 返回 2xx、无 5xx、结果与关闭态一致（图分量按 0 计） | 注入响应 + 结果比对 |
| [AC-24] | 失败不回滚事实：图桥全阻断期间写入 5 条事实，解除阻断后 5 条在 Qdrant 中均可检索到（`GET /memories/{id}` 命中） | 写入 + 读取 |
| [AC-25] | 熔断生效并可恢复：连续 5 次失败后进入冷却，冷却窗口内的派发被记为丢弃（不发起新请求，图桥访问日志 0 次）；冷却结束后自动放行（访问日志恢复增长） | `GET /graph/stats` 的 `circuit_open` / `consecutive_failures` / `graph_dropped` 读数 + 图桥访问日志 |

## 11.5 开关、接口与范围

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-26] | 关闭时零图调用：`enabled=false` 下连续 20 次写入与 10 次检索，图桥访问日志计数与关闭前相同（增量为 0） | 图桥访问日志 |
| [AC-27] | 开关可运行时切换：以配置通道切换 关→开→关，检索行为随之变化，无需重启容器 | 三次 `POST /search` + `GET /configure` 复核 |
| [AC-28] | 存储面不变：变更前后 Qdrant 集合的 `payload_schema` 键集合相同；不存在新增集合；无新增 payload 字段 | 直接查 Qdrant REST |
| [AC-29] | 既有资产可用：衰减的验收用例（衰减函数、足迹写入、开关回退、存量兼容）复跑通过；bi-temporal 的 `as_of` / `include_invalidated` 行为保持一致 | 复跑既有用例 |
| [AC-30] | 接口契约不变：`GET /memories` 顺序仍为 `created_at` 降序且 `total` 与基线一致；`GET /memories/{id}` 与导出的字段集合与基线相同（`explain` 之外的响应不含新键） | 端点对比 |
| [AC-31] | `explain` 只增键：`score_details` 含 `graph_boost` / `graph_facts`，且既有键（`semantic_score` / `bm25_score` / `entity_boost` / `raw_score` / `max_possible_score` / `final_score` / `threshold`）取值语义不变 | 读取响应 JSON 的键集合与取值 |

## 11.6 验证纪律与工程

| 编号 | 验收内容 |
| --- | --- |
| [AC-32] | 全部端到端验证使用隔离 `user_id`（`test_graph_*`）及其派生图键；验证完毕后删除图键（`DELETE /graph/{group_id}`）并清理 Qdrant 测试点，`GET /memories?limit=1` 的 `total` 回到基线值 |
| [AC-33] | 存量 3500+ 条真实记忆全程无删除、无重建、无向量重算；`user_id=xue` 的 `points_count` 单调不减 |
| [AC-34] | 图信号的一致性分两条判定：**(a) 健康路径**——图服务可用且两次调用都未超时（结果 `graph_status` 均为 `ok`）时，同一 query 两次 `explain` 调用的 `graph_boost` 逐条相等（折算为纯函数，不依赖 LLM 采样）；**(b) 降级路径**——发生超时（或故障）时，响应必须把该次标为 `graph_status = "timeout"`（`error`），即「不一致」只允许出现在被显式标注的那一类里，不允许静默。判据的「一致」是「健康路径确定 + 降级路径显式」，不是「任何情况下都一致」——显式降级下两次取值必然不同，那是设计而非缺陷 | 两态响应比对 + `graph_status` 取值；预算取 §6.5 的默认值（1.0s，实测尾部见 E17） |
| [AC-35] | `make lint`（ruff，line length 120）对 `mem0/`、`server/` 改动零告警；`docker exec mem0-dev-mem0-1 python -c "from mem0.memory.graph_sync import <新符号>"` 成功 |
| [AC-36] | 实现完成后 `bash patches/generate-patch.sh` 成功刷新 `patches/mem0-local.patch`，覆盖本次全部改动文件 |
| [AC-37] | 图桥不修改 graphiti-core 源码：镜像内 `graphiti_core` 与 PyPI 0.30.2 的文件清单一致（无本地补丁文件） |
| [AC-38] | VM 规格与评估结论一致：`limactl list` 显示 2 CPU / 8GiB，与第 3.3 节结论相同（未发生扩容） |

---

# 12. 实现卡拆分建议

链路为「图桥服务 → SDK 同步 → SDK 融合 → 服务层配置」，两块可独立开发并在同一张实现卡内顺序推进：

| 分块 | 内容 | 依赖 | 独立验收面 |
| --- | --- | --- | --- |
| A 图桥与部署 | `server/graph-bridge/`、`server/docker-compose.yaml`、`server/.env`、FalkorDB 载体 | 无（先行） | [AC-1]–[AC-4]、[AC-5]（图侧）、[AC-7]、[AC-8] |
| B SDK 同步与融合 | `graph_sync.py`、`configs/base.py`、`scoring.py`、`memory/main.py`、`server/main.py`、`tests/` | A 的接口契约（`/episodes`、`/search` 请求响应形态） | [AC-6]、[AC-9]–[AC-31] |

合并顺序建议：A 完成后其接口形态冻结，B 可并行开发；端到端验收（11.2–11.5）要求两者同时就位。[AC-32]–[AC-38] 为整体纪律项，随实现卡收口核验。核验卡在实现卡之后独立复跑全量 [AC-n]。

后续独立卡（本阶段范围外，可另立）：

| 卡 | 内容 | 触发条件 |
| --- | --- | --- |
| C 存量回填 | 图桥 `POST /backfill` 的限速、续跑与进度可观测；按 Qdrant 游标顺序回填 3500+ 条 | 图覆盖度不足以支撑检索增益的观测结论 |
| D 图侧运维 | 图键清单、规模趋势、按作用域清理的例行任务 | 图键数量增长到需要例行巡检时 |

---

# 13. 附录

## 附录 A：术语表

| 术语 | 含义 |
| --- | --- |
| episode | Graphiti 的一次数据摄入单元，本身是图中的一个节点，抽取出的实体通过 `MENTIONS` 边连到它 |
| 图键（group_id） | 本方案中的作用域分区键；FalkorDB 后端下即图（Redis key）名，由作用域派生 |
| 图桥（graph-bridge） | 持有 graphiti-core 与图库连接的独立服务，对 mem0 暴露 `/episodes`、`/search` 等端点 |
| join key | `episode uuid == memory id` 的构造性映射，使图检索结果直接携带 memory id |
| `graph_boost` | 图检索命中折算出的补充加分，取值 `[0, W_g]` |
| 保序判据 | `s_A ≥ (s_B + W_g)/FLOOR` 时两机制联合作用下 A 恒在前 |

## 附录 B：参考文献

| 来源 | 用途 |
| --- | --- |
| Graphiti（Apache-2.0，getzep/graphiti，v0.30.2）：`graphiti_core/graphiti.py::add_episode`、`edges.py::EntityEdge.episodes`、`driver/falkordb_driver.py` | 接口形态、join key 可行性、图键机制、后端支持 |
| Graphiti 官方 FalkorDB 配置页（help.getzep.com/graphiti） | `graphiti-core[falkordb]` 安装形态与连接参数 |
| Neo4j 5.26 官方文档（`server.memory.pagecache.size`、最低内存要求） | 4.3 对照维度 |
| Kuzu 项目状态公告（`kuzudb/kuzu` README、`kuzudb.com` 解析状态） | 4.3 载体可持续性 |
| mem0 v3 云平台图记忆说明（`docs`：无外部图库、实体节点与共享实体连接、排序阶段提分） | 本方案的融合位置与「补充信号」定位 |
| `FalkorDB/mem0-falkordb`（provider `falkordb`）与 `Nerfherder16/System-Recall`（Mem0 + Neo4j + Qdrant + Graphiti 自托管，README 建议 8GB+ RAM） | 生态一致性与资源量级对照 |
| 衰减设计 `docs/design/memory-decay.md` | 时间因子定义、复合顺序、开关与回退形态 |
