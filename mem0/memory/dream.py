"""Dream：后台记忆整合（Orient → Gather → Consolidate → Prune）。

设计见 `docs/design/memory-dream.md`。本模块把一组彼此相关的既有事实精炼成一条
更高层的「观察」（observation），写回同一向量集合，并保留正反双向的证据链。

四阶段严格单向，职责互不重叠：

| 阶段 | 职责 | 是否调用 LLM | 是否写数据 |
| --- | --- | --- | --- |
| Orient | 枚举作用域、汇总存量口径、收集短路基准 | 否 | 否 |
| Gather | 作用域内向量聚类、算簇指纹、已判定短路 | 否 | 否 |
| Consolidate | 每簇一次 LLM 调用，产出至多一条观察候选 | 是 | 否 |
| Prune | 剪枝判定 + 落地观察 + 处置被取代的旧观察 | 否 | **是（唯一写入点）** |

两条不变量贯穿全模块：

* **源事实零写**：事实条目（payload 无 `memory_kind` 的记录）在任何阶段都不产生
  Qdrant 写操作，也不写 history 行。被写入的只有观察条目。
* **幂等**：观察的 point id 由簇成员指纹确定性派生（`uuid5`），同簇重复落地是覆写
  而非追加；`Prune` 的全部判定为纯函数，同输入同输出。

`run_dream()` 是本模块唯一的编排入口；`dry_run` 与实跑共用同一套判定与 LLM 调用，
差别只有两点：不执行落地写入、不落 `dream_runs` 行。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Set, Tuple

from mem0.configs.prompts import (
    DREAM_MAX_CLUSTER_SIZE,
    DREAM_MAX_CLUSTERS_PER_RUN,
    DREAM_MIN_CLUSTER_SIZE,
    DREAM_TAU,
    INVALID_REASON_OBSERVATION_RECOMPUTED,
    MEMORY_KIND_OBSERVATION,
    OBSERVATION_SYNTHESIS_PROMPT,
    generate_observation_synthesis_prompt,
)
from mem0.memory.utils import extract_json, remove_code_blocks

logger = logging.getLogger(__name__)

# 观察 payload 中除文本与时间以外的自有字段（读路径提升为一等字段的清单见
# `mem0.memory.main.OBSERVATION_PAYLOAD_KEYS`）。
OBSERVATION_MEMBER_KEY = "observation_key"
OBSERVATION_SOURCE_KEY = "source_memory_ids"
OBSERVATION_EVIDENCE_KEY = "evidence_count"
OBSERVATION_RUN_KEY = "dream_run_id"

# 观察演化的判定阈值：新簇与既有观察的成员集合 Jaccard 相似度 ≥ 0.6 且指纹不同，
# 即视为同一支观察的演化（设计 §4.2）。
OBSERVATION_SUPERSEDE_JACCARD = 0.6

# 全量扫描的分页大小（Orient 的 payload 扫描与 Gather 的向量扫描共用）。
SCROLL_PAGE_SIZE = 1000

# `Prune` 的判定取值（写入状态表与报告的 `decision` / `skip_reason`）。
DECISION_WRITTEN = "written"
SKIP_NO_PATTERN = "no_higher_order_pattern"
SKIP_UNRESOLVABLE = "unresolvable_sources"
SKIP_INSUFFICIENT = "insufficient_evidence"
SKIP_DUPLICATE_TEXT = "duplicate_text"
SKIP_FAILED = "failed"
SKIP_ALREADY_EVALUATED = "already_evaluated"
SKIP_DEFERRED = "deferred"

_MODE_DRY_RUN = "dry_run"
_MODE_LIVE = "live"


class DreamUnsupportedStoreError(RuntimeError):
    """当前向量存储无法提供聚类所需的向量扫描能力时抛出。"""


# ---------------------------------------------------------------------------
# 配置与状态存储协议
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DreamSettings:
    """一轮整合的全部可调参数（设计 §5.2.2、§5.6）。

    默认值取自设计文档的参数扫描结论；部署可通过环境变量覆盖（见
    `server/dream_scheduler.py` 的 `DreamSettings.from_env`）。
    """

    tau: float = DREAM_TAU
    min_cluster_size: int = DREAM_MIN_CLUSTER_SIZE
    max_cluster_size: int = DREAM_MAX_CLUSTER_SIZE
    max_clusters_per_run: int = DREAM_MAX_CLUSTERS_PER_RUN
    # 单簇 LLM 调用超时；超时按失败处理（不入状态表，下轮重试）。
    per_cluster_timeout_seconds: float = 60.0
    # 整轮超时；超时后中断 Consolidate，已落地的观察保留。
    run_timeout_seconds: float = 1800.0
    report_dir: str = "/app/history/dream-reports"


class DreamClusterStateStore(Protocol):
    """「已判定的簇」状态表：只影响成本，不参与正确性判定（设计 §3.3）。

    表数据丢失只导致重复调用 LLM，结果不变——因此这里的实现允许整体缺失
    （退化行为见 `_NullStateStore`）。
    """

    def known_keys(self) -> Set[str]:
        """返回历史上判定过的全部簇指纹。"""
        ...

    def record(
        self,
        *,
        observation_key: str,
        scope_user_id: str,
        scope_agent_id: str,
        decision: str,
        observed_point_id: Optional[str] = None,
    ) -> None:
        """写入或更新一行（同一 key 再次判定时以最新判定为准）。"""
        ...

    def touch(self, *, observation_key: str) -> None:
        """标记「该簇本轮被再次判定过」：只更新 `last_evaluated_at` 与 `evaluations`。

        用于 G1 已判定短路的簇——它不进 Consolidate/Prune（因此 `decision` 不变），但
        本轮确实又判定了一次它不需要重算（设计 §10.3 的 [AC-16]）。
        """
        ...


class DreamObserver(Protocol):
    """本轮运行的生命周期挂载点（设计 §4.3.1）。

    实跑由服务侧传入一个把统计写入 `dream_runs` 的实现；dry-run 传 None——dry-run 不落
    任何运行行（§5.7）。SDK 直连调用无审计载体时同样传 None。
    """

    def start_run(self, *, run_id: str, mode: str, started_at: str) -> None:
        """登记一轮运行的开端。"""
        ...

    def finish_run(self, *, run_id: str, status: str, stats: Dict[str, Any], report_path: Optional[str]) -> None:
        """写入一轮运行的终态与统计。"""
        ...


class _NullStateStore:
    """状态表不可用时的退化实现：短路只剩「已落地观察」一层（设计 §5.2.3 G3）。"""

    def known_keys(self) -> Set[str]:
        return set()

    def record(self, **_kwargs: Any) -> None:
        return None

    def touch(self, **_kwargs: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# 标识派生（纯函数）
# ---------------------------------------------------------------------------


def observation_key(user_id: str, agent_id: str, member_ids: Iterable[str]) -> str:
    """簇成员指纹：作用域 + 成员 id 升序拼接的 sha256 前 32 位（设计 §4.2）。

    分隔符用 ASCII 单元分隔符（`\\x1f`）而非直接拼接，避免相邻 id 边界歧义导致
    不同成员集合得到同一指纹；同一组源事实恒得同一 key。
    """
    payload = "\x1f".join([str(user_id), str(agent_id), *sorted(str(mid) for mid in member_ids)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def observation_point_id(key: str) -> str:
    """由指纹确定性派生 point id：`uuid5(NAMESPACE_URL, key)`（设计 §4.2）。

    幂等完全依赖这一点：同一簇重复落地得到同一 point id，Qdrant 的 upsert 是覆写，
    集合条目总数不增。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def _member_text(member: Dict[str, Any]) -> str:
    return str(member.get("text") or "")


def _jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    left_set, right_set = set(left), set(right)
    if not left_set and not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def _date_part(value: Any) -> Optional[str]:
    """取 ISO8601 字符串 / date / datetime 的日期部分；不可解析时返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _parse_instant(value: Any) -> Optional[datetime]:
    """把时间戳解析成带时区的 UTC 时刻；不可解析返回 None。"""
    if isinstance(value, datetime):
        parsed = value
    elif value is None:
        return None
    else:
        text = str(value).strip()
        if not text:
            return None
        if text[-1] in ("Z", "z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 扫描工具（只读）
# ---------------------------------------------------------------------------


def _unwrap_rows(listed: Any) -> List[Any]:
    """兼容向量存储 `list()` 的多种返回形态（见 `server/main.py` 的同名处理）。"""
    if isinstance(listed, (list, tuple)) and listed and isinstance(listed[0], (list, tuple)):
        return list(listed[0])
    return list(listed or [])


def _iter_payloads(vector_store: Any) -> List[Any]:
    """按 `created_at` 游标遍历集合全部记录（只读，keyset 分页）。

    `Qdrant.list()` 本身就是 newest-first 的 keyset 分页，用上一页末条的
    `created_at` 作下一页游标即可覆盖全量；游标为空表示已到末页。
    """
    rows: List[Any] = []
    cursor: Optional[str] = None
    while True:
        page = _unwrap_rows(vector_store.list(top_k=SCROLL_PAGE_SIZE, cursor=cursor))
        if not page:
            break
        rows.extend(page)
        last_created = (getattr(page[-1], "payload", None) or {}).get("created_at")
        if not last_created or len(page) < SCROLL_PAGE_SIZE:
            break
        cursor = str(last_created)
    return rows


def _point_vector(point: Any) -> Optional[List[float]]:
    """取出点的稠密向量；命名向量形态（`{"": [...]}`）与裸数组都接受。"""
    vector = getattr(point, "vector", None)
    if isinstance(vector, dict):
        vector = vector.get("") or next(iter(vector.values()), None)
    if vector is None:
        return None
    return list(vector)


def _scan_scope_records(vector_store: Any, scope_filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    """一次取回某个作用域的全部成员（id / payload / 向量），供聚类使用。

    这是本模块唯一依赖具体向量存储实现的地方：Gather 需要已入库向量，只有能
    直接遍历点并附带向量的存储才可用（Qdrant 经 `client.scroll`）。其余阶段只
    依赖 `VectorStoreBase` 的接口。
    """
    client = getattr(vector_store, "client", None)
    build_filter = getattr(vector_store, "_create_filter", None)
    if client is None or build_filter is None:
        raise DreamUnsupportedStoreError(
            "Dream 的 Gather 阶段需要能遍历向量集合的存储实现（Qdrant 的 client.scroll）。"
        )

    query_filter = build_filter(scope_filters)
    records: List[Dict[str, Any]] = []
    offset: Any = None
    while True:
        points, next_offset = client.scroll(
            collection_name=vector_store.collection_name,
            scroll_filter=query_filter,
            limit=SCROLL_PAGE_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        for point in points:
            payload = dict(getattr(point, "payload", None) or {})
            records.append(
                {
                    "id": str(point.id),
                    "payload": payload,
                    "text": payload.get("data", ""),
                    "created_at": payload.get("created_at"),
                    "valid_at": payload.get("valid_at"),
                    "vector": _point_vector(point),
                }
            )
        if next_offset is None:
            break
        offset = next_offset
    return records


# ---------------------------------------------------------------------------
# 阶段一：Orient（只读）
# ---------------------------------------------------------------------------


@dataclass
class OrientResult:
    """Orient 的输出（设计 §5.1.2）。"""

    scopes: List[Dict[str, Any]] = field(default_factory=list)
    totals: Dict[str, Any] = field(default_factory=dict)
    known_evaluated_keys: Set[str] = field(default_factory=set)
    state_table_available: bool = True
    # 不在报告里出现、只在本轮内部使用的派生数据：
    #   全部既有 hash（P4 文本去重）、已落地观察索引（P11 演化处置）。
    existing_hashes: Set[str] = field(default_factory=set)
    observations: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def as_report(self) -> Dict[str, Any]:
        return {
            "scopes": [dict(scope) for scope in self.scopes],
            "totals": dict(self.totals),
            "known_evaluated_keys": len(self.known_evaluated_keys),
            "state_table_available": self.state_table_available,
        }


def orient(
    vector_store: Any,
    *,
    state_store: Optional[DreamClusterStateStore] = None,
    scope_filter: Optional[Sequence[Tuple[str, str]]] = None,
) -> OrientResult:
    """阶段一：枚举作用域、汇总存量口径、收集本轮的短路基准。

    O1 作用域 = `(user_id, agent_id)` 二元组，任一字段缺失的记录跳过并计入
    `skipped_records`；O2 汇总事实 / 观察 / 跳过的记录数；O3 短路基准 = 状态表
    已判定 key ∪ 集合内已落地观察的 key。全程只读，不调用 LLM，不做任何写。
    """
    try:
        known = set(state_store.known_keys()) if state_store is not None else set()
        state_available = state_store is not None
    except Exception as exc:  # 状态表不可用 → 退化为只按已落地观察短路（G3）
        logger.warning(f"Dream state table unavailable, falling back to landed observations only: {exc}")
        known, state_available = set(), False

    allowed = set(scope_filter) if scope_filter else None
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    existing_hashes: Set[str] = set()
    observations: Dict[str, Dict[str, Any]] = {}
    facts = 0
    observation_count = 0
    skipped_records = 0

    for row in _iter_payloads(vector_store):
        payload = getattr(row, "payload", None) or {}
        point_id = str(getattr(row, "id", ""))
        payload_hash = payload.get("hash")
        if payload_hash:
            existing_hashes.add(str(payload_hash))

        kind = payload.get("memory_kind")
        if kind == MEMORY_KIND_OBSERVATION:
            observation_count += 1
            key = payload.get(OBSERVATION_MEMBER_KEY)
            if key:
                known.add(str(key))
                observations[str(key)] = {
                    "point_id": point_id,
                    "scope_user_id": payload.get("user_id"),
                    "scope_agent_id": payload.get("agent_id"),
                    "members": [str(mid) for mid in (payload.get(OBSERVATION_SOURCE_KEY) or [])],
                    "created_at": payload.get("created_at"),
                    "invalid_at": payload.get("invalid_at"),
                }
        else:
            facts += 1

        user_id = payload.get("user_id")
        agent_id = payload.get("agent_id")
        if not user_id or not agent_id:
            skipped_records += 1
            continue
        scope = (str(user_id), str(agent_id))
        if allowed is not None and scope not in allowed:
            continue
        bucket = buckets.setdefault(scope, {"user_id": scope[0], "agent_id": scope[1], "facts": 0, "observations": 0})
        bucket["observations" if kind == MEMORY_KIND_OBSERVATION else "facts"] += 1

    scopes = [buckets[scope] for scope in sorted(buckets)]
    return OrientResult(
        scopes=scopes,
        totals={
            "facts": facts,
            "observations": observation_count,
            "skipped_records": skipped_records,
            "scopes": len(scopes),
        },
        known_evaluated_keys=known,
        state_table_available=state_available,
        existing_hashes=existing_hashes,
        observations=observations,
    )


# ---------------------------------------------------------------------------
# 阶段二：Gather（只读、确定性纯函数）
# ---------------------------------------------------------------------------


def cluster_members(
    members: Sequence[Dict[str, Any]],
    *,
    tau: float = DREAM_TAU,
    min_cluster_size: int = DREAM_MIN_CLUSTER_SIZE,
    max_cluster_size: int = DREAM_MAX_CLUSTER_SIZE,
) -> List[List[str]]:
    """贪心种子扩张聚类（设计 §5.2.1）。纯函数：不含 LLM、不含 I/O、同输入同输出。

    处理顺序按「与该作用域内全部成员的余弦相似度之和」降序（中心性优先），同值按
    成员 id 升序；以未归组的首个成员为种子，收集相似度 ≥ `tau` 的未归组成员；候选
    数不足 `min_cluster_size` 时种子单独归组（不成簇），否则按相似度降序取前
    `max_cluster_size` 个成员成簇并整簇标记已归组，直至全部成员归组。

    Args:
        members: 作用域成员，每项含 `id` 与 `vector`（等长稠密向量）。
        tau: 余弦相似度阈值。
        min_cluster_size: 成簇的最小成员数（含种子）。
        max_cluster_size: 单簇成员数上限。

    Returns:
        簇列表，每簇为成员 id 升序列表；簇间顺序为种子处理顺序（确定性）。
    """
    usable = [m for m in members if m.get("vector") is not None]
    if len(usable) < min_cluster_size:
        return []

    import numpy as np

    ids = [str(m["id"]) for m in usable]
    matrix = np.asarray([m["vector"] for m in usable], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    # 零向量（不应出现，但线上数据不可信）归一化时置 1，使其相似度为 0 而不是 NaN。
    norms[norms == 0] = 1.0
    normalized = matrix / norms

    # 中心性 = 与全部成员的余弦相似度之和；只按行取用，不物化 N×N 矩阵。
    centrality = normalized @ normalized.sum(axis=0)
    order = sorted(range(len(ids)), key=lambda idx: (-float(centrality[idx]), ids[idx]))

    assigned = [False] * len(ids)
    clusters: List[List[str]] = []
    for seed in order:
        if assigned[seed]:
            continue
        similarities = normalized @ normalized[seed]
        candidates = [idx for idx in range(len(ids)) if not assigned[idx] and float(similarities[idx]) >= tau]
        if len(candidates) < min_cluster_size:
            assigned[seed] = True
            continue
        candidates.sort(key=lambda idx: (-float(similarities[idx]), ids[idx]))
        chosen = candidates[:max_cluster_size]
        for idx in chosen:
            assigned[idx] = True
        clusters.append(sorted(ids[idx] for idx in chosen))
    return clusters


def gather(
    vector_store: Any,
    scope: Dict[str, Any],
    *,
    settings: DreamSettings,
    known_keys: Set[str],
) -> Dict[str, Any]:
    """阶段二：把一个作用域聚成候选簇，并算出每簇的成员指纹与短路标记。

    只读、不调用 LLM。`skipped_reason = "already_evaluated"` 的簇不进入
    Consolidate（G1：状态表命中，含上一轮判定为「提炼不出」的簇）；成员集合变化
    会得到新指纹，从而正常进入 Consolidate（G2）。
    """
    scope_filters = {"user_id": scope["user_id"], "agent_id": scope["agent_id"]}
    records = _scan_scope_records(vector_store, scope_filters)
    facts = [record for record in records if record["payload"].get("memory_kind") != MEMORY_KIND_OBSERVATION]

    clusters: List[Dict[str, Any]] = []
    for member_ids in cluster_members(
        facts,
        tau=settings.tau,
        min_cluster_size=settings.min_cluster_size,
        max_cluster_size=settings.max_cluster_size,
    ):
        key = observation_key(scope["user_id"], scope["agent_id"], member_ids)
        clusters.append(
            {
                "scope": {"user_id": scope["user_id"], "agent_id": scope["agent_id"]},
                "members": member_ids,
                OBSERVATION_MEMBER_KEY: key,
                "skipped_reason": SKIP_ALREADY_EVALUATED if key in known_keys else None,
            }
        )
    return {"scope": scope, "records": records, "clusters": clusters}


def _cluster_sort_key(cluster: Dict[str, Any]) -> Tuple[int, str]:
    """簇处理顺序：成员数降序、同规模按指纹字典序升序（确定性）。"""
    return (-len(cluster["members"]), cluster[OBSERVATION_MEMBER_KEY])


# ---------------------------------------------------------------------------
# 阶段三：Consolidate（每簇一次 LLM 调用）
# ---------------------------------------------------------------------------


def parse_observation_response(response: Any) -> Dict[str, Any]:
    """解析观察合成输出。

    Returns:
        `{"text": str | None, "source_ids": [...], "counterexample": [...]}`；
        `text` 为 None 表示该簇提炼不出更高层结论（合法结论，不是失败）。

    Raises:
        ValueError: 输出不可解析（该簇记 failed，不入状态表，下轮重试）。
    """
    if response is None or not str(response).strip():
        raise ValueError("empty observation synthesis response")
    raw = str(response)
    try:
        parsed = json.loads(remove_code_blocks(raw), strict=False)
    except json.JSONDecodeError:
        parsed = json.loads(extract_json(remove_code_blocks(raw)), strict=False)

    if not isinstance(parsed, dict):
        raise ValueError("observation synthesis response is not a JSON object")
    observation = parsed.get("observation")
    if observation is None:
        return {"text": None, "source_ids": [], "counterexample": []}
    if not isinstance(observation, dict):
        raise ValueError("'observation' is neither null nor an object")

    text = observation.get("text")
    if text is None or not str(text).strip():
        return {"text": None, "source_ids": [], "counterexample": []}
    source_ids = observation.get("source_ids")
    counterexample = observation.get("counterexample")
    return {
        "text": str(text).strip(),
        "source_ids": [str(sid) for sid in source_ids] if isinstance(source_ids, list) else [],
        "counterexample": [str(sid) for sid in counterexample] if isinstance(counterexample, list) else [],
    }


class _UsageSink:
    """只统计 Dream 自身 LLM 调用的 token 用量。

    既有的响应回调挂在共享的 LLM 配置上，而整合线程与请求线程共用同一个 LLM 实例；
    因此这里按 system prompt 精确匹配，只累计观察合成调用，避免把并发的 `add()`
    调用（1a / 1b）算进本轮成本。
    """

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def __call__(self, _llm: Any, response: Any, params: Dict[str, Any]) -> None:
        messages = (params or {}).get("messages") or []
        if not messages or messages[0].get("content") != OBSERVATION_SYNTHESIS_PROMPT:
            return
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
        self.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)


class _usage_sink_installed:
    """上下文管理器：在本轮运行期间安装用量回调，退出时原样恢复。"""

    def __init__(self, llm: Any, sink: _UsageSink) -> None:
        self.config = getattr(llm, "config", None)
        self.sink = sink
        self.previous: Any = None
        self.installed = False

    def __enter__(self) -> _UsageSink:
        if self.config is None or not hasattr(self.config, "response_callback"):
            return self.sink
        self.previous = getattr(self.config, "response_callback", None)
        self.config.response_callback = self.sink
        self.installed = True
        return self.sink

    def __exit__(self, *_exc: Any) -> None:
        if self.installed and self.config is not None:
            self.config.response_callback = self.previous


def synthesize_observation(
    llm: Any,
    members: Sequence[Dict[str, Any]],
    *,
    executor: Optional[ThreadPoolExecutor] = None,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """对单簇发起一次 LLM 调用，返回观察候选（设计 §5.3）。

    一簇一调用、串行；输入只含成员的 `id` / `text` / `created_at` 三项。调用异常、
    超时与不可解析输出都由调用方按「该簇失败」处理，不向上抛出——单簇失败绝不中断
    整轮。
    """
    prompt = generate_observation_synthesis_prompt(members=members)
    messages = [
        {"role": "system", "content": OBSERVATION_SYNTHESIS_PROMPT},
        {"role": "user", "content": prompt},
    ]

    def _call() -> Any:
        return llm.generate_response(messages=messages, response_format={"type": "json_object"})

    if executor is not None and timeout_seconds:
        # 阻塞式 provider 调用无法从外部取消：超时只是放弃等待，后台线程随进程结束回收。
        future = executor.submit(_call)
        try:
            response = future.result(timeout=timeout_seconds)
        except FutureTimeoutError as exc:
            raise TimeoutError(f"per-cluster timeout after {timeout_seconds}s") from exc
    else:
        response = _call()
    return parse_observation_response(response)


# ---------------------------------------------------------------------------
# 阶段四：Prune（唯一改变记忆数据的阶段）
# ---------------------------------------------------------------------------


def prune_candidate(
    candidate: Dict[str, Any],
    *,
    member_ids: Sequence[str],
    existing_hashes: Iterable[str],
    min_cluster_size: int,
) -> Dict[str, Any]:
    """P1-P5 剪枝判定（纯函数，不产生任何写入）。

    | 规则 | 语义 |
    | --- | --- |
    | P1 | `text` 为空 / 空白 → `no_higher_order_pattern` |
    | P2 | `source_ids` 不属于该簇成员的 id 一律剔除；剔除后为空 → `unresolvable_sources` |
    | P3 | 剔除后 `source_ids` 长度 < `min_cluster_size` → `insufficient_evidence` |
    | P4 | 观察文本 md5 命中既有 hash（事实或观察）→ `duplicate_text` |
    | P5 | `evidence_count` 一律等于落地时源列表长度，不由 LLM 提供 |

    Returns:
        `{"decision", "skip_reason", "source_ids", "evidence_count", "text"}`。
    """
    text = candidate.get("text")
    if text is None or not str(text).strip():
        return {"decision": SKIP_NO_PATTERN, "skip_reason": SKIP_NO_PATTERN, "source_ids": [], "evidence_count": 0, "text": None}

    text = str(text).strip()
    allowed = {str(mid) for mid in member_ids}
    source_ids = [str(sid) for sid in (candidate.get("source_ids") or [])]
    kept, seen = [], set()
    for sid in source_ids:
        if sid in allowed and sid not in seen:
            kept.append(sid)
            seen.add(sid)
    if not kept:
        logger.warning("Dream candidate dropped: no source id belongs to its cluster (hallucinated ids)")
        return {"decision": SKIP_UNRESOLVABLE, "skip_reason": SKIP_UNRESOLVABLE, "source_ids": [], "evidence_count": 0, "text": text}
    if len(kept) < min_cluster_size:
        return {"decision": SKIP_INSUFFICIENT, "skip_reason": SKIP_INSUFFICIENT, "source_ids": kept, "evidence_count": len(kept), "text": text}

    text_hash = hashlib.md5(text.encode()).hexdigest()
    if text_hash in set(existing_hashes):
        return {"decision": SKIP_DUPLICATE_TEXT, "skip_reason": SKIP_DUPLICATE_TEXT, "source_ids": kept, "evidence_count": len(kept), "text": text}

    return {"decision": DECISION_WRITTEN, "skip_reason": None, "source_ids": kept, "evidence_count": len(kept), "text": text}


def build_observation_payload(
    *,
    decision: Dict[str, Any],
    scope: Dict[str, Any],
    members: Sequence[Dict[str, Any]],
    run_id: str,
    created_at: str,
) -> Dict[str, Any]:
    """观察条目的完整 payload（设计 §5.4.2 的 P7 / P10）。

    `valid_at` 取该簇成员的**最早生效时间**（成员 `valid_at` 缺失时以其 `created_at`
    的日期兜底），使 point-in-time 查询在成员覆盖的时间区间内能命中该观察。
    """
    from mem0.utils.lemmatization import lemmatize_for_bm25

    text = decision["text"]
    effective_dates = [
        _date_part(member.get("valid_at")) or _date_part(member.get("created_at")) for member in members
    ]
    effective_dates = [value for value in effective_dates if value]
    payload: Dict[str, Any] = {
        "data": text,
        "hash": hashlib.md5(text.encode()).hexdigest(),
        "text_lemmatized": lemmatize_for_bm25(text),
        "memory_kind": MEMORY_KIND_OBSERVATION,
        OBSERVATION_MEMBER_KEY: decision[OBSERVATION_MEMBER_KEY],
        OBSERVATION_SOURCE_KEY: list(decision["source_ids"]),
        OBSERVATION_EVIDENCE_KEY: decision["evidence_count"],
        OBSERVATION_RUN_KEY: run_id,
        "user_id": scope["user_id"],
        "agent_id": scope["agent_id"],
        "created_at": created_at,
        "updated_at": created_at,
        "valid_at": min(effective_dates) if effective_dates else _date_part(created_at),
    }
    return payload


def _select_superseded(
    *,
    new_key: str,
    new_members: Sequence[str],
    new_point_id: str,
    new_created_at: str,
    observations: Dict[str, Dict[str, Any]],
    scope: Dict[str, Any],
    already_disposed: Set[str],
) -> List[Dict[str, Any]]:
    """P11/P12：挑出本轮需要写失效标记的旧观察（纯函数，判定与写入分离）。

    命中条件：同作用域、指纹不同、成员集合 Jaccard ≥ 0.6、尚未失效、且本轮内未被
    处置过（一条旧观察只被处置一次，其失效标记即为最终值）。
    """
    targets: List[Tuple[float, str, Dict[str, Any]]] = []
    for key, record in observations.items():
        if key == new_key or key in already_disposed:
            continue
        if record.get("invalid_at"):
            continue
        if str(record.get("scope_user_id")) != str(scope["user_id"]):
            continue
        if str(record.get("scope_agent_id")) != str(scope["agent_id"]):
            continue
        similarity = _jaccard(record.get("members") or [], new_members)
        if similarity >= OBSERVATION_SUPERSEDE_JACCARD:
            targets.append((similarity, key, record))

    targets.sort(key=lambda item: (-item[0], item[1]))
    date_part = _date_part(new_created_at)
    return [
        {
            OBSERVATION_MEMBER_KEY: key,
            "point_id": record.get("point_id"),
            "superseded_by": new_point_id,
            "invalid_at": date_part,
            "invalid_reason": INVALID_REASON_OBSERVATION_RECOMPUTED,
        }
        for _similarity, key, record in targets
    ]


def _apply_supersede(memory: Any, disposed: Sequence[Dict[str, Any]], test_mode: bool) -> int:
    """对旧观察做 payload-only 更新：只写 `invalid_at` / `superseded_by` / `invalid_reason`。

    文本、hash、时间字段与向量一律不动（与 bi-temporal 1c 同构）。dry-run 只返回
    判定结果，不执行写入。
    """
    if test_mode:
        return len(disposed)
    written = 0
    for item in disposed:
        payload = {
            "invalid_at": item["invalid_at"],
            "superseded_by": item["superseded_by"],
            "invalid_reason": item["invalid_reason"],
        }
        try:
            memory.vector_store.update(vector_id=item["point_id"], vector=None, payload=payload)
            written += 1
        except Exception as exc:
            logger.error(f"Dream supersede failed for observation {item['point_id']}: {exc}")
    return written


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


def _parse_scope_spec(spec: str) -> Optional[Tuple[str, str]]:
    """解析手工指定的作用域：`user_id` 或 `user_id:agent_id`。"""
    text = str(spec).strip()
    if not text:
        return None
    if ":" in text:
        user_id, agent_id = text.split(":", 1)
        return (user_id.strip(), agent_id.strip())
    return None


def _scope_filter_from_specs(specs: Optional[Sequence[str]]) -> Optional[Set[Tuple[str, str]]]:
    """把 `scopes` 参数解析成作用域集合；只给 `user_id` 时按前缀匹配其全部 agent。"""
    if not specs:
        return None
    exact: Set[Tuple[str, str]] = set()
    users: Set[str] = set()
    for spec in specs:
        parsed = _parse_scope_spec(spec)
        if parsed:
            exact.add(parsed)
        else:
            users.add(str(spec).strip())
    return exact if not users else set()


def resolve_scopes(orient_result: OrientResult, specs: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
    """按 `scopes` 参数筛出本轮要处理的作用域（未指定时为 Orient 枚举出的全部）。"""
    if not specs:
        return list(orient_result.scopes)
    exact: Set[Tuple[str, str]] = set()
    users: Set[str] = set()
    for spec in specs:
        parsed = _parse_scope_spec(spec)
        if parsed:
            exact.add(parsed)
        elif str(spec).strip():
            users.add(str(spec).strip())
    selected = []
    for scope in orient_result.scopes:
        key = (str(scope["user_id"]), str(scope["agent_id"]))
        if key in exact or key[0] in users:
            selected.append(scope)
    return selected


def _cluster_member_records(records: Sequence[Dict[str, Any]], member_ids: Sequence[str]) -> List[Dict[str, Any]]:
    """按指纹顺序取出簇成员记录，供 Consolidate 与 Prune 使用。"""
    index = {str(record["id"]): record for record in records}
    return [index[str(mid)] for mid in member_ids if str(mid) in index]


def run_dream(
    memory: Any,
    *,
    mode: str = _MODE_LIVE,
    settings: Optional[DreamSettings] = None,
    state_store: Optional[DreamClusterStateStore] = None,
    observer: Optional[DreamObserver] = None,
    scopes: Optional[Sequence[str]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """执行一轮整合，返回 dry-run 报告形态的结果（设计 §5.8）。

    `mode = "dry_run"` 与 `"live"` 走同一套判定与同一数量的 LLM 调用，差别只有两点：
    不执行落地写入（Prune 只产出判定），以及调用方不落 `dream_runs` 行。

    簇级流水线：Consolidate 与 Prune 按簇交替推进，处理完一个簇即落地该簇的判定，
    这样中断后已完成的簇（含被剪枝者）都已持久化，重跑不会重复付出 LLM 成本。

    Args:
        memory: `mem0.Memory` 实例（读 `vector_store` / `embedding_model` / `llm`）。
        mode: `dry_run` 或 `live`。
        settings: 参数集合，缺省用设计默认值。
        state_store: 已判定簇状态表；缺失时退化为只按已落地观察短路。
        observer: 运行审计挂载点；dry-run 与 SDK 直连传 None。
        scopes: 只处理指定作用域（`user_id` 或 `user_id:agent_id`），缺省为全部。
        now: 本轮统一时刻基准，便于测试注入。

    Returns:
        报告字典，字段与设计 §5.8 的 dry-run 报告一致，另含 `status`。
    """
    settings = settings or DreamSettings()
    test_mode = mode == _MODE_DRY_RUN
    started = now or datetime.now(timezone.utc)
    run_id = str(uuid.uuid4())
    deadline = started.timestamp() + settings.run_timeout_seconds
    report: Dict[str, Any] = {
        "run_id": run_id,
        "mode": mode,
        "started_at": started.isoformat(),
        "finished_at": None,
        "duration_seconds": 0.0,
        "status": "completed",
        "scopes": [],
        "totals": {
            "facts": 0,
            "observations": 0,
            "clusters": 0,
            "llm_calls": 0,
            "failed_clusters": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "clusters_skipped": 0,
            "clusters_deferred": 0,
        },
        "candidates": [],
        "supersede_preview": [],
        "would_write": [],
        "observations_written": 0,
        "observations_superseded": 0,
        "errors": [],
    }

    if observer is not None and not test_mode:
        observer.start_run(run_id=run_id, mode=mode, started_at=report["started_at"])

    sink = _UsageSink()
    llm_calls = 0
    failed_clusters = 0
    written = 0
    superseded = 0
    disposed_keys: Set[str] = set()

    with _usage_sink_installed(memory.llm, sink), ThreadPoolExecutor(max_workers=1) as executor:
        orient_result = orient(memory.vector_store, state_store=state_store)
        report["totals"]["facts"] = orient_result.totals["facts"]
        report["totals"]["observations"] = orient_result.totals["observations"]
        selected = resolve_scopes(orient_result, scopes)

        def _fail(message: str) -> None:
            report["errors"].append(message)
            report["status"] = "timeout" if "timeout" in message else "failed"

        for scope in selected:
            scope_report = {**scope, "candidates": 0, "skipped_clusters": 0, "deferred_clusters": 0}
            if scope["facts"] < settings.min_cluster_size:
                # O4：事实数不足最小簇规模，本轮跳过该作用域。
                report["scopes"].append(scope_report)
                continue

            try:
                gathered = gather(
                    memory.vector_store,
                    scope,
                    settings=settings,
                    known_keys=orient_result.known_evaluated_keys,
                )
            except DreamUnsupportedStoreError:
                raise
            except Exception as exc:
                logger.error(f"Dream gather failed for scope {scope}: {exc}")
                _fail(f"gather failed for scope {scope['user_id']}/{scope['agent_id']}: {exc}")
                report["scopes"].append(scope_report)
                continue

            pending = [cluster for cluster in gathered["clusters"] if not cluster["skipped_reason"]]
            scope_report["skipped_clusters"] = len(gathered["clusters"]) - len(pending)
            # G1 已判定短路的簇本轮确实又判定了一次（结论不变），记一次 evaluations，
            # 使 [AC-16] 的「decision 与首轮一致、evaluations 各自加一」可复核。
            if state_store is not None and not test_mode:
                for cluster in gathered["clusters"]:
                    if cluster["skipped_reason"] == SKIP_ALREADY_EVALUATED:
                        try:
                            state_store.touch(observation_key=cluster[OBSERVATION_MEMBER_KEY])
                        except Exception as exc:
                            logger.warning(f"Dream state touch failed for {cluster[OBSERVATION_MEMBER_KEY]}: {exc}")
            pending.sort(key=_cluster_sort_key)
            if len(pending) > settings.max_clusters_per_run:
                # G4：单轮簇数上限，其余留待下一轮（不进状态表，下轮继续处理）。
                scope_report["deferred_clusters"] = len(pending) - settings.max_clusters_per_run
                pending = pending[: settings.max_clusters_per_run]

            for cluster in pending:
                if datetime.now(timezone.utc).timestamp() > deadline:
                    report["status"] = "timeout"
                    _fail("run timeout reached, remaining clusters deferred")
                    break

                member_records = _cluster_member_records(gathered["records"], cluster["members"])
                consolidate_input = [
                    {
                        "id": record["id"],
                        "text": record["text"],
                        "created_at": record["created_at"],
                    }
                    for record in member_records
                ]
                try:
                    candidate = synthesize_observation(
                        memory.llm,
                        consolidate_input,
                        executor=executor,
                        timeout_seconds=settings.per_cluster_timeout_seconds,
                    )
                    llm_calls += 1
                except Exception as exc:
                    # 失败不是结论：该簇不入状态表、不写观察，下一轮重新判定（F12/§5.4.1）。
                    failed_clusters += 1
                    logger.warning(f"Dream cluster failed ({cluster[OBSERVATION_MEMBER_KEY]}): {exc}")
                    report["errors"].append(f"cluster {cluster[OBSERVATION_MEMBER_KEY]}: {exc}")
                    continue

                decision = prune_candidate(
                    {**candidate, OBSERVATION_MEMBER_KEY: cluster[OBSERVATION_MEMBER_KEY]},
                    member_ids=cluster["members"],
                    existing_hashes=orient_result.existing_hashes,
                    min_cluster_size=settings.min_cluster_size,
                )
                decision[OBSERVATION_MEMBER_KEY] = cluster[OBSERVATION_MEMBER_KEY]

                point_id = observation_point_id(cluster[OBSERVATION_MEMBER_KEY])
                created_at = datetime.now(timezone.utc).isoformat()
                candidate_report = {
                    OBSERVATION_MEMBER_KEY: cluster[OBSERVATION_MEMBER_KEY],
                    "members": list(cluster["members"]),
                    "text": decision["text"],
                    OBSERVATION_SOURCE_KEY: list(decision["source_ids"]),
                    OBSERVATION_EVIDENCE_KEY: decision["evidence_count"],
                    "counterexample": candidate.get("counterexample", []),
                    "decision": "would_write" if decision["decision"] == DECISION_WRITTEN else "skipped",
                    "skip_reason": decision["skip_reason"],
                }

                supersede_targets: List[Dict[str, Any]] = []
                if decision["decision"] == DECISION_WRITTEN:
                    candidate_report["would_write"] = {"point_id": point_id, "memory_kind": MEMORY_KIND_OBSERVATION}
                    supersede_targets = _select_superseded(
                        new_key=cluster[OBSERVATION_MEMBER_KEY],
                        new_members=cluster["members"],
                        new_point_id=point_id,
                        new_created_at=created_at,
                        observations=orient_result.observations,
                        scope=scope,
                        already_disposed=disposed_keys,
                    )
                    if not test_mode:
                        payload = build_observation_payload(
                            decision=decision,
                            scope=scope,
                            members=member_records,
                            run_id=run_id,
                            created_at=created_at,
                        )
                        try:
                            vector = memory.embedding_model.embed(decision["text"], "add")
                        except Exception as exc:
                            failed_clusters += 1
                            logger.error(f"Dream embedding failed for cluster {cluster[OBSERVATION_MEMBER_KEY]}: {exc}")
                            report["errors"].append(f"embedding failed for {cluster[OBSERVATION_MEMBER_KEY]}: {exc}")
                            continue
                        try:
                            memory.vector_store.insert(vectors=[vector], ids=[point_id], payloads=[payload])
                        except Exception as exc:
                            failed_clusters += 1
                            logger.error(f"Dream insert failed for cluster {cluster[OBSERVATION_MEMBER_KEY]}: {exc}")
                            report["errors"].append(f"insert failed for {cluster[OBSERVATION_MEMBER_KEY]}: {exc}")
                            continue
                        written += 1
                        orient_result.observations[cluster[OBSERVATION_MEMBER_KEY]] = {
                            "point_id": point_id,
                            "scope_user_id": scope["user_id"],
                            "scope_agent_id": scope["agent_id"],
                            "members": list(cluster["members"]),
                            "created_at": created_at,
                            "invalid_at": None,
                        }
                        oriented_hashes = orient_result.existing_hashes
                        oriented_hashes.add(payload["hash"])
                    disposed_keys.update(item[OBSERVATION_MEMBER_KEY] for item in supersede_targets)

                if supersede_targets:
                    report["supersede_preview"].extend(supersede_targets)
                    superseded += _apply_supersede(memory, supersede_targets, test_mode)

                report["candidates"].append(candidate_report)
                scope_report["candidates"] += 1
                if candidate_report["decision"] == "would_write" and test_mode:
                    report["would_write"].append(point_id)

                # 判定结果必须落状态表——被剪枝的候选同样要记，否则每轮都会重新付出该簇的
                # LLM 成本；失败的簇不在此列（失败不是结论）。
                if state_store is not None and not test_mode:
                    try:
                        state_store.record(
                            observation_key=cluster[OBSERVATION_MEMBER_KEY],
                            scope_user_id=scope["user_id"],
                            scope_agent_id=scope["agent_id"],
                            decision=decision["decision"],
                            observed_point_id=point_id if decision["decision"] == DECISION_WRITTEN else None,
                        )
                    except Exception as exc:
                        logger.warning(f"Dream state write failed for {cluster[OBSERVATION_MEMBER_KEY]}: {exc}")

            report["scopes"].append(scope_report)

    finished = datetime.now(timezone.utc)
    report["finished_at"] = finished.isoformat()
    report["duration_seconds"] = round((finished - started).total_seconds(), 3)
    report["totals"]["clusters"] = len(report["candidates"])
    report["totals"]["llm_calls"] = llm_calls
    report["totals"]["failed_clusters"] = failed_clusters
    report["totals"]["prompt_tokens"] = sink.prompt_tokens
    report["totals"]["completion_tokens"] = sink.completion_tokens
    report["totals"]["clusters_skipped"] = sum(scope["skipped_clusters"] for scope in report["scopes"])
    report["totals"]["clusters_deferred"] = sum(scope["deferred_clusters"] for scope in report["scopes"])
    report["observations_written"] = written
    report["observations_superseded"] = superseded

    if observer is not None and not test_mode:
        observer.finish_run(run_id=run_id, status=report["status"], stats=report, report_path=None)
    return report


def write_report(report: Dict[str, Any], report_dir: str) -> str:
    """把报告落盘到 `report_dir/<run_id>.json`，返回绝对路径。

    报告不参与任何判定，仅作审阅与留档；dry-run 的持久化产出只有这一份文件
    （设计 §5.7）。
    """
    import os

    path = os.path.join(str(report_dir), f"{report['run_id']}.json")
    os.makedirs(str(report_dir), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
    return path
