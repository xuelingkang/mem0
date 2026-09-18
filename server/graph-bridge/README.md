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

join key 是构造性的：`episode uuid == memory id`，图侧不另存映射表。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FALKORDB_HOST` / `FALKORDB_PORT` | `falkordb` / `6379` | 图库地址 |
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
