# graph-memory 实测证据

本目录是 `docs/design/graph-memory.md` 第 10 节「实测证据」的原始材料，采集于 2026-09-17，
环境：macOS 宿主（vz）+ lima `docker` VM（aarch64，2 vCPU / 7922MiB），mem0 四服务在线。

## 脚本

| 文件 | 作用 | 复跑方式 |
| --- | --- | --- |
| `llm_probe.py` | 探测本机 LLM 网关对 graphiti-core 三种结构化输出客户端的兼容性 | 需 `graphiti-core` 可用环境；读 `server/.env` 的 `LLM_API_KEY` / `LLM_BASE_URL` |
| `graphiti_e2e.py` | 端到端：FalkorDB 容器 + 本机 LLM 与 embedder，写 5 条中文事实、检索、导出图统计 | 需 FalkorDB 可达（脚本内置 `127.0.0.1:16379`，按环境调整） |
| `joinkey_probe.py` | join key 与幂等：预建 `EpisodicNode(uuid=memory_id)` → `add_episode(uuid=同)` → 检索边 `episodes` 校验 → 重放表现 | 同上；脚本内 `driver.database` 必须与 `group_id` 相同 |
| `falkor_scale.py` | 图库规模内存模型：直连 Redis 协议压 8150 实体 + 3545 边，读 `INFO memory` | 需 FalkorDB 可达；不调用 LLM |
| `bridge_search_latency.py` | 图桥 `/search` 尾部分位：自建探针图键（实体 + 关系边）后连续采样，给 `timeout_seconds` 选值 | 在 mem0 容器内跑（图桥不发布宿主端口）：`docker cp` 后 `docker exec mem0-dev-mem0-1 python /tmp/bridge_search_latency.py 100`；跑完 `DELETE /graph/test_graph_rect_lat` |
| `backfill_probe.py` | 存量回填（§5.5）：`seed`（隔离作用域直写 Qdrant 26 条）/ `run`（一次 `POST /backfill` + 前后图规模对账）/ `status` / `graph` / `concurrency`（回填在飞时打主链路）/ `cleanup` | 在 mem0 容器内跑（同上）：`docker cp` 后 `docker exec -e MEM0_API_KEY=<key> -w /tmp mem0-dev-mem0-1 python backfill_probe.py <stage> --scope bfprobe_<ts>`；逐级执行，最后 `cleanup` |

脚本仅写探针图键（`gm-*`）与探针作用域（`bfprobe_*` / `test_*`），不触碰 mem0 的线上记忆；
`backfill_probe.py` 的 `seed` 会向 Qdrant 写隔离作用域的点（随机向量，非真实 embedding，不参与检索），
其 `cleanup` 子命令按作用域全额清除（点 + 图键），跑完必须执行。

## 原始输出

| 文件 | 对应证据 |
| --- | --- |
| `llm_probe.txt` | E7（结构化输出通道） |
| `e2e_responses.txt` | E6（端到端时延、实体/边规模、检索时延） |
| `joinkey_probe.txt` | E11（join key）、E12（幂等）、E13（图键机制） |
| `falkor_scale.txt` | E4（目标规模内存模型） |
| `bridge_search_latency.txt` | E6（`/search` 时延的修订口径）、E17（尾部分位与预算选值） |
| `backfill_probe.txt` | §5.5 存量回填（幂等续跑、限速对照、中断续跑、进度可观测、主链路并发；末段为 run 31 复测：同批量 rate 对照、20 条小样本、终态耗时冻结、单条 LLM 调用量级、重建后回填在飞时的主链路并发） |
| `vm_resource.txt` | E1–E3、E5、E14–E15（VM 与容器资源、Neo4j 对照、镜像体积） |
| `official_image_probe.txt` | E8–E10（官方镜像三处缺口 G1–G3） |

`E16`（graphiti-core 版本与构造器签名）来自 PyPI JSON API、GitHub release 与 clone 源码核对，
无独立日志文件。
