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
| `POST /backfill` | 存量回填：按 Qdrant 游标顺序取事实、限速入图、可中断续跑 |
| `GET /backfill/status` | 回填进度读数（进程内快照，只读） |

三条实现约定（均来自本机实测，方案 §5.3–5.4）：

1. **join key = episode uuid**：入图前先落一个 `uuid == memory_id` 的 `EpisodicNode`，
   再以同一 uuid 调 `add_episode`；检索结果里的边自带 `episodes` 列表，即 memory id。
2. **`group_id` 即 FalkorDB 图键**：driver 的 database 必须与该次请求的 `group_id`
   一致，否则 `add_episode` 会克隆 driver 去另一个图键读 episode（`NodeNotFoundError`）。
   因此按 `group_id` 缓存 Graphiti 实例。
3. **幂等必须前置判定**：同 uuid 重放 `add_episode` 不报错但会重新抽取（边数增长），
   所以先查该 uuid 的 `Episodic.entity_edges` 是否非空，非空即直接返回 `already_synced`。

存量回填（方案 §5.5）：`POST /backfill` 按 Qdrant 的 `created_at` 游标顺序（与
mem0 侧 `GET /memories` 同一读面口径）扫过某个图键作用域内的存量事实，逐条走与
`/episodes` **完全相同**的入图路径——去重复用它的 `already_synced` 前置判定，不另存
进度表、不另造一套去重。单次调用只处理 `limit` 条，由调用方分批推进；中断（进程重启 /
客户端放弃 / 批次正常结束）后重新调用即可续跑，已入图的事实被判定为 `already_synced`
而跳过且不再消耗 LLM。`rate` 控制入图节奏（每分钟条数），长跑任务因此不长时间占满
LLM 通道；进度由响应字段与 `GET /backfill/status` 双向暴露。

本服务不修改 graphiti-core 一行源码：LLM 客户端走 OpenAI 兼容的 `/chat/completions` 通道
（`OpenAIGenericClient`，见 `_build_llm_client`），embedder 用 OpenAI 兼容客户端并显式指定维度。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
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

# --- 存量回填（方案 §5.5）-----------------------------------------------------
# Qdrant 是事实主存，回填由本服务直读（读面口径与 mem0 侧 `vector_store.list()` 一致：
# `created_at` 降序 + keyset 游标），不经过 mem0 的 HTTP 面：回填是图桥自己的动作，
# 且 mem0 侧列表端点是管理面（鉴权 + 分页参数面向 dashboard）。
#
# 走 Qdrant 的 REST 接口（httpx 已在依赖里）而不是 `qdrant-client`：本镜像的依赖表刻意
# 收窄（见 README「依赖钉版」），而回填只用到 scroll / count 两个读接口，REST 形态稳定且
# 无客户端版本耦合。
QDRANT_HOST = os.environ.get("QDRANT_HOST", "qdrant")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION_NAME", "memories")
# 事实主存是只读面（回填不写 Qdrant），预算给足以覆盖集合扫描的尾部分位。
QDRANT_TIMEOUT_SECONDS = float(os.environ.get("QDRANT_TIMEOUT_SECONDS", "30"))

# 作用域键的优先级与图键前缀：与 SDK 侧 `mem0/memory/graph_sync.py` 同构（图键是两端
# 共享的契约）。图桥镜像不含 SDK 源码，故此处按同一规则复写；两侧只在「归一化字符集」
# 与「优先级」上耦合，改动需同步。
SCOPE_KEYS = ("user_id", "agent_id", "run_id")
GROUP_ID_PREFIX = "mem0"
_GROUP_ID_MAX_LENGTH = 96
_GROUP_ID_INVALID = re.compile(r"[^0-9A-Za-z_-]")

# Dream 观察条目（`server/main.py` 的 `OBSERVATION_KIND`）不入图：增量派发路径只派发
# 写入管道抽取出的事实，观察由 dream 直接写 Qdrant、从不派发（`mem0/memory/dream.py`）。
# 回填按同一口径排除，既保持「图内容 = 增量路径本该产生的内容」，也避免为合成信念
# 消耗抽取用的 LLM 调用。
MEMORY_KIND_OBSERVATION = "observation"

# 单次调用向 Qdrant 取一页的大小；`scan_limit` 是单次调用允许扫过的记录条数上限——
# 已全部入图的作用域重扫一遍时，扫描（而非入图）成为主要成本，需要有界。
BACKFILL_SCAN_PAGE = 64
BACKFILL_MAX_LIMIT = 500
BACKFILL_MAX_SCAN_LIMIT = 5000
# 响应/进度里保留的失败摘要条数（长跑任务里失败可能很多，只留最近若干条便于定位）。
BACKFILL_ERROR_SAMPLES = 5


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


class BackfillRequest(BaseModel):
    """存量回填的一次请求（方案 §5.5 的 `{group_id, limit, rate}`）。

    本端点是**分批推进**的长跑动作：一次调用最多把 `limit` 条事实入图后返回，调用方
    反复调用即可推进全量；任何时刻中断（进程重启 / 客户端放弃 / 批次结束）后重新调用
    都安全——已入图的事实由 `/episodes` 的 `already_synced` 前置判定跳过。
    """

    group_id: str = Field(
        ...,
        description="图键（必须是 SDK 侧派生的 `mem0_<scope>` 形态；回填只覆盖该键作用域内的事实）。",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=BACKFILL_MAX_LIMIT,
        description="本次调用最多**新入图**的事实条数（已同步的跳过不计数）；到此即返回，由调用方续下一批。",
    )
    rate: float = Field(
        default=0.0,
        ge=0.0,
        description="入图速率上限，单位「事实条数/分钟」；`0` 表示不限速（由单条抽取耗时自然限速）。",
    )
    cursor: Optional[str] = Field(
        default=None,
        description="续跑锚点：上一次响应里的 `next_cursor`（记录 `created_at` 原值）。缺省从最新一条开始。",
    )
    scan_limit: int = Field(
        default=200,
        ge=1,
        le=BACKFILL_MAX_SCAN_LIMIT,
        description="本次调用允许扫过的 Qdrant 记录条数上限（含已同步与作用域不匹配的），防止全已同步时无界扫描。",
    )


class BackfillResponse(BaseModel):
    """一次回填调用的计数与续跑锚点（`status` 端点的字段是其子集）。"""

    group_id: str
    processed: int = Field(default=0, description="本次新入图的事实条数。")
    already_synced: int = Field(default=0, description="本次跳过的事实条数（图侧已同步，未耗 LLM）。")
    failed: int = Field(default=0, description="本次入图失败的事实条数。")
    out_of_scope: int = Field(default=0, description="扫到但图键不属于本次 `group_id` 的记录条数（候选过滤的过采样）。")
    scanned: int = Field(default=0, description="本次扫过的 Qdrant 记录条数。")
    exhausted: bool = Field(default=False, description="`true` 表示该作用域的存量已扫到底，没有更多记录。")
    next_cursor: Optional[str] = Field(default=None, description="续跑锚点；配合 `cursor` 使用可从本次扫到的位置继续。")
    elapsed_seconds: float = Field(default=0.0, description="本次调用耗时。")
    paced_wait_seconds: float = Field(default=0.0, description="其中因 `rate` 主动等待的时间（限速生效的直接证据）。")
    facts_per_minute: float = Field(default=0.0, description="本次调用的实测入图速率（`processed / elapsed`）。")
    scope_total: int = Field(default=0, description="估读：该作用域的存量事实总数（Qdrant 精确计数，含候选过采样）。")
    graph_episodes: int = Field(default=0, description="估读：图内 episode 总数。")
    remaining: int = Field(default=0, description="估读剩余量：`scope_total - graph_episodes`（下限 0）。")
    errors: List[str] = Field(default_factory=list, description="最近的失败摘要（最多若干条）。")


# --------------------------------------------------------------------------- clients


def _build_llm_client():
    """装配 LLM 客户端（OpenAI 兼容的 `/chat/completions` 结构化输出通道）。

    选型依据：`OpenAIClient` 在 `_create_structured_completion()` 里硬走 OpenAI 专有的
    `responses.parse()`，本机网关 https://esp.xkw.cn/ai/v1 会把 `/responses` 转发给上游
    provider，而该 provider 只实现 `/chat/completions`，回 400 `missing messages`
    （表现为 `/episodes` 500、`graph_failed` 持续增长而 `graph_synced` 恒为 0）。
    `OpenAIGenericClient` 面向任意 OpenAI 兼容端点，走 `chat.completions.create()`，
    与网关实测互通。曾一度成立的「`/responses` 是本机唯一可用形态」结论随上游 provider
    路由变化而失效，故不再沿用。

    `structured_output_mode` 显式设为 `json_object`：本机网关对 `json_schema` 形态的
    `response_format` 回 422 `This response_format type is unavailable now`
    （`OpenAIGenericClient` 默认即 `json_schema`，故必须显式覆盖），而 `json_object`
    实测通过。该模式下 schema 由客户端注入 prompt 引导，不由 API 强制校验。
    """
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

    config = LLMConfig(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        model=LLM_MODEL,
        small_model=LLM_MODEL,
    )
    return OpenAIGenericClient(config=config, structured_output_mode="json_object")


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


async def _sync_one(
    graphiti,
    group_id: str,
    uuid: str,
    text: str,
    reference_time: Optional[str] = None,
    source_description: Optional[str] = None,
) -> tuple:
    """把一条事实入图，返回 `(status, nodes, edges)`，`status` 为 `synced` / `already_synced`。

    `/episodes` 与 `POST /backfill` 走同一实现：join key 的构造（先落 `uuid == memory_id`
    的 episode，再以同一 uuid 调 `add_episode`）与幂等前置判定只此一处，回填因此**复用**
    `/episodes` 的 `already_synced` 语义而不另造一套去重。抽取异常向上抛出，由调用方按
    失败计数（`/episodes` 转 500，回填记一条失败并继续）。
    """
    from graphiti_core.nodes import EpisodeType, EpisodicNode

    if await _episode_already_synced(graphiti, uuid):
        return "already_synced", 0, 0

    parsed_time = _parse_reference_time(reference_time)
    description = source_description or SOURCE_DESCRIPTION_DEFAULT

    # 先落 episode 节点：uuid 由调用方控制，add_episode 才能以同一 uuid 读取并复用
    # （方案 §5.4 的构造性 join key）。
    episode = EpisodicNode(
        name=uuid,
        group_id=group_id,
        labels=[],
        source=EpisodeType.text,
        content=text,
        source_description=description,
        created_at=parsed_time,
        valid_at=parsed_time,
    )
    episode.uuid = uuid
    await episode.save(graphiti.driver)

    result = await graphiti.add_episode(
        uuid=uuid,
        name=uuid,
        episode_body=text,
        source_description=description,
        reference_time=parsed_time,
        source=EpisodeType.text,
        group_id=group_id,
    )
    return "synced", len(result.nodes), len(result.edges)


# --------------------------------------------------------------------------- backfill helpers


def _sanitize_group_id(value: Any) -> str:
    """把作用域值归一为 graphiti 接受的图键片段（与 SDK 侧同构）。"""
    text = _GROUP_ID_INVALID.sub("_", str(value).strip())
    return text[:_GROUP_ID_MAX_LENGTH]


def _derive_group_id(payload: Dict[str, Any]) -> Optional[str]:
    """由记录 payload 推出它所属的图键；无作用域键时返回 `None`。

    与 `mem0/memory/graph_sync.py` 的 `derive_group_id` 同构（优先级 `user_id` →
    `agent_id` → `run_id`）：Qdrant 侧的候选过滤只能按「作用域值」粗筛（三个键任一相等），
    要判定一条记录究竟属于哪个图键，必须按同一优先级复算——否则 `user_id=other /
    agent_id=xue` 这类记录会被误写进 `mem0_xue`。
    """
    for key in SCOPE_KEYS:
        value = payload.get(key)
        if value:
            return f"{GROUP_ID_PREFIX}_{_sanitize_group_id(value)}"
    return None


def _scope_suffix(group_id: str) -> str:
    """取出图键里的作用域值；不是 `mem0_<scope>` 形态时抛 400（无法定位作用域就无法回填）。"""
    if not group_id.startswith(f"{GROUP_ID_PREFIX}_"):
        raise HTTPException(
            status_code=400,
            detail=f"group_id must be an SDK-derived graph key ('{GROUP_ID_PREFIX}_<scope>'); got {group_id!r}",
        )
    return group_id[len(GROUP_ID_PREFIX) + 1 :]


def _candidate_filter(group_id: str, cursor: Optional[str]) -> Dict[str, Any]:
    """构造回填扫描的 Qdrant 过滤器（候选集，非最终判定），形态即 REST 请求体里的 `filter`。

    * 作用域：三个键任一等于图键后缀——这是**上界**（`should` 取 OR 时高优先级键的
      判定只能事后做，见 `_derive_group_id`）；
    * 排除 `memory_kind == "observation"`（与增量派发口径一致，见模块常量注释）；
    * 游标：`created_at < cursor`（keyset，严格小于），使批次之间不重叠——与 mem0 侧
      `vector_store.list()` 的游标语义一致。

    嵌套 `should` 放进 `must` 是有意的：顶层 `must` + `should` 的语义在各版本间有过
    歧义，嵌套写法在实测中稳定表达「`must` 全真且 `should` 至少一真」（本机 Qdrant
    实测：嵌套 `should` 取不可能值时命中 0，取真实值时与全量计数一致）。
    """
    suffix = _scope_suffix(group_id)
    must: List[Dict[str, Any]] = [
        {"should": [{"key": key, "match": {"value": suffix}} for key in SCOPE_KEYS]},
    ]
    if cursor:
        must.append({"key": "created_at", "range": {"lt": cursor}})
    return {
        "must": must,
        "must_not": [{"key": "memory_kind", "match": {"value": MEMORY_KIND_OBSERVATION}}],
    }


class QdrantFactSource:
    """事实主存的只读访问面（回填取事实用），走 Qdrant REST。

    只暴露回填需要的两个读操作，形态与 mem0 侧 `vector_store.list()` 对齐：
    `created_at` 降序 + keyset 游标（`created_at < cursor`，严格小于）。**只读**：本类
    不提供任何写入方法，回填不改写事实主存。
    """

    def __init__(
        self,
        host: str = QDRANT_HOST,
        port: int = QDRANT_PORT,
        collection: str = QDRANT_COLLECTION,
        timeout_seconds: float = QDRANT_TIMEOUT_SECONDS,
    ) -> None:
        import httpx

        self.collection = collection
        self._client = httpx.AsyncClient(base_url=f"http://{host}:{port}", timeout=timeout_seconds)

    async def scroll(self, group_id: str, cursor: Optional[str], limit: int) -> List[Dict[str, Any]]:
        """取一页事实（每项含 `id` 与 `payload`），按 `created_at` 降序、严格小于游标。"""
        response = await self._client.post(
            f"/collections/{self.collection}/points/scroll",
            json={
                "limit": limit,
                "with_payload": True,
                "with_vector": False,
                "order_by": {"key": "created_at", "direction": "desc"},
                "filter": _candidate_filter(group_id, cursor),
            },
        )
        response.raise_for_status()
        points = response.json().get("result", {}).get("points") or []
        return [{"id": str(point.get("id")), "payload": point.get("payload") or {}} for point in points]

    async def count(self, group_id: str) -> int:
        """该作用域的存量事实条数（精确计数，候选过滤口径）。"""
        response = await self._client.post(
            f"/collections/{self.collection}/points/count",
            json={"exact": True, "filter": _candidate_filter(group_id, None)},
        )
        response.raise_for_status()
        return int(response.json().get("result", {}).get("count") or 0)

    async def aclose(self) -> None:
        await self._client.aclose()


def _build_fact_source() -> QdrantFactSource:
    """构造事实主存只读访问面（测试的替身挂载点）。"""
    return QdrantFactSource()


@dataclass
class BackfillRun:
    """一次回填调用的运行态（进程内，供 `GET /backfill/status` 在长跑中观察）。

    进程内状态即够用：调用方（编排者）与本服务在同一部署里；重启会丢掉「上一次跑到哪」
    的读数，但不会影响续跑的正确性——续跑只依赖图侧的 `already_synced` 判定。
    """

    group_id: str
    limit: int
    rate: float
    scan_limit: int
    cursor: Optional[str]
    started_at: str
    started_monotonic: float
    # 终态时刻：run 到达 completed/interrupted 后冻结，使 `/backfill/status` 对已结束的
    # 调用给出固定的耗时读数（否则它会随查询时间无限增长，`facts_per_minute` 随之衰减）。
    finished_monotonic: Optional[float] = None
    state: str = "running"  # running / completed / interrupted
    processed: int = 0
    already_synced: int = 0
    failed: int = 0
    out_of_scope: int = 0
    scanned: int = 0
    paced_wait_seconds: float = 0.0
    current: Optional[str] = None
    next_cursor: Optional[str] = None
    exhausted: bool = False
    scope_total: int = 0
    graph_episodes: int = 0
    errors: List[str] = field(default_factory=list)

    def elapsed_seconds(self) -> float:
        """本次调用的耗时；到达终态后冻结，不再随查询时间增长。"""
        end = self.finished_monotonic if self.finished_monotonic is not None else time.monotonic()
        return max(0.0, end - self.started_monotonic)

    def finish(self, state: str) -> None:
        """置终态并冻结耗时读数；只认第一次终态（`finally` 与正常返回不会互相覆盖）。"""
        if self.state == "running":
            self.state = state
            self.finished_monotonic = time.monotonic()

    def facts_per_minute(self) -> float:
        elapsed = self.elapsed_seconds()
        return round(self.processed * 60.0 / elapsed, 3) if elapsed > 0 else 0.0

    def remaining_estimate(self) -> int:
        """剩余量估读：作用域事实数 − 图内 episode 数（`scope_total` 在调用开始时取一次，
        之后随本进程入图数推进）。抽取失败留下的空 episode 会被算作已完成，故这是估读。"""
        return max(0, self.scope_total - self.graph_episodes - self.processed)

    def as_payload(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload.pop("started_monotonic", None)
        payload.pop("finished_monotonic", None)
        payload["elapsed_seconds"] = round(self.elapsed_seconds(), 3)
        payload["facts_per_minute"] = self.facts_per_minute()
        payload["remaining"] = self.remaining_estimate()
        return payload


# 每个图键只保留最近一次调用的读数；全服务同时只允许一个回填在跑（见 `backfill`）。
_backfill_runs: Dict[str, BackfillRun] = {}
_backfill_task: Optional[asyncio.Task] = None


def _record_error(run: BackfillRun, message: str) -> None:
    """记一条失败摘要（只留最近若干条，避免长跑任务的响应无限膨胀）。"""
    run.errors.append(message)
    del run.errors[:-BACKFILL_ERROR_SAMPLES]


# --------------------------------------------------------------------------- backfill runner


async def _run_backfill(request: BackfillRequest, run: BackfillRun) -> BackfillResponse:
    """回填的一次调用主体：扫一批、按限速入图、回报计数（见 `backfill` 的说明）。"""
    source = _build_fact_source()
    graphiti = await registry.get(request.group_id)
    # 运行态一进入主体就登记，长跑期间 `/backfill/status` 才有读数可查。
    _backfill_runs[request.group_id] = run
    try:
        # 起点读数：剩余量估读要用，且长跑中调用方从 `/backfill/status` 读到的是同一份基准。
        run.scope_total = await source.count(request.group_id)
        run.graph_episodes = await _count(graphiti, "MATCH (n:Episodic) RETURN count(n)")

        cursor = request.cursor
        interval = 60.0 / request.rate if request.rate > 0 else 0.0
        next_allowed = time.monotonic()

        while run.processed < request.limit and run.scanned < request.scan_limit:
            page_limit = min(BACKFILL_SCAN_PAGE, request.scan_limit - run.scanned)
            points = await source.scroll(request.group_id, cursor, page_limit)
            if not points:
                run.exhausted = True
                break

            for point in points:
                run.scanned += 1
                payload = point.get("payload") or {}
                created_at = payload.get("created_at")
                # 游标推进到本页扫到的最后一条（无论是否入图）：下一批从更深处继续，
                # 已同步的大片区域因此只被走一遍。
                if isinstance(created_at, str) and created_at:
                    cursor = created_at
                    run.next_cursor = created_at

                if _derive_group_id(payload) != request.group_id:
                    # 候选过滤的过采样：作用域值出现在低优先级键上的记录属于别的图键。
                    run.out_of_scope += 1
                    continue

                text = payload.get("data")
                if not isinstance(text, str) or not text.strip():
                    run.failed += 1
                    _record_error(run, f"{point.get('id')}: 事实正文为空，无法入图")
                    continue

                try:
                    # 已同步的先判掉：跳过不消耗 LLM，因此**也不该消耗限速预算**——否则在
                    # 「重扫已入图区域」这条路径上，扫描会被人为压到 rate 的节奏。判据与
                    # `/episodes` 同源（同一 `_episode_already_synced`），`_sync_one` 里还会
                    # 再判一次作为权威门。
                    if await _episode_already_synced(graphiti, str(point.get("id"))):
                        run.already_synced += 1
                        run.current = str(point.get("id"))
                        continue

                    if interval:
                        wait = next_allowed - time.monotonic()
                        if wait > 0:
                            run.paced_wait_seconds += wait
                            await asyncio.sleep(wait)
                    next_allowed = time.monotonic() + interval
                    run.current = str(point.get("id"))

                    status, _nodes, _edges = await _sync_one(
                        graphiti,
                        request.group_id,
                        str(point.get("id")),
                        text,
                        created_at if isinstance(created_at, str) else None,
                    )
                except Exception as exc:  # noqa: BLE001 - 单条失败不终止整批（与 worker 的重试面一致）
                    run.failed += 1
                    _record_error(run, f"{point.get('id')}: {type(exc).__name__}: {exc}")
                    logger.warning("Backfill episode %s failed: %s", point.get("id"), exc)
                    continue

                if status == "already_synced":
                    run.already_synced += 1
                else:
                    run.processed += 1

                # `limit` 只计新入图条数：满额即停在本页中间，剩余的下一页不再扫。
                if run.processed >= request.limit:
                    break

        # 终点读数：剩余量用实际图规模复算，而不是用起点读数加计数推断。
        run.scope_total = await source.count(request.group_id)
        run.graph_episodes = await _count(graphiti, "MATCH (n:Episodic) RETURN count(n)")
    finally:
        await source.aclose()

    run.finish("completed")
    return BackfillResponse(
        group_id=request.group_id,
        processed=run.processed,
        already_synced=run.already_synced,
        failed=run.failed,
        out_of_scope=run.out_of_scope,
        scanned=run.scanned,
        exhausted=run.exhausted,
        next_cursor=run.next_cursor,
        elapsed_seconds=round(run.elapsed_seconds(), 3),
        paced_wait_seconds=round(run.paced_wait_seconds, 3),
        facts_per_minute=run.facts_per_minute(),
        scope_total=run.scope_total,
        graph_episodes=run.graph_episodes,
        remaining=max(0, run.scope_total - run.graph_episodes),
        errors=list(run.errors),
    )


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
    graphiti = await registry.get(request.group_id)
    status, nodes, edges = await _sync_one(
        graphiti,
        request.group_id,
        request.uuid,
        request.text,
        request.reference_time,
        request.source_description,
    )
    return EpisodeResponse(
        status=status,
        uuid=request.uuid,
        group_id=request.group_id,
        nodes=nodes,
        edges=edges,
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


@app.post("/backfill", response_model=BackfillResponse, summary="Backfill stored facts into the graph")
async def backfill(request: BackfillRequest) -> BackfillResponse:
    """按 Qdrant 游标顺序回填某个作用域的存量事实（方案 §5.5）。

    语义（细节按本实现补充，契约形态按方案 §5.5）：

    * **遍历顺序**：`created_at` 降序 + keyset 游标（与 mem0 侧 `vector_store.list()`
      同一读面口径），批次之间用严格小于的游标衔接，因此不重复、不遗漏；
    * **限速**：`rate` 是「事实条数/分钟」的上限，实现为相邻两次**入图**的起始时间间隔不小于
      `60 / rate` 秒；`rate=0` 表示不限速（单条抽取本身的耗时即天然上限）。已同步的跳过不
      消耗 LLM，因此也不消耗限速预算（重扫已入图区域不会被压到 `rate` 的节奏）；
    * **幂等续跑**：入图走 `/episodes` 的同一实现（`_sync_one`），因此复用它的
      `already_synced` 前置判定——已入图的事实只花一次图内查询即跳过，不再抽一次；
    * **可中断**：单次调用最多新入图 `limit` 条、最多扫过 `scan_limit` 条，返回即结束；
      进程重启、客户端放弃、批次结束都只留下「已入图的部分」这一可续的状态。响应里的
      `next_cursor` 是本次扫到的最深处，传回 `cursor` 可接着往下扫；不传则从头重扫
      （安全但更慢，因为要重新走过已入图的部分）。

    `limit` 只计**新入图**条数，已同步的跳过不计数——因此「连续调用直到 `processed == 0`
    且 `exhausted == true`」即可推进到全量完成。长跑调用请把客户端超时设到
    `limit × 单条耗时 + 限速等待` 的量级（单条实测 15–101s，见方案 §6.5）。

    并发：全服务同时只允许一个回填（回填与图派发共用同一 LLM 通道，并发回填会同时压
    LLM 与 2 vCPU 的 VM），冲突时返回 409；进度读见 `GET /backfill/status`。
    """
    global _backfill_task

    # 先做形态校验：非 SDK 派生形态（`mem0_<scope>`）的键定位不到作用域，且若先
    # `registry.get()` 会在 FalkorDB 里惰性建出这个空图键（「读即建键」副作用），
    # 因此校验必须发生在任何副作用之前。
    _scope_suffix(request.group_id)

    # 单飞判定与置位之间没有 await，因此在单进程事件循环里是原子的。
    current = asyncio.current_task()
    if _backfill_task is not None and not _backfill_task.done():
        running = next((run for run in _backfill_runs.values() if run.state == "running"), None)
        detail = f"another backfill run is in progress (group_id={running.group_id})" if running else "another backfill run is in progress"
        raise HTTPException(status_code=409, detail=detail)
    _backfill_task = current

    run = BackfillRun(
        group_id=request.group_id,
        limit=request.limit,
        rate=request.rate,
        scan_limit=request.scan_limit,
        cursor=request.cursor,
        started_at=datetime.now(timezone.utc).isoformat(),
        started_monotonic=time.monotonic(),
    )
    try:
        return await _run_backfill(request, run)
    finally:
        # 正常返回时已置 completed；被取消（客户端放弃 / 服务关停）或异常时留 interrupted，
        # 部分进度仍可从 `/backfill/status` 读到。两种情况都冻结耗时读数（`finish` 只认首次）。
        run.finish("interrupted")
        _backfill_task = None


@app.get("/backfill/status", summary="Backfill progress")
async def backfill_status(group_id: Optional[str] = None) -> Dict[str, Any]:
    """回填进度读数（只读，进程内快照）。

    长跑任务（单批 `limit` 条可达数十分钟）在调用进行中也能读到实时计数，因此不必等
    响应返回才知道「跑到哪了」。`running: true` 表示当前有调用在跑；`remaining` 是
    估读（作用域事实数 − 图内 episode 数 − 本进程已入图数）。

    快照是**进程内**的：图桥重启后这里只剩空表，续跑本身不受影响（正确性只依赖图侧的
    `already_synced` 判定）。不传 `group_id` 时返回全部图键的最近一次读数。
    """
    runs = (
        [run.as_payload() for run in _backfill_runs.values() if run.group_id == group_id]
        if group_id
        else [run.as_payload() for run in _backfill_runs.values()]
    )
    payload: Dict[str, Any] = {
        "runs": runs,
        "running": bool(_backfill_task is not None and not _backfill_task.done()),
    }
    return payload


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
