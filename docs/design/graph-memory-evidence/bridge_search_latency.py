"""图桥 `/search` 尾部分位实测（核验整改卡 t_362c77d2 第 3 项）。

先按真实形态建一个有实体与关系边的图键，再连续调 `/search` 采样，输出分位数与
超过各档预算的条数——0.4s 与选定的 1.0s 两个候选值都按同一份样本判定。

只写自己的探针图键（`test_graph_rect_lat`），不触碰任何既有图键与事实数据；跑完用
`DELETE /graph/test_graph_rect_lat` 清理。

在 mem0 容器内执行（图桥不发布宿主端口）：

    docker cp bridge_search_latency.py mem0-dev-mem0-1:/tmp/
    docker exec mem0-dev-mem0-1 python /tmp/bridge_search_latency.py 100
"""

import json
import statistics
import sys
import time

import httpx

BRIDGE = "http://graph-bridge:8000"
GROUP_ID = "test_graph_rect_lat"
QUERY = "学科网基础应用中心 授权 登录"

FACTS = [
    "学科网基础应用中心依赖统一身份认证中心完成用户登录，授权页退出登录后必须重新走一遍授权流程。",
    "影（架构师）负责为墨（执行者）产出实现方案，鉴（核验者）独立复核墨的交付物，三者同属 xkw 工程链路。",
    "umbrellaedge 仓库的 graph-memory 阶段使用 FalkorDB 作为图库，graph-bridge 服务通过 Redis 协议访问它。",
]


def main() -> None:
    samples = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    client = httpx.Client(base_url=BRIDGE, timeout=180.0)

    for index, text in enumerate(FACTS):
        started = time.perf_counter()
        response = client.post(
            "/episodes",
            json={
                "uuid": f"rect-lat2-probe-{index}",
                "group_id": GROUP_ID,
                "text": text,
                "source_description": "rect latency probe 2",
            },
        )
        print(f"ingest[{index}] {response.status_code} {time.perf_counter() - started:.2f}s {response.text[:140]}")

    print("stats:", client.get("/stats", params={"group_id": GROUP_ID}).text)

    for index in range(3):
        response = client.post(
            "/search", json={"group_ids": [GROUP_ID], "query": QUERY, "max_facts": 10}, timeout=60.0
        )
        print(f"warmup[{index}] facts={len(response.json().get('facts', []))}")

    values = []
    for index in range(samples):
        started = time.perf_counter()
        response = client.post(
            "/search", json={"group_ids": [GROUP_ID], "query": QUERY, "max_facts": 10}, timeout=60.0
        )
        elapsed = time.perf_counter() - started
        if response.status_code != 200:
            print(f"  !! sample {index} status={response.status_code} {response.text[:120]}")
            continue
        values.append(elapsed)

    ordered = sorted(values)
    n = len(ordered)

    def pct(q: float) -> float:
        return ordered[min(n - 1, int(round(q * (n - 1))))]

    report = {
        "n": n,
        "min": round(ordered[0], 3),
        "p50": round(pct(0.50), 3),
        "p90": round(pct(0.90), 3),
        "p95": round(pct(0.95), 3),
        "p99": round(pct(0.99), 3),
        "max": round(ordered[-1], 3),
        "mean": round(statistics.fmean(ordered), 3),
        "over_0.4": sum(1 for value in ordered if value >= 0.4),
        "over_0.6": sum(1 for value in ordered if value >= 0.6),
        "over_0.8": sum(1 for value in ordered if value >= 0.8),
        "over_1.0": sum(1 for value in ordered if value >= 1.0),
    }
    print("SAMPLES", json.dumps([round(value, 3) for value in ordered]))
    print("REPORT", json.dumps(report))


if __name__ == "__main__":
    main()
