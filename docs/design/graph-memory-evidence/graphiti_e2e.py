"""Graphiti 端到端实测：FalkorDB（容器）+ 本机 LLM 网关 + 本机阿里云 embedder。

目的：为 graph-memory 方案的「就地可跑」结论提供实测证据（图可用性、资源占用、时延）。
仅写探测用图（group_id=gm-probe-*），不动 mem0 的 Qdrant 数据。
"""

import asyncio
import json
import os
import resource
import time
from datetime import datetime, timezone

from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_client import OpenAIClient
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.nodes import EpisodeType
from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF

ENV = {}
for line in open("/Users/xuelingkang/Documents/Containers/mem0/server/.env"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    ENV[k.strip()] = v.strip()

GROUP = os.environ.get("GM_GROUP", "gm-probe")

# Graphiti 在构造期即建立 OpenAIClient / OpenAIEmbedder / OpenAIRerankerClient 三个客户端；
# 默认客户端只从环境里取 key（`OPENAI_API_KEY`），所以必须先落到环境变量里。
os.environ.setdefault("OPENAI_API_KEY", ENV["LLM_API_KEY"])

FACTS = [
    "薛是学科网基础应用中心的后端工程师，主力语言是 Java 与 Spring。",
    "薛的机器上跑着一个 lima docker 虚拟机，配置为 2 CPU / 8GiB。",
    "学科网基础应用中心有一个自托管的 mem0 记忆服务，主存在 Qdrant。",
    "薛偏好扁平目录结构，不喜欢多余的嵌套层级。",
    "mem0 的记忆衰减机制由 Ebbinghaus 遗忘曲线实现，时间因子的下界是 0.90。",
]


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024


async def main() -> None:
    mode = os.environ.get("GM_CLIENT", "responses")
    if mode == "responses":
        # 原生结构化输出（/responses + text.format=json_schema）：本机网关实测可用，
        # 模型无法把 schema 当值回吐。
        llm = OpenAIClient(
            config=LLMConfig(
                api_key=ENV["LLM_API_KEY"],
                base_url=ENV["LLM_BASE_URL"],
                model=ENV["MEM0_DEFAULT_LLM_MODEL"],
                small_model=ENV["MEM0_DEFAULT_LLM_MODEL"],
            )
        )
    else:
        llm = OpenAIGenericClient(
            config=LLMConfig(
                api_key=ENV["LLM_API_KEY"],
                base_url=ENV["LLM_BASE_URL"],
                model=ENV["MEM0_DEFAULT_LLM_MODEL"],
                small_model=ENV["MEM0_DEFAULT_LLM_MODEL"],
            ),
            structured_output_mode="json_object",
        )
    embedder = OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            api_key=ENV["EMBEDDER_API_KEY"],
            base_url=ENV["EMBEDDER_BASE_URL"],
            embedding_model=ENV["MEM0_DEFAULT_EMBEDDER_MODEL"],
            embedding_dim=1024,
        )
    )
    driver = FalkorDriver(host="127.0.0.1", port=16379, database="default_db")
    g = Graphiti(graph_driver=driver, llm_client=llm, embedder=embedder)

    t0 = time.perf_counter()
    await g.build_indices_and_constraints()
    print(f"[setup] build_indices_and_constraints {time.perf_counter() - t0:.2f}s")

    lat = []
    for i, fact in enumerate(FACTS):
        t = time.perf_counter()
        res = await g.add_episode(
            name=f"fact-{i}",
            episode_body=fact,
            source_description="mem0 事实同步（探测）",
            reference_time=datetime.now(timezone.utc),
            source=EpisodeType.text,
            group_id=GROUP,
        )
        dt = time.perf_counter() - t
        lat.append(dt)
        print(
            f"[ingest {i}] {dt:.2f}s nodes={len(res.nodes)} edges={len(res.edges)}"
            f" communities={len(res.communities)} rss={rss_mb():.0f}MB"
        )

    for q in ["薛用什么语言写后端？", "mem0 的记忆衰减用什么曲线？", "docker 虚拟机多少内存？"]:
        t = time.perf_counter()
        edges = await g.search(query=q, group_ids=[GROUP], num_results=5)
        dt = time.perf_counter() - t
        print(f"[search] {dt:.2f}s {q!r} -> {len(edges)} facts")
        for e in edges[:3]:
            print(f"    fact={e.fact!r} valid_at={e.valid_at} invalid_at={e.invalid_at}")

    t = time.perf_counter()
    res = await g._search(query="薛的偏好", config=EDGE_HYBRID_SEARCH_RRF, group_ids=[GROUP])
    print(f"[_search RRF] {time.perf_counter() - t:.2f}s edges={len(res.edges)} nodes={len(res.nodes)}")

    nodes = await g.nodes.entity.get_by_group_ids([GROUP], limit=100)
    print(f"[graph] entity_nodes={len(nodes)}")
    for n in nodes[:12]:
        print(f"    node={n.name!r} labels={n.labels}")

    edges = await g.edges.entity.get_by_group_ids([GROUP], limit=100)
    print(f"[graph] entity_edges={len(edges)}")
    for e in edges[:12]:
        print(f"    edge name={e.name!r} fact={e.fact!r}")

    print(f"[stats] ingest latency min/med/max = {min(lat):.2f}/{sorted(lat)[len(lat) // 2]:.2f}/{max(lat):.2f}s  peak_rss={rss_mb():.0f}MB")
    await g.close()


asyncio.run(main())
