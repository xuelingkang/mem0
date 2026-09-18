# dashboard-mechanism-visibility 实测证据

本目录是 `docs/design/dashboard-mechanism-visibility.md` 第 10 节「实测证据」的原始材料，采集于
2026-09-18，基线 commit `737d10e0`（`upstream/main` = `0df3e4b8`）。

运行环境：macOS 宿主 + lima `docker` VM；compose 五服务在线（mem0 `:8888`、dashboard `:3000`、
postgres、qdrant、graph-bridge + falkordb）。三项特性开关均为 `true`（`decay` / `graph` / `dream`）。

## 脚本

| 文件 | 作用 | 复跑方式 |
| --- | --- | --- |
| `probe_payload_inventory.py` | 全库 payload 清单：有效/失效/观察/召回足迹计数，机制条目在最新优先序中的位置，payload 索引 | `docker compose cp` 进 mem0 后 `docker compose exec -T mem0 python /tmp/<file>` |
| `probe_rest_readings.py` | 全部读端点实测：管理面列表与作用域列表的形状差异、`score_details` 全键与检索确定性、`include_observations` / `include_invalidated` 行为、`/graph/stats`、`/dream/runs`、双向追溯端点、三个开关取值 | 先在 `/tmp/mem0_token.txt` 放一个管理员 token，再 `python3 <file>`（脚本内含 token 生成命令） |
| `probe_dream_dryrun.py` | dry-run 零写入验证：前后 `dream_runs` 行数与 Qdrant 点数、报告与响应体逐字段相等 | 同上（token 由脚本内 `users` 表查 admin 生成，不写死 id）；脚本末尾删掉自己那份报告 |
| `probe_observation_cost.py` | 观察清单两条取数路径的成本对比（逐页拉全量再前端过滤 vs 服务端按索引过滤） | 同上 |
| `probe_graph_bridge.py` | 图桥 `/graphs` / `/stats` 读数与「读即建键」副作用 | `docker compose cp` 进 graph-bridge 后执行；**脚本会删除自己的探针键**并在末尾核对键集合复原 |
| `probe_graph_counters.py` | 图派发计数器的实例归属（容器 1 号进程启动时刻 + 两次读数） | `docker compose cp` 进 mem0 后执行 |

## 原始输出

| 文件 | 对应发现 |
| --- | --- |
| `probe_payload_inventory.txt` | F-1、F-4、F-9（全库计数、位置分布、payload 索引） |
| `probe_rest_readings.txt` | F-1～F-5、F-8（端点形状、`score_details` 14 键、检索确定性、计数快照、追溯端点字段面） |
| `probe_dream_dryrun.txt` | §4.4 预览语义（零写入、报告与响应体一致） |
| `probe_observation_cost.txt` | F-7（6 次 / 4.77 MB vs 1 次 / 6 ms） |
| `probe_graph_bridge.txt` | F-6（图键清单、`mem0_xue` 规模、读即建键与清理复原） |
| `probe_graph_counters.txt` | F-5、F-10（计数归实例、进程起点） |

## 数据安全

探针**只读业务数据**，不写 `user_id=xue` 的记忆、不改 `mem0_xue` 图键、不动三项开关。全部输出
只含计数、id 与机制字段值，**不含记忆正文**——正文里出现过用户自述的凭据片段，落盘即泄痕，
故所有探针在打印前已排除 `data` 字段。

例外只有一处，已在脚本内自证并复原：`probe_graph_bridge.py` 为验证「对未知图键读 `/stats` 会新建
空图键」而创建了探针键 `mem0_dashboard_probe_key`，同一脚本随后 `DELETE` 它，并在输出里核对
`cleanup_restored_original_key_set = true`。
