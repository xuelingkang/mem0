"""dashboard-mechanism-visibility 设计探针 A：payload 清单（只读，不落任何写入）。

复跑：
    cd /Users/xuelingkang/Documents/Containers/mem0/server
    docker compose cp ../docs/design/dashboard-mechanism-visibility-evidence/probe_payload_inventory.py mem0:/tmp/
    docker compose exec -T mem0 python /tmp/probe_payload_inventory.py

说明：本探针**只输出计数、id 与机制字段值**，不输出记忆正文——正文里出现过用户自述的
凭据片段，落盘到仓库即为泄痕。机制字段值本身来自 `valid_at` / `invalid_at` /
`superseded_by` / `invalid_reason` / `access_count` / `last_accessed`，不含敏感值。
"""

from __future__ import annotations

import json
from collections import Counter

from qdrant_client import QdrantClient

COLL = "memories_2048"

FIELDS = [
    "valid_at",
    "invalid_at",
    "superseded_by",
    "invalid_reason",
    "memory_kind",
    "access_count",
    "last_accessed",
    "user_id",
    "agent_id",
    "dream_run_id",
    "evidence_count",
    "source_memory_ids",
    "created_at",
]


def main() -> None:
    client = QdrantClient(url="http://qdrant:6333")

    rows = []
    offset = None
    while True:
        batch, offset = client.scroll(
            collection_name=COLL,
            limit=1000,
            offset=offset,
            with_payload=FIELDS,
            with_vectors=False,
        )
        rows.extend([p.payload for p in batch])
        if offset is None:
            break

    invalid = [r for r in rows if r.get("invalid_at")]
    obs = [r for r in rows if r.get("memory_kind") == "observation"]
    newest = sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True)

    out = {
        "collection": COLL,
        "total_points": len(rows),
        "valid_at_non_null": sum(1 for r in rows if r.get("valid_at")),
        "invalid_at_non_null": len(invalid),
        "superseded_by_non_null": sum(1 for r in rows if r.get("superseded_by")),
        "access_count_non_zero": sum(1 for r in rows if r.get("access_count")),
        "last_accessed_non_null": sum(1 for r in rows if r.get("last_accessed")),
        "observations": len(obs),
        "memory_kind_counter": dict(Counter(str(r.get("memory_kind")) for r in rows)),
        "invalid_reason_counter": dict(Counter(r.get("invalid_reason") for r in invalid)),
        "observation_scopes": dict(
            Counter(f"{r.get('user_id')}:{r.get('agent_id')}" for r in obs)
        ),
        "observation_dream_runs": dict(Counter(str(r.get("dream_run_id")) for r in obs)),
        "invalidated_ids": [r.get("created_at") for r in invalid],
        "invalidated_positions_in_newest_first_order": [
            idx for idx, r in enumerate(newest) if r.get("invalid_at")
        ],
        "observations_positions_in_newest_first_order": [
            idx for idx, r in enumerate(newest) if r.get("memory_kind") == "observation"
        ][:5],
        "observations_position_max_in_newest_first_order": max(
            (idx for idx, r in enumerate(newest) if r.get("memory_kind") == "observation"),
            default=None,
        ),
        "scope_agent_ying_total": sum(1 for r in rows if r.get("agent_id") == "ying"),
        "scope_agent_ying_newest_1000_observations": sum(
            1
            for r in [x for x in newest if x.get("agent_id") == "ying"][:1000]
            if r.get("memory_kind") == "observation"
        ),
        "global_newest_1000_observations": sum(
            1 for r in newest[:1000] if r.get("memory_kind") == "observation"
        ),
        "invalid_field_values_sample": next(
            (
                {
                    "id_present": True,
                    "valid_at": r.get("valid_at"),
                    "invalid_at": r.get("invalid_at"),
                    "superseded_by_present": bool(r.get("superseded_by")),
                    "invalid_reason": r.get("invalid_reason"),
                }
                for r in invalid
            ),
            None,
        ),
        "access_field_values_sample": next(
            (
                {
                    "access_count": r.get("access_count"),
                    "last_accessed": r.get("last_accessed"),
                }
                for r in rows
                if r.get("access_count")
            ),
            None,
        ),
        "payload_schema": json.loads(
            __import__("urllib.request", fromlist=["urlopen"])
            .urlopen(f"http://qdrant:6333/collections/{COLL}")
            .read()
            .decode()
        )["result"]["payload_schema"],
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))


main()
