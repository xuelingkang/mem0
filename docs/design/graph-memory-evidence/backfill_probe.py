"""graph-bridge `POST /backfill` 实测探针（设计 `docs/design/graph-memory.md` §5.5）。

在 mem0 容器内跑（图桥不发布宿主端口，容器内用服务名互访）：

    cd /Users/xuelingkang/Documents/Containers/mem0
    docker cp docs/design/graph-memory-evidence/backfill_probe.py mem0-dev-mem0-1:/tmp/
    docker exec -e MEM0_API_KEY=<key> -w /tmp mem0-dev-mem0-1 \
      python backfill_probe.py seed --count 26 --scope bfprobe_<ts>
    ... run / status / graph / concurrency / cleanup ...

各子命令输出的 JSON 即证据原文（`backfill_probe.txt`）。

**只写隔离作用域**：种子事实直接 upsert 进 Qdrant（附随机向量，非真实 embedding——它们
不参与检索，只用于让回填有存量可取），`user_id` 取探针专名，`cleanup` 按作用域全额清除。
不触碰 `xue` 的存量记录与 `mem0_xue` 图键。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

BRIDGE = os.environ.get("BRIDGE_URL", "http://graph-bridge:8000")
QDRANT = os.environ.get("QDRANT_URL", "http://qdrant:6333")
MEM0 = os.environ.get("MEM0_URL", "http://localhost:8000")
COLLECTION = os.environ.get("QDRANT_COLLECTION_NAME", "memories_2048")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDER_EMBEDDING_DIMS", "2048"))
API_KEY = os.environ.get("MEM0_API_KEY", "")

FACTS = [
    "探针项目 Aurora 的代码仓库托管在自建 GitLab，项目编号是 7301。",
    "Aurora 的构建机有两块 vCPU 与八 GiB 内存，构建镜像基于 Python 3.12。",
    "Aurora 的图服务容器内存上限是 512 MiB，图库容器上限是 384 MiB。",
    "Aurora 的向量库集合名为 aurora_vectors_2048，维度是 2048。",
    "Aurora 的网关地址是 esp.internal.example，只实现了 chat/completions 通道。",
    "Aurora 的默认抽取模型是 aurora-extract-v2，温度设为 0.2。",
    "Aurora 的嵌入模型是 aurora-embedding-3，输出 2048 维向量。",
    "Aurora 的观察条目由夜间整合任务生成，标记为 observation。",
    "Aurora 的衰减半衰期设为 30 天，访问上限设为 12 次。",
    "Aurora 的重试策略是最多 3 次、退避基数为 5 秒。",
    "Aurora 的熔断阈值是连续 5 次失败，冷却窗口 60 秒。",
    "Aurora 的队列上限是 1000 条，队列满时丢弃最旧任务。",
    "Aurora 的图检索预算默认 1.0 秒，超时按无图信号降级。",
    "Aurora 的图加分上限是 0.5，与实体加分同量级。",
    "Aurora 的图派发客户端超时预算设为 240 秒。",
    "Aurora 的仪表盘运行在 3000 端口，通过中间件校验刷新令牌。",
    "Aurora 的管理接口需要管理员角色或 API Key 鉴权。",
    "Aurora 的数据库连接串指向 pgvector 17，库名是 aurora_app。",
    "Aurora 的请求日志保留 30 天，之后由例行任务清理。",
    "Aurora 的导出接口支持 JSON 与 CSV 两种格式。",
    "Aurora 的观察接口按页返回，页大小上限是 1000 行。",
    "Aurora 的记忆写入接口在派发图任务时只做内存入队。",
    "Aurora 的检索接口默认不返回观察条目。",
    "Aurora 的图键命名规则是前缀加作用域值，分隔符用下划线。",
    "Aurora 的隔离验证约定：测试数据一律用 test 前缀的作用域值。",
    "Aurora 的文档目录是 docs/design，设计文档与证据分目录存放。",
]


def _request(url: str, method: str = "GET", body=None, headers=None, timeout=1800):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read().decode()
            status = response.status
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode()
        status = exc.code
    return {"status": status, "body": payload, "elapsed_seconds": round(time.monotonic() - started, 3)}


def _json(result):
    try:
        return json.loads(result["body"])
    except ValueError:
        return result["body"]


def _group_id(scope: str) -> str:
    return f"mem0_{scope}"


def _qdrant_count(scope: str) -> int:
    result = _request(
        f"{QDRANT}/collections/{COLLECTION}/points/count",
        "POST",
        {"exact": True, "filter": {"must": [{"key": "user_id", "match": {"value": scope}}]}},
    )
    return int(_json(result)["result"]["count"])


def _graph_stats(group_id: str):
    return _json(_request(f"{BRIDGE}/stats?group_id={group_id}"))


def _graph_keys():
    return _json(_request(f"{BRIDGE}/graphs"))["graphs"]


def _seed(args) -> None:
    """按 `count` 条种子事实直接 upsert 进 Qdrant（绕过 mem0 的派发，回填才有活可干）。"""
    rng = random.Random(20260918)
    now = datetime.now(timezone.utc)
    points = []
    for index in range(args.count):
        # created_at 逐条递减：与真实写入的时间序一致，游标扫描才有可判定的顺序。
        created = (now - timedelta(minutes=index)).isoformat()
        points.append(
            {
                "id": str(uuid.uuid4()),
                "vector": [rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIMS)],
                "payload": {
                    "user_id": args.scope,
                    "data": FACTS[index % len(FACTS)],
                    "hash": f"{index:032x}",
                    "created_at": created,
                    "updated_at": created,
                },
            }
        )
    result = _request(f"{QDRANT}/collections/{COLLECTION}/points?wait=true", "PUT", {"points": points})
    print(
        json.dumps(
            {
                "stage": "seed",
                "scope": args.scope,
                "group_id": _group_id(args.scope),
                "seeded": args.count,
                "upsert_status": result["status"],
                "qdrant_scope_count": _qdrant_count(args.scope),
                "graph_key_present_before": _group_id(args.scope) in _graph_keys(),
            },
            ensure_ascii=False,
        )
    )


def _run(args) -> None:
    """一次 `POST /backfill`，输出请求体、响应与前后图规模（增量可对账）。"""
    group_id = _group_id(args.scope)
    stats_before = _graph_stats(group_id)
    body = {"group_id": group_id, "limit": args.limit, "rate": args.rate, "scan_limit": args.scan_limit}
    if args.cursor:
        body["cursor"] = args.cursor

    started = time.monotonic()
    result = _request(f"{BRIDGE}/backfill", "POST", body, timeout=args.timeout)
    wall = time.monotonic() - started
    stats_after = _graph_stats(group_id)
    print(
        json.dumps(
            {
                "stage": "run",
                "request": body,
                "http_status": result["status"],
                "wall_seconds": round(wall, 3),
                "response": _json(result),
                "graph_before": stats_before,
                "graph_after": stats_after,
                "graph_episode_delta": stats_after.get("episodes", 0) - stats_before.get("episodes", 0),
                "graph_edge_delta": stats_after.get("entity_edges", 0) - stats_before.get("entity_edges", 0),
            },
            ensure_ascii=False,
        )
    )


def _status(args) -> None:
    print(json.dumps({"stage": "status", "body": _json(_request(f"{BRIDGE}/backfill/status"))}, ensure_ascii=False))


def _graph(args) -> None:
    print(
        json.dumps(
            {
                "stage": "graph",
                "stats": _graph_stats(_group_id(args.scope)),
                "keys": _graph_keys(),
                "qdrant_scope_count": _qdrant_count(args.scope),
            },
            ensure_ascii=False,
        )
    )


def _concurrency(args) -> None:
    """回填进行中的主链路实测：`POST /memories` 与 `/search` 的时延与正确性。

    `POST /memories` 用 `infer=false` 写入一条隔离作用域的事实（不触发抽取的 LLM 调用，
    因此这里量的是**主链路自身**的时延，而不是 LLM 时延）；`/search` 打同一隔离作用域。
    """
    headers = {"X-API-Key": API_KEY}
    marker = f"并发实测标记 {int(time.time())}"
    writes = []
    for index in range(args.writes):
        result = _request(
            f"{MEM0}/memories",
            "POST",
            {
                "messages": [{"role": "user", "content": f"{marker} #{index}"}],
                "user_id": args.scope,
                "infer": False,
            },
            headers=headers,
        )
        writes.append({"http_status": result["status"], "elapsed_seconds": result["elapsed_seconds"]})

    searches = []
    for _ in range(args.searches):
        result = _request(
            f"{MEM0}/search",
            "POST",
            {"query": "Aurora 的图检索预算", "user_id": args.scope, "limit": 3},
            headers=headers,
        )
        body = _json(result)
        searches.append(
            {
                "http_status": result["status"],
                "elapsed_seconds": result["elapsed_seconds"],
                "result_count": len(body.get("results", [])) if isinstance(body, dict) else None,
                "graph_status": (body.get("results") or [{}])[0].get("graph_status") if isinstance(body, dict) else None,
            }
        )
    print(
        json.dumps(
            {
                "stage": "concurrency",
                "scope": args.scope,
                "writes": writes,
                "searches": searches,
                "backfill_status_while_running": _json(_request(f"{BRIDGE}/backfill/status")),
            },
            ensure_ascii=False,
        )
    )


def _cleanup(args) -> None:
    """清理：隔离作用域的 Qdrant 点（经 mem0 的删除接口）+ 图键（经图桥）。"""
    headers = {"X-API-Key": API_KEY}
    delete_points = _request(f"{MEM0}/memories?user_id={args.scope}", "DELETE", None, headers=headers)
    drop_graph = _request(f"{BRIDGE}/graph/{_group_id(args.scope)}", "DELETE")
    time.sleep(1.0)
    print(
        json.dumps(
            {
                "stage": "cleanup",
                "delete_memories": {"http_status": delete_points["status"], "body": _json(delete_points)},
                "drop_graph": {"http_status": drop_graph["status"], "body": _json(drop_graph)},
                "qdrant_scope_count_after": _qdrant_count(args.scope),
                "graph_key_present_after": _group_id(args.scope) in _graph_keys(),
            },
            ensure_ascii=False,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="graph-bridge backfill probe")
    parser.add_argument("stage", choices=["seed", "run", "status", "graph", "concurrency", "cleanup"])
    parser.add_argument("--scope", default="", help="隔离作用域值（user_id），如 bfprobe_1410（`status` 不需要）")
    parser.add_argument("--count", type=int, default=26)
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--rate", type=float, default=0.0)
    parser.add_argument("--scan-limit", type=int, default=200)
    parser.add_argument("--cursor", default=None)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--writes", type=int, default=5)
    parser.add_argument("--searches", type=int, default=5)
    args = parser.parse_args()
    # `status` 只看进程内读数，与作用域无关，故它不需要 `--scope`；其余阶段都要。
    if args.stage != "status" and not args.scope:
        parser.error(f"--scope is required for '{args.stage}'")
    {
        "seed": _seed,
        "run": _run,
        "status": _status,
        "graph": _graph,
        "concurrency": _concurrency,
        "cleanup": _cleanup,
    }[args.stage](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
