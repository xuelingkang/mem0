"""dashboard-mechanism-visibility 设计探针 E：观察清单的两条取数路径成本对比（只读）。

复跑：
    cd /Users/xuelingkang/Documents/Containers/mem0/server
    docker compose cp ../docs/design/dashboard-mechanism-visibility-evidence/probe_observation_cost.py mem0:/tmp/
    docker compose exec -T mem0 python /tmp/probe_observation_cost.py

路径 A（纯前端可行方案）：逐页拉全量管理列表，客户端过滤 `memory_kind == "observation"`。
路径 B（服务端过滤）：Qdrant payload 过滤 `memory_kind`（已建 keyword 索引），一次取回观察。

输出：两条路径的请求次数、传输字节数、耗时与结果条数——用于论证观察清单是否需要服务端过滤。
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

BASE = "http://localhost:8000"
COLL = "memories_2048"


def _admin_token() -> str:
    import sys

    sys.path.insert(0, "/app")
    from auth import create_access_token
    from db import SessionLocal
    from models import User
    from sqlalchemy import select

    session = SessionLocal()
    try:
        uid = str(session.scalar(select(User).where(User.role == "admin").order_by(User.created_at.asc())).id)
    finally:
        session.close()
    return create_access_token(uid, "admin")


TOKEN = _admin_token()


def get(path: str):
    req = urllib.request.Request(
        BASE + path, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    )
    raw = urllib.request.urlopen(req, timeout=300).read()
    return json.loads(raw.decode()), len(raw)


def main() -> None:
    out = {}

    # 路径 A：逐页拉全量再客户端过滤
    started = time.time()
    cursor = None
    requests = 0
    bytes_total = 0
    rows = 0
    observations = 0
    while True:
        path = "/memories?top_k=1000" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        page, nbytes = get(path)
        requests += 1
        bytes_total += nbytes
        rows += len(page["results"])
        observations += sum(1 for r in page["results"] if r.get("memory_kind") == "observation")
        cursor = page.get("next_cursor")
        if not cursor or not page["results"]:
            break
        if requests >= 200:
            break
    out["path_a_full_walk"] = {
        "requests": requests,
        "rows": rows,
        "bytes": bytes_total,
        "seconds": round(time.time() - started, 2),
        "observations_found": observations,
    }

    # 路径 B：服务端按 payload 索引过滤（SDK get_all 的 memory_kind 条件）
    from qdrant_client import QdrantClient

    client = QdrantClient(url="http://qdrant:6333")
    started = time.time()
    points, _ = client.scroll(
        collection_name=COLL,
        limit=200,
        with_payload=["data", "memory_kind", "evidence_count", "source_memory_ids", "dream_run_id"],
        with_vectors=False,
        scroll_filter={
            "must": [
                {"key": "memory_kind", "match": {"value": "observation"}},
            ]
        },
    )
    out["path_b_server_side_filter"] = {
        "requests": 1,
        "rows": len(points),
        "seconds": round(time.time() - started, 4),
    }

    out["payload_index_present"] = "memory_kind" in json.loads(
        urllib.request.urlopen(f"http://qdrant:6333/collections/{COLL}").read().decode()
    )["result"]["payload_schema"]

    print(json.dumps(out, ensure_ascii=False, indent=1))


main()
