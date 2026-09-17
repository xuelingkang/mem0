
"""join key 机制验证（修正版）：FalkorDB 后端下 group_id 即图键（database）。

driver.database 与 group_id 必须一致，否则 add_episode 会克隆 driver 指向另一个图键，
导致预建的 episode 读不到（实测 NodeNotFoundError）。
"""
import asyncio, os, uuid
from datetime import datetime, timezone

from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_client import OpenAIClient
from graphiti_core.nodes import EpisodeType, EpisodicNode

ENV = {}
for line in open("/Users/xuelingkang/Documents/Containers/mem0/server/.env"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    ENV[k.strip()] = v.strip()

os.environ.setdefault("OPENAI_API_KEY", ENV["LLM_API_KEY"])
GROUP = "gm-joinkey-probe2"
MID = str(uuid.uuid4())
FACT = "薛的 mem0 记忆服务部署在一台 lima docker 虚拟机上，虚拟机由 vz 驱动。"


async def main():
    llm = OpenAIClient(config=LLMConfig(api_key=ENV["LLM_API_KEY"], base_url=ENV["LLM_BASE_URL"],
                                        model=ENV["MEM0_DEFAULT_LLM_MODEL"], small_model=ENV["MEM0_DEFAULT_LLM_MODEL"]))
    embedder = OpenAIEmbedder(config=OpenAIEmbedderConfig(
        api_key=ENV["EMBEDDER_API_KEY"], base_url=ENV["EMBEDDER_BASE_URL"],
        embedding_model=ENV["MEM0_DEFAULT_EMBEDDER_MODEL"], embedding_dim=1024))
    driver = FalkorDriver(host="127.0.0.1", port=16379, database=GROUP)
    g = Graphiti(graph_driver=driver, llm_client=llm, embedder=embedder)
    await g.build_indices_and_constraints()

    now = datetime.now(timezone.utc)
    ep = EpisodicNode(name=MID, group_id=GROUP, labels=[], source=EpisodeType.text,
                      content=FACT, source_description="mem0 事实同步", created_at=now, valid_at=now)
    ep.uuid = MID
    await ep.save(driver)
    print(f"[pre-create] uuid={MID} db={driver._database}")

    res = await g.add_episode(uuid=MID, name=MID, episode_body=FACT,
                              source_description="mem0 事实同步", reference_time=now,
                              source=EpisodeType.text, group_id=GROUP)
    print(f"[add#1] uuid={res.episode.uuid} same_as_mid={res.episode.uuid == MID} nodes={len(res.nodes)} edges={len(res.edges)}")

    edges = await g.search(query="mem0 记忆服务部署在哪台机器？", group_ids=[GROUP], num_results=5)
    for e in edges:
        print(f"[search] fact={e.fact!r} episodes={e.episodes}")

    e1 = await g.edges.entity.get_by_group_ids([GROUP], limit=50)
    print(f"[edges total] {len(e1)}")
    for e in e1:
        print(f"    {e.name} :: {e.fact!r} episodes={e.episodes}")

    try:
        res2 = await g.add_episode(uuid=MID, name=MID, episode_body=FACT,
                                   source_description="mem0 事实同步", reference_time=now,
                                   source=EpisodeType.text, group_id=GROUP)
        print(f"[add#2 replay] ok nodes={len(res2.nodes)} edges={len(res2.edges)}")
    except Exception as ex:
        print(f"[add#2 replay] {type(ex).__name__}: {str(ex)[:200]}")

    e2 = await g.edges.entity.get_by_group_ids([GROUP], limit=50)
    n2 = await g.nodes.entity.get_by_group_ids([GROUP], limit=50)
    print(f"[after replay] entity_edges={len(e2)} entity_nodes={len(n2)}")
    await g.close()


asyncio.run(main())
