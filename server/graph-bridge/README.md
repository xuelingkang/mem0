# graph-bridge：mem0 的旁路图服务

设计见 [`docs/design/graph-memory.md`](../../docs/design/graph-memory.md) §4。本服务是
`graphiti-core` 的薄壳，向 mem0 暴露事实入图与图检索两个能力；它**不修改 graphiti-core
源码**，图库选 FalkorDB（Redis 协议内存型图库，目标规模数据集 8.25MiB / 常驻 68MiB）。

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/episodes` | 入图一条事实。请求 `{uuid, group_id, text, reference_time, source_description}`；同 `uuid` 重复调用返回 `already_synced` 且不重复抽取 |
| POST | `/search` | 图检索。请求 `{group_ids, query, max_facts}`；响应 `{facts:[{uuid,name,fact,valid_at,invalid_at,episodes}]}`，`episodes` 即 memory id |
| GET | `/stats?group_id=` | 图规模：`episodes` / `entity_nodes` / `entity_edges` |
| GET | `/health` | 存活 + `backend=falkordb` + 图库连通性 |
| GET | `/graphs` | 图键清单 |
| DELETE | `/graph/{group_id}` | 整体删除一个图键（隔离验证数据的清理面） |
| POST | `/backfill` | 存量回填：按 Qdrant 游标顺序取事实、限速入图、可中断续跑（见下节） |
| GET | `/backfill/status?group_id=` | 回填进度读数（进程内快照，只读） |

join key 是构造性的：`episode uuid == memory id`，图侧不另存映射表。

## 存量回填（`POST /backfill`）

设计见 [`docs/design/graph-memory.md`](../../docs/design/graph-memory.md) §5.5。背景：增量路径只覆盖
新写入，存量覆盖率 2.8%（116 episodes / 4105 条），图检索对历史查询等于无效。

```bash
# 一批：最多新入图 5 条，限速 6 条/分钟，扫过最多 500 条记录
curl -s -X POST http://graph-bridge:8000/backfill -H 'content-type: application/json' \
  -d '{"group_id":"mem0_<scope>","limit":5,"rate":6,"scan_limit":500}'
# 续跑：把上一次响应的 next_cursor 传回 cursor
curl -s -X POST http://graph-bridge:8000/backfill -H 'content-type: application/json' \
  -d '{"group_id":"mem0_<scope>","limit":5,"rate":6,"cursor":"<next_cursor>"}'
# 进度
curl -s 'http://graph-bridge:8000/backfill/status?group_id=mem0_<scope>'
# 完成判定：processed == 0 且 exhausted == true
```

| 参数 | 默认 | 语义 |
| --- | --- | --- |
| `group_id` | 必填 | 图键（`mem0_<scope>`）；非该形态回 400 且不建键 |
| `limit` | 20 | 本次最多**新入图**条数（已同步的跳过不计数） |
| `rate` | 0 | 入图速率上限「条/分钟」；`0` = 不限速 |
| `cursor` | 无 | 续跑锚点（`created_at` 原值）；缺省从最新一条起扫 |
| `scan_limit` | 200 | 本次允许扫过的记录条数上限（防全已同步时无界扫描） |

四条语义：**游标顺序取事实**（`created_at` 降序 + `created_at < cursor`，与 mem0 侧
`vector_store.list()` 同一口径）；**限速**（相邻两次入图起始间隔 ≥ `60/rate` 秒，读数见响应的
`paced_wait_seconds`）；**可中断续跑**（复用 `/episodes` 的 `already_synced` 前置判定，同一
`_sync_one` 实现，不另存进度表）；**进度可观测**（响应计数 + `/backfill/status`）。

两个上界决定了它不会「一次调用跑到完」：`limit`（新入图条数）与 `scan_limit`（扫描条数），
由编排者分批推进。运行期间全服务只允许一个回填（否则 409）：回填与图派发共用同一 LLM 通道与
2 vCPU，限速存在的理由就是别把主链路压住。

`memory_kind == "observation"` 的 Dream 观察条目不入图（与增量派发口径一致）。单条入图耗时由
LLM 抽取主导（实测中位 20.5s），故全量 4105 条 ≈ 20+ 小时串行 LLM 占用。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FALKORDB_HOST` / `FALKORDB_PORT` | `falkordb` / `6379` | 图库地址 |
| `QDRANT_HOST` / `QDRANT_PORT` | `qdrant` / `6333` | 事实主存地址（回填读取用；与 mem0 侧 `QDRANT_*` 同源） |
| `QDRANT_COLLECTION_NAME` | `memories` | 事实主存集合名（本部署为 `memories_2048`） |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 空 / 空 / `gpt-5-mini` | 抽取管道使用的 LLM（走 OpenAI 兼容的 `/chat/completions` 结构化输出通道） |
| `EMBEDDER_API_KEY` / `EMBEDDER_BASE_URL` / `EMBEDDER_MODEL` / `EMBEDDER_DIMS` | 回落 `LLM_*` / `text-embedding-3-small` / `1024` | 事实向量化用的 embedder |
| `SEMAPHORE_LIMIT` | `1` | 并发上限，默认串行入图 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 依赖钉版（不要放宽）

`requirements.txt` 里 `httpx` 与 `openai<3` 是**必须显式写死**的，不是冗余声明：

- `graphiti-core 0.30.2` 的 `llm_client/client.py` 直接 `import httpx`，并用 `httpx.HTTPStatusError`
  判定 5xx 重试与限流，但它**没有把 httpx 声明为依赖**——这份依赖此前只是从 `openai` 传递而来。
- `openai 3.x` 改用 `httpx2`，传递依赖随之消失。实测：不钉版时镜像构建「成功」，容器也「在跑」，
  但 `import httpx` 在运行期抛 `ModuleNotFoundError`，表现为 `/health` 与 `/episodes` 一律 500。
- 钉住 `openai<3` 的第二个理由是语义一致：graphiti 用 `isinstance(exc, httpx.HTTPStatusError)`
  做判定，而 `openai 3.x` 抛的是 `httpx2` 的异常类，两者不同源，5xx 重试与限流翻译会静默失效。

验证方式（构建后必跑）：

```bash
docker exec mem0-dev-graph-bridge-1 python -c "import httpx, graphiti_core; print('imports ok')"
curl -s localhost:8000/health   # 期望 {"status":"healthy","backend":"falkordb","falkordb":"ok"}
```

## 本地验证

```bash
cd server && docker compose up -d falkordb && docker compose up -d --build graph-bridge
curl -s localhost:8000/health            # 容器内：curl http://graph-bridge:8000/health
curl -s -X POST localhost:8000/episodes -H 'content-type: application/json' \
  -d '{"uuid":"<uuid4>","group_id":"mem0_test_graph_probe","text":"验证用事实","reference_time":"2026-09-17T00:00:00+00:00"}'
curl -s -X POST localhost:8000/search -H 'content-type: application/json' \
  -d '{"group_ids":["mem0_test_graph_probe"],"query":"验证用事实","max_facts":5}'
curl -s "localhost:8000/stats?group_id=mem0_test_graph_probe"
curl -s -X DELETE localhost:8000/graph/mem0_test_graph_probe
```

一次真实入图需要多次 LLM 调用（实测单条中文事实 14.24–36.65s），因此 `/episodes` 的
超时预算应按分钟级设置（mem0 侧代码缺省 `MEM0_GRAPH_REQUEST_TIMEOUT_SECONDS=120`）。
核验实测出现过单次 `Completed add_episode in 101031.1 ms`（101.0s），与 120s 的裕度仅约
16%，故本部署的模板值取 240s（≈2.4 倍实测峰值，见 `.env.example` 与方案 §6.5）。
