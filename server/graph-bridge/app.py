"""graph-bridge：mem0 的旁路图服务（Graphiti 薄壳）。

设计见 `docs/design/graph-memory.md` §4。本服务持有 graphiti-core 与 FalkorDB 连接，
对 mem0 侧暴露四个端点：

| 端点 | 作用 |
| --- | --- |
| `POST /episodes` | 一条事实入图（幂等前置判定 + 先建 episode 再抽取） |
| `POST /search` | 按查询文本做图检索，返回事实及其关联的 memory id |
| `GET /stats` | 图规模（episodes / 实体节点 / 关系边） |
| `GET /health` | 存活与后端自证（含 FalkorDB 连通性） |
| `GET /graphs`、`DELETE /graph/{group_id}` | 图键清单与按作用域整体清理 |

三条实现约定（均来自本机实测，方案 §5.3–5.4）：

1. **join key = episode uuid**：入图前先落一个 `uuid == memory_id` 的 `EpisodicNode`，
   再以同一 uuid 调 `add_episode`；检索结果里的边自带 `episodes` 列表，即 memory id。
2. **`group_id` 即 FalkorDB 图键**：driver 的 database 必须与该次请求的 `group_id`
   一致，否则 `add_episode` 会克隆 driver 去另一个图键读 episode（`NodeNotFoundError`）。
   因此按 `group_id` 缓存 Graphiti 实例。
3. **幂等必须前置判定**：同 uuid 重放 `add_episode` 不报错但会重新抽取（边数增长），
   所以先查该 uuid 的 `Episodic.entity_edges` 是否非空，非空即直接返回 `already_synced`。

本服务不修改 graphiti-core 一行源码：LLM 客户端走 `/responses` 通道（本机网关实测唯一
可用形态），embedder 用 OpenAI 兼容客户端并显式指定维度。
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

logger = logging.getLogger("graph-bridge")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

# --------------------------------------------------------------------------- config

FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "falkordb")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6379"))

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or None
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-5-mini")

EMBEDDER_API_KEY = os.environ.get("EMBEDDER_API_KEY", LLM_API_KEY)
EMBEDDER_BASE_URL = os.environ.get("EMBEDDER_BASE_URL") or None
EMBEDDER_MODEL = os.environ.get("EMBEDDER_MODEL", "text-embedding-3-small")
EMBEDDER_DIMS = int(os.environ.get("EMBEDDER_DIMS", "1024"))

SOURCE_DESCRIPTION_DEFAULT = "mem0 事实同步"


# --------------------------------------------------------------------------- schemas


class EpisodeRequest(BaseModel):
    """一条待入图的事实。"""

    uuid: str = Field(..., description="episode uuid，等于 mem0 的 memory id。")
    group_id: str = Field(..., description="图键（作用域派生）。")
    text: str = Field(..., description="事实文本。")
    reference_time: Optional[str] = Field(None, description="事实时间（ISO8601，缺省取当前时刻）。")
    source_description: Optional[str] = Field(None, description="来源描述。")


class EpisodeResponse(BaseModel):
    status: str = Field(..., description="`synced` 或 `already_synced`。")
    uuid: str
    group_id: str
    nodes: int = Field(default=0, description="本次抽取出的实体节点数。")
    edges: int = Field(default=0, description="本次抽取出的关系边数。")


class SearchRequest(BaseModel):
    group_ids: List[str] = Field(..., description="要检索的图键列表。")
    query: str = Field(..., description="检索文本。")
    max_facts: int = Field(default=10, ge=1, description="最多返回的事实条数。")


class FactItem(BaseModel):
    uuid: str = Field(..., description="关系边 uuid。")
    name: str = Field(default="", description="关系名。")
    fact: str = Field(default="", description="事实文本。")
    valid_at: Optional[str] = Field(default=None)
    invalid_at: Optional[str] = Field(default=None)
    episodes: List[str] = Field(default_factory=list, description="产生该事实的 memory id 列表。")


class SearchResponse(BaseModel):
    facts: List[FactItem] = Field(default_factory=list)


# --------------------------------------------------------------------------- clients


def _build_llm_client():
    """装配 LLM 客户端（`/responses` 结构化输出通道，本机网关实测唯一可用形态）。"""
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_client import OpenAIClient

    config = LLMConfig(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        model=LLM_MODEL,
        small_model=LLM_MODEL,
    )
    return OpenAIClient(config=config)


def _build_embedder():
    """装配 embedder（OpenAI 兼容端点，显式指定维度以对齐本机 embedder 模型）。"""
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig

    return OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            api_key=EMBEDDER_API_KEY,
            base_url=EMBEDDER_BASE_URL,
            embedding_model=EMBEDDER_MODEL,
            embedding_dim=EMBEDDER_DIMS,
        )
    )


def _build_cross_encoder():
    """装配 cross encoder：显式传本机 LLM 配置，避免回落到读环境变量的默认客户端。"""
    from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
    from graphiti_core.llm_client.config import LLMConfig

    return OpenAIRerankerClient(config=LLMConfig(api_key=LLM_API_KEY, base_url=LLM_BASE_URL, model=LLM_MODEL))


class GraphitiRegistry:
    """按图键缓存 Graphiti 实例（driver 的 database 必须等于图键，见模块 docstring）。"""

    def __init__(self) -> None:
        self._instances: Dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def get(self, group_id: str):
        async with self._lock:
            instance = self._instances.get(group_id)
            if instance is not None:
                return instance
            from graphiti_core import Graphiti
            from graphiti_core.driver.falkordb_driver import FalkorDriver

            driver = FalkorDriver(
                host=FALKORDB_HOST,
                port=FALKORDB_PORT,
                database=group_id,
            )
            instance = Graphiti(
                graph_driver=driver,
                llm_client=_build_llm_client(),
                embedder=_build_embedder(),
                cross_encoder=_build_cross_encoder(),
                # 单 worker 串行入图：图侧 LLM 调用不与主链路的 LLM 调用争抢（方案 §3.3）。
                max_coroutines=int(os.environ.get("SEMAPHORE_LIMIT", "1")),
            )
            await instance.build_indices_and_constraints()
            self._instances[group_id] = instance
            return instance

    async def drop(self, group_id: str) -> None:
        """丢弃某图键的缓存实例（图键被整体删除后调用）。"""
        async with self._lock:
            instance = self._instances.pop(group_id, None)
        if instance is not None:
            try:
                await instance.close()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不影响清理结果
                logger.warning("closing graphiti instance for %s failed: %s", group_id, exc)

    async def close(self) -> None:
        for group_id in list(self._instances):
            await self.drop(group_id)


registry = GraphitiRegistry()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await registry.close()


app = FastAPI(title="mem0 graph-bridge", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- helpers


def _parse_reference_time(value: Optional[str]) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning("unparsable reference_time %r; using now", value)
    return datetime.now(timezone.utc)


async def _episode_already_synced(graphiti, uuid: str) -> bool:
    """幂等前置判定：该 uuid 的 Episodic 节点存在且已挂上关系边即视为已同步。"""
    from graphiti_core.errors import NodeNotFoundError
    from graphiti_core.nodes import EpisodicNode

    try:
        episode = await EpisodicNode.get_by_uuid(graphiti.driver, uuid)
    except NodeNotFoundError:
        return False
    return bool(getattr(episode, "entity_edges", None))


async def _count(graphiti, cypher: str) -> int:
    records, _header, _summary = await graphiti.driver.execute_query(cypher)
    if not records:
        return 0
    value = list(records[0].values())[0]
    return int(value or 0)


# --------------------------------------------------------------------------- endpoints


@app.get("/health", summary="Liveness + backend self-check")
async def health() -> Dict[str, Any]:
    """存活 + 后端自证：`backend` 字段固定为 `falkordb`，并顺带探一次图库连通性。"""
    from graphiti_core.driver.falkordb_driver import FalkorDriver

    backend = "falkordb"
    try:
        driver = FalkorDriver(host=FALKORDB_HOST, port=FALKORDB_PORT, database="default_db")
        await driver.execute_query("RETURN 1")
        await driver.close()
        db_status = "ok"
    except Exception as exc:  # noqa: BLE001 - 健康检查永远返回 200，状态体现在字段里
        db_status = f"error: {exc}"
        logger.warning("falkordb health check failed: %s", exc)
    return {"status": "healthy", "backend": backend, "falkordb": db_status}


@app.post("/episodes", response_model=EpisodeResponse, summary="Ingest one fact as an episode")
async def ingest_episode(request: EpisodeRequest) -> EpisodeResponse:
    """把一条事实同步入图；同 uuid 重复调用返回 `already_synced` 且不重复抽取。"""
    from graphiti_core.nodes import EpisodeType, EpisodicNode

    graphiti = await registry.get(request.group_id)

    if await _episode_already_synced(graphiti, request.uuid):
        return EpisodeResponse(status="already_synced", uuid=request.uuid, group_id=request.group_id)

    reference_time = _parse_reference_time(request.reference_time)
    source_description = request.source_description or SOURCE_DESCRIPTION_DEFAULT

    # 先落 episode 节点：uuid 由调用方控制，add_episode 才能以同一 uuid 读取并复用
    # （方案 §5.4 的构造性 join key）。
    episode = EpisodicNode(
        name=request.uuid,
        group_id=request.group_id,
        labels=[],
        source=EpisodeType.text,
        content=request.text,
        source_description=source_description,
        created_at=reference_time,
        valid_at=reference_time,
    )
    episode.uuid = request.uuid
    await episode.save(graphiti.driver)

    result = await graphiti.add_episode(
        uuid=request.uuid,
        name=request.uuid,
        episode_body=request.text,
        source_description=source_description,
        reference_time=reference_time,
        source=EpisodeType.text,
        group_id=request.group_id,
    )
    return EpisodeResponse(
        status="synced",
        uuid=request.uuid,
        group_id=request.group_id,
        nodes=len(result.nodes),
        edges=len(result.edges),
    )


@app.post("/search", response_model=SearchResponse, summary="Search the graph for facts")
async def search_graph(request: SearchRequest) -> SearchResponse:
    """图检索：返回事实及其 `episodes`（即 memory id），顺序即相关度序。"""
    facts: List[FactItem] = []
    for group_id in request.group_ids:
        graphiti = await registry.get(group_id)
        edges = await graphiti.search(
            query=request.query,
            group_ids=[group_id],
            num_results=request.max_facts,
        )
        for edge in edges:
            facts.append(
                FactItem(
                    uuid=str(edge.uuid),
                    name=getattr(edge, "name", "") or "",
                    fact=getattr(edge, "fact", "") or "",
                    valid_at=str(edge.valid_at) if getattr(edge, "valid_at", None) else None,
                    invalid_at=str(edge.invalid_at) if getattr(edge, "invalid_at", None) else None,
                    episodes=[str(episode) for episode in (getattr(edge, "episodes", None) or [])],
                )
            )
        if len(facts) >= request.max_facts:
            break
    return SearchResponse(facts=facts[: request.max_facts])


@app.get("/stats", summary="Graph size for one group")
async def stats(group_id: str) -> Dict[str, Any]:
    """图规模：episodes / 实体节点 / 关系边（单调上升并与新增事实数同阶）。"""
    graphiti = await registry.get(group_id)
    return {
        "group_id": group_id,
        "episodes": await _count(graphiti, "MATCH (n:Episodic) RETURN count(n)"),
        "entity_nodes": await _count(graphiti, "MATCH (n:Entity) RETURN count(n)"),
        "entity_edges": await _count(graphiti, "MATCH ()-[e:RELATES_TO]->() RETURN count(e)"),
    }


@app.get("/graphs", summary="List graph keys")
async def list_graphs() -> Dict[str, Any]:
    """列出 FalkorDB 里的图键（运维面：按作用域清理前的清点）。"""
    from falkordb.asyncio import FalkorDB

    client = FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)
    try:
        return {"graphs": sorted(await client.list_graphs())}
    finally:
        await client.aclose()


@app.delete("/graph/{group_id}", summary="Drop one graph key")
async def drop_graph(group_id: str) -> Dict[str, Any]:
    """整体删除一个图键（隔离验证数据按作用域清理）。"""
    from falkordb.asyncio import FalkorDB

    client = FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)
    try:
        graph = client.select_graph(group_id)
        try:
            await graph.delete()
        except Exception as exc:  # noqa: BLE001 - 图键不存在时也算清理完成
            logger.warning("drop graph %s: %s", group_id, exc)
    finally:
        await client.aclose()
    await registry.drop(group_id)
    return {"group_id": group_id, "dropped": True}


@app.get("/", include_in_schema=False)
async def root() -> Dict[str, str]:
    return {"service": "mem0 graph-bridge"}


if __name__ == "__main__":  # pragma: no cover - 本地直跑用
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
