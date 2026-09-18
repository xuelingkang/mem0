"""dashboard-mechanism-visibility 设计探针 B：REST 读面清点（全部只读，零写入）。

复跑：
    cd /Users/xuelingkang/Documents/Containers/mem0/server
    # 1) 取一个管理员 access token（30 分钟有效，本机 admin 用户）
    docker compose exec -T mem0 python -c "
    import sys; sys.path.insert(0,'/app')
    from auth import create_access_token
    print(create_access_token('<users.id>', 'admin'))" > /tmp/mem0_token.txt
    # 2) 跑探针
    python3 ../docs/design/dashboard-mechanism-visibility-evidence/probe_rest_readings.py

输出仅含字段名、计数、id 前缀与机制字段值，**不含记忆正文**（正文里出现过用户自述的
凭据片段）。开关态由 server/.env 决定（本次：decay/graph/dream 均为 true）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://localhost:8888"
TOKEN = open("/tmp/mem0_token.txt").read().strip()


def call(path: str, method: str = "GET", body=None, timeout: int = 300):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    except urllib.error.HTTPError as exc:
        return {"http_status": exc.code, "http_body": exc.read().decode()[:200]}


def main() -> None:
    out = {}

    # 1. 管理面列表（无作用域）：游标翻页 + total
    page1 = call("/memories?top_k=1000")
    ids1 = [r["id"] for r in page1["results"]]
    page2 = call(f"/memories?top_k=1000&cursor={urllib.parse.quote(page1['next_cursor'])}")
    ids2 = [r["id"] for r in page2["results"]]
    out["memories_admin_list"] = {
        "total": page1.get("total"),
        "page1_rows": len(ids1),
        "page2_rows": len(ids2),
        "overlap_page1_page2": len(set(ids1) & set(ids2)),
        "page1_newest": page1["results"][0]["created_at"],
        "page1_oldest": page1["results"][-1]["created_at"],
        "page2_newest": page2["results"][0]["created_at"],
        "response_keys": sorted(page1.keys()),
        "row_keys": sorted(page1["results"][0].keys()),
        "invalidated_rows_in_page1": sum(1 for r in page1["results"] if r.get("invalid_at")),
        "observation_rows_in_page1": sum(
            1 for r in page1["results"] if r.get("memory_kind") == "observation"
        ),
        "top_k_10_rows": len(call("/memories?top_k=10")["results"]),
    }

    # 2. 作用域列表：形状 + 观察可见性 + 上限
    scoped = call("/memories?user_id=xue&agent_id=ying&top_k=5")
    scoped_max = call("/memories?user_id=xue&agent_id=ying&top_k=1000")
    out["memories_scoped_list"] = {
        "response_keys": sorted(scoped.keys()),
        "rows": len(scoped["results"]),
        "row_keys": sorted(scoped["results"][0].keys()),
        "kinds_in_first_5": [r.get("memory_kind") for r in scoped["results"]],
        "rows_at_top_k_1000": len(scoped_max["results"]),
        "unknown_param_ignored": len(call("/memories?user_id=xue&kind=observation")["results"]),
    }

    # 3. 检索：解释分量、图分支状态、观察可见性、失效可见性
    explain = call(
        "/search",
        "POST",
        {
            "query": "记忆衰减 机制",
            "top_k": 8,
            "filters": {"user_id": "xue"},
            "explain": True,
        },
    )
    plain = call(
        "/search", "POST", {"query": "记忆衰减 机制", "top_k": 8, "filters": {"user_id": "xue"}}
    )
    out["search_explain"] = {
        "response_keys": sorted(explain.keys()),
        "row_keys": sorted(explain["results"][0].keys()),
        "score_details_keys": sorted(explain["results"][0]["score_details"].keys()),
        "graph_status_distinct_in_one_response": sorted(
            {r.get("graph_status") for r in explain["results"]}
        ),
        "ids_identical_with_and_without_explain": [r["id"] for r in explain["results"]]
        == [r["id"] for r in plain["results"]],
        "score_details_absent_without_explain": "score_details" not in plain["results"][0],
        "score_fields_sample": {
            k: explain["results"][0]["score_details"].get(k)
            for k in (
                "semantic_score",
                "bm25_score",
                "entity_boost",
                "graph_boost",
                "graph_facts",
                "max_possible_score",
                "raw_score",
                "final_score",
                "threshold",
                "decay_weight",
                "retention",
                "memory_strength_days",
                "elapsed_days",
                "access_count",
            )
        },
    }

    wildcard = call(
        "/search",
        "POST",
        {"query": "记忆衰减 机制", "top_k": 8, "filters": {"user_id": "*"}, "explain": True},
    )
    scoped_graph = call(
        "/search",
        "POST",
        {"query": "记忆衰减 机制", "top_k": 8, "filters": {"user_id": "xue"}, "explain": True},
    )
    out["search_graph_signal"] = {
        "wildcard_scope_boosts": [r["score_details"]["graph_boost"] for r in wildcard["results"]],
        "wildcard_scope_status": sorted({r.get("graph_status") for r in wildcard["results"]}),
        "xue_scope_boosts": [r["score_details"]["graph_boost"] for r in scoped_graph["results"]],
        "xue_scope_facts": [r["score_details"]["graph_facts"] for r in scoped_graph["results"]],
    }

    obs_search = call(
        "/search",
        "POST",
        {
            "query": "iris",
            "top_k": 5,
            "filters": {"user_id": "xue", "agent_id": "ying", "memory_kind": {"eq": "observation"}},
            "include_observations": True,
        },
    )
    obs_search_without_flag = call(
        "/search",
        "POST",
        {
            "query": "iris",
            "top_k": 5,
            "filters": {"user_id": "xue", "agent_id": "ying", "memory_kind": {"eq": "observation"}},
        },
    )
    invalid_search = call(
        "/search",
        "POST",
        {
            "query": "看板任务 graph-memory",
            "top_k": 5,
            "filters": {"user_id": "xue"},
            "include_invalidated": True,
        },
    )
    out["search_flags"] = {
        "observations_with_include_observations": len(obs_search["results"]),
        "observations_without_flag": len(obs_search_without_flag["results"]),
        "invalidated_with_include_invalidated": sum(
            1 for r in invalid_search["results"] if r.get("invalid_at")
        ),
        "invalidated_sample_fields": [
            {
                "invalid_at": r.get("invalid_at"),
                "superseded_by_present": bool(r.get("superseded_by")),
                "invalid_reason": r.get("invalid_reason"),
            }
            for r in invalid_search["results"]
            if r.get("invalid_at")
        ],
    }

    # 4. 检索确定性：id 序稳定，分值因向量检索本身有末位抖动
    repeat = [
        call("/search", "POST", {"query": "记忆衰减 机制", "top_k": 5, "filters": {"user_id": "xue"}})
        for _ in range(3)
    ]
    out["search_repeatability"] = {
        "id_order_identical_across_3_calls": len({tuple(r["id"] for r in x["results"]) for x in repeat})
        == 1,
        "max_score_spread_at_position_0": max(x["results"][0]["score"] for x in repeat)
        - min(x["results"][0]["score"] for x in repeat),
    }

    # 5. 图分支计数快照（进程内）
    out["graph_stats"] = call("/graph/stats")
    out["graph_routes_absent"] = {
        "/graph/graphs": call("/graph/graphs").get("http_status"),
        "/graph/bridge": call("/graph/bridge").get("http_status"),
    }

    # 6. Dream 运行审计
    runs = call("/dream/runs?limit=5")
    out["dream_runs"] = {
        "total": runs.get("total"),
        "response_keys": sorted(runs.keys()),
        "row_keys": sorted(runs["results"][0].keys()) if runs.get("results") else [],
        "row_sample": {
            k: runs["results"][0].get(k)
            for k in ("mode", "status", "scopes", "clusters", "llm_calls", "failed_clusters",
                      "observations_written", "observations_superseded", "duration_seconds")
        }
        if runs.get("results")
        else None,
    }
    if runs.get("results"):
        out["dream_run_by_id_keys"] = sorted(
            call(f"/dream/runs/{runs['results'][0]['id']}").keys()
        )

    # 7. 证据链双向（取一条观察与它的第一条源事实）
    all_rows = page1["results"] + page2["results"]
    observation = next((r for r in all_rows if r.get("memory_kind") == "observation"), None)
    if observation:
        sources = call(f"/memories/{observation['id']}/sources")
        out["dream_evidence_chain"] = {
            "observation_rows_shape": {},
            "observation_detail_keys": sorted(call(f"/memories/{observation['id']}").keys()),
            "observation_row_keys_in_admin_list": sorted(observation.keys()),
            "observation_row_lacks_full_text": "memory" in observation,
            "sources_response_keys": sorted(sources.keys()),
            "sources_total": sources.get("total"),
            "sources_missing": sources.get("missing"),
            "source_row_keys": sorted(sources["results"][0].keys()) if sources.get("results") else [],
            "sources_of_non_observation_status": call(
                "/memories/" + page1["results"][0]["id"] + "/sources"
            ).get("http_status"),
        }
        if sources.get("results"):
            reverse = call(f"/memories/{sources['results'][0]['id']}/observations")
            out["dream_evidence_chain"]["reverse_response_keys"] = sorted(reverse.keys())
            out["dream_evidence_chain"]["reverse_row_keys"] = (
                sorted(reverse["results"][0].keys()) if reverse.get("results") else []
            )
            out["dream_evidence_chain"]["reverse_finds_the_observation"] = any(
                r.get("id") == observation["id"] for r in reverse.get("results", [])
            )
            out["dream_evidence_chain"]["reverse_total"] = reverse.get("total")

    # 8. 配置面（三个开关的当前取值）
    cfg = call("/configure")
    out["config_switches"] = {
        "decay": cfg.get("decay"),
        "dream_enabled": (cfg.get("dream") or {}).get("enabled"),
        "graph_enabled": (cfg.get("graph") or {}).get("enabled"),
        "graph_timeout_seconds": (cfg.get("graph") or {}).get("timeout_seconds"),
    }

    print(json.dumps(out, ensure_ascii=False, indent=1))


main()
