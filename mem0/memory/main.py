import asyncio
import concurrent.futures
import gc
import hashlib
import json
import logging
import math
import os
import threading
import time
import uuid
import warnings
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from mem0.configs.base import DecayConfig, MemoryConfig, MemoryItem
from mem0.configs.enums import MemoryType
from mem0.configs.prompts import (
    ADDITIVE_EXTRACTION_PROMPT,
    AGENT_CONTEXT_SUFFIX,
    BITEMP_CANDIDATE_K,
    BITEMP_EPOCH,
    CONTRADICTION_DETECTION_PROMPT,
    INVALID_REASON_SUPERSEDED,
    PROCEDURAL_MEMORY_SYSTEM_PROMPT,
    generate_additive_extraction_prompt,
    generate_contradiction_detection_prompt,
)
from mem0.exceptions import LLMError
from mem0.exceptions import ValidationError as Mem0ValidationError
from mem0.memory.base import MemoryBase
from mem0.memory.notices import (
    PERFORMANCE_SLOW_QUERY_THRESHOLD_SECONDS,
    detect_decay_usage_from_delete,
    detect_decay_usage_from_delete_all,
    detect_scale_threshold_from_add_result,
    detect_scale_threshold_from_top_k,
    detect_temporal_usage_from_metadata,
    detect_temporal_usage_from_search,
    display_decay_usage_notice,
    display_decay_usage_notice_async,
    display_first_run_notice,
    display_first_run_notice_async,
    display_performance_slow_query_notice,
    display_performance_slow_query_notice_async,
    display_scale_threshold_notice,
    display_scale_threshold_notice_async,
    display_temporal_usage_notice,
    display_temporal_usage_notice_async,
    get_decay_feature_error_message,
    get_decay_feature_error_message_async,
    get_temporal_feature_error_message,
    get_temporal_feature_error_message_async,
)
from mem0.memory.setup import mem0_dir, setup_config
from mem0.memory.storage import SQLiteManager
from mem0.memory.telemetry import MEM0_TELEMETRY, capture_event
from mem0.memory.utils import (
    extract_json,
    parse_messages,
    parse_vision_messages,
    process_telemetry_filters,
    remove_code_blocks,
)
from mem0.utils.entity_extraction import extract_entities, extract_entities_batch
from mem0.utils.factory import (
    EmbedderFactory,
    LlmFactory,
    RerankerFactory,
    VectorStoreFactory,
)
from mem0.utils.lemmatization import lemmatize_for_bm25
from mem0.utils.scoring import (
    ENTITY_BOOST_WEIGHT,
    get_bm25_params,
    normalize_bm25,
    score_and_rank,
)
from mem0.vector_stores.base import VectorStoreBase

# Suppress SWIG deprecation warnings globally
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*SwigPy.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*swigvarlink.*")

# Initialize logger early for util functions
logger = logging.getLogger(__name__)


def _vector_store_list_rows(listed):
    if isinstance(listed, (list, tuple)) and listed and isinstance(listed[0], list):
        return listed[0]
    if isinstance(listed, (list, tuple)):
        return listed
    return []


# Fields that hold runtime auth/connection objects and must be preserved.
# These are non-serializable objects (e.g. AWSV4SignerAuth, RequestsHttpConnection)
# needed by clients like OpenSearch — not sensitive strings to redact.
_RUNTIME_FIELDS = frozenset({
    "http_auth",
    "auth",
    "connection_class",
    "ssl_context",
})

# Fields that are known to contain sensitive secrets and must be redacted.
_SENSITIVE_FIELDS_EXACT = frozenset({
    "api_key",
    "secret_key",
    "private_key",
    "access_key",
    "password",
    "credentials",
    "credential",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "auth_token",
    "session_token",
    "client_secret",
    "auth_client_secret",
    "azure_client_secret",
    "service_account_json",
    "aws_session_token",
})

# Suffixes that indicate a field likely holds a secret value.
_SENSITIVE_SUFFIXES = (
    "_password",
    "_secret",
    "_token",
    "_credential",
    "_credentials",
)

# Entity parameters that must be passed via filters, not top-level kwargs
ENTITY_PARAMS = frozenset({"user_id", "agent_id", "run_id"})
DELETE_ALL_BATCH_SIZE = 1000

# Tenant-scoping fields that caller-supplied metadata must never set, on either the
# creation or the update path (issues #4490, #6277, #6655).
_IDENTITY_KEYS = ENTITY_PARAMS | {"actor_id"}


def _strip_identity_keys(
    metadata: Dict[str, Any],
    existing_payload: Dict[str, Any],
    *,
    context: str = "update()",
) -> Dict[str, Any]:
    """Drop identity keys from caller metadata; scope is set by the entity params, not metadata.

    On the update path `existing_payload` carries the memory's current scope, so
    re-sending an identical value is silently accepted; only a changed value warns.
    On the creation path there is no prior payload, so pass an empty dict and every
    identity key present in `metadata` is dropped with a warning.
    """
    clean = {}
    for key, value in metadata.items():
        if key not in _IDENTITY_KEYS:
            clean[key] = value
        elif value != existing_payload.get(key):
            logger.warning(f"{context}: ignoring metadata['{key}'] - identity fields cannot be set through metadata")
    return clean


def _reject_top_level_entity_params(kwargs: Dict[str, Any], method_name: str) -> None:
    """Reject top-level entity parameters - must use filters instead."""
    invalid_keys = ENTITY_PARAMS & set(kwargs.keys())
    if invalid_keys:
        raise ValueError(
            f"Top-level entity parameters {invalid_keys} are not supported in {method_name}(). "
            f"Use filters={{'user_id': '...'}} instead."
        )


def _validate_and_trim_entity_id(value: Optional[Any], name: str) -> Optional[str]:
    """
    Validates and normalizes an entity ID.
    - Coerces non-string values (e.g. integer ids) to str
    - Trims leading/trailing whitespace
    - Rejects empty or whitespace-only strings
    - Rejects strings containing internal whitespace

    Args:
        value: The entity ID value to validate
        name: The parameter name (for error messages)

    Returns:
        The trimmed entity ID, or None if input is None

    Raises:
        ValueError: If entity ID is invalid
    """
    if value is None:
        return None
    # Callers commonly pass integer ids (e.g. a database primary key). Coerce
    # to str at this single validation point so scoping stays consistent across
    # add/search/get_all/delete_all instead of crashing on `.strip()`.
    if not isinstance(value, str):
        value = str(value)
    trimmed = value.strip()
    if trimmed == "":
        raise ValueError(
            f"Invalid {name}: cannot be empty or whitespace-only. Provide a valid identifier."
        )
    if any(c.isspace() for c in trimmed):
        raise ValueError(
            f"Invalid {name}: cannot contain whitespace. Provide a valid identifier without spaces."
        )
    return trimmed


def _validate_search_params(threshold: Optional[float] = None, top_k: Optional[int] = None) -> None:
    """
    Validates search parameters.

    Args:
        threshold: Similarity threshold (must be between 0 and 1)
        top_k: Number of results to return (must be non-negative integer)

    Raises:
        ValueError: If threshold or top_k are invalid
    """
    if threshold is not None:
        if not isinstance(threshold, (int, float)):
            raise ValueError("threshold must be a valid number")
        if threshold < 0 or threshold > 1:
            raise ValueError(
                f"Invalid threshold: {threshold}. Must be between 0 and 1 (inclusive)."
            )
    if top_k is not None:
        if not isinstance(top_k, int) or isinstance(top_k, bool):
            raise ValueError("top_k must be a valid integer")
        if top_k < 0:
            raise ValueError(
                f"Invalid top_k: {top_k}. Must be a non-negative integer."
            )


def _validate_and_trim_search_query(query: str) -> str:
    """
    Validates and normalizes a search query before embedding/vector search.

    Raises:
        ValueError: If query is not a string or is empty/whitespace-only.
    """
    if not isinstance(query, str):
        raise ValueError("Invalid query: must be a non-empty string.")
    trimmed = query.strip()
    if not trimmed:
        raise ValueError("Invalid query: cannot be empty or whitespace-only.")
    return trimmed


def _is_sensitive_field(field_name: str) -> bool:
    """Check if a field should be redacted for telemetry safety.

    Uses a layered approach:
    1. Runtime fields (allowlist) — always preserved, highest priority.
    2. Exact deny list — known secret field names.
    3. Suffix deny list — catches patterns like db_password, auth_secret, etc.
    """
    name = field_name.lower().strip()
    if name in _RUNTIME_FIELDS:
        return False
    if name in _SENSITIVE_FIELDS_EXACT:
        return True
    return any(name.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES)


def _safe_deepcopy_config(config):
    """Safely deepcopy config, falling back to dict-based cloning for non-serializable objects."""
    try:
        return deepcopy(config)
    except Exception as e:
        logger.debug(f"Deepcopy failed, using dict-based cloning: {e}")

        config_class = type(config)

        if hasattr(config, "model_dump"):
            try:
                clone_dict = config.model_dump()
            except Exception:
                clone_dict = dict(config.__dict__)
        else:
            clone_dict = dict(config.__dict__)

        # Restore runtime fields, redact sensitive ones
        for field_name in list(clone_dict.keys()):
            if field_name in _RUNTIME_FIELDS and hasattr(config, field_name):
                clone_dict[field_name] = getattr(config, field_name)
            elif _is_sensitive_field(field_name):
                clone_dict[field_name] = None

        try:
            return config_class(**clone_dict)
        except Exception:
            logger.debug("Config reconstruction failed, returning shallow dict clone")
            return type("Config", (), clone_dict)()


def _normalize_iso_timestamp_to_utc(timestamp: Optional[str]) -> Optional[str]:
    """Normalize timezone-aware ISO timestamps to UTC without rewriting naive values."""
    if not timestamp:
        return timestamp
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    if parsed.tzinfo is None:
        return timestamp
    return parsed.astimezone(timezone.utc).isoformat()


def _build_filters_and_metadata(
    *,  # Enforce keyword-only arguments
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    run_id: Optional[str] = None,
    actor_id: Optional[str] = None,  # For query-time filtering
    input_metadata: Optional[Dict[str, Any]] = None,
    input_filters: Optional[Dict[str, Any]] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Constructs metadata for storage and filters for querying based on session and actor identifiers.

    This helper supports multiple session identifiers (`user_id`, `agent_id`, and/or `run_id`)
    for flexible session scoping and optionally narrows queries to a specific `actor_id`. It returns two dicts:

    1. `base_metadata_template`: Used as a template for metadata when storing new memories.
       It includes all provided session identifier(s) and any `input_metadata`. Identity
       scope is set from the entity params only; identity keys in `input_metadata` are
       dropped, so freeform metadata cannot place a memory into an unrequested scope.
    2. `effective_query_filters`: Used for querying existing memories. It includes all
       provided session identifier(s), any `input_filters`, and a resolved actor
       identifier for targeted filtering if specified by any actor-related inputs.

    Actor filtering precedence: explicit `actor_id` arg → `filters["actor_id"]`
    This resolved actor ID is used for querying but is not added to `base_metadata_template`,
    as the actor for storage is typically derived from message content at a later stage.

    Args:
        user_id (Optional[str]): User identifier, for session scoping.
        agent_id (Optional[str]): Agent identifier, for session scoping.
        run_id (Optional[str]): Run identifier, for session scoping.
        actor_id (Optional[str]): Explicit actor identifier, used as a potential source for
            actor-specific filtering. See actor resolution precedence in the main description.
        input_metadata (Optional[Dict[str, Any]]): Base dictionary to be augmented with
            session identifiers for the storage metadata template. Defaults to an empty dict.
        input_filters (Optional[Dict[str, Any]]): Base dictionary to be augmented with
            session and actor identifiers for query filters. Defaults to an empty dict.

    Returns:
        tuple[Dict[str, Any], Dict[str, Any]]: A tuple containing:
            - base_metadata_template (Dict[str, Any]): Metadata template for storing memories,
              scoped to the provided session(s).
            - effective_query_filters (Dict[str, Any]): Filters for querying memories,
              scoped to the provided session(s) and potentially a resolved actor.
    """

    # Identity scope is set below from the entity params only. Stripping the keys here
    # stops caller metadata from placing a memory into a scope the caller did not pass,
    # which the re-pins below cannot prevent for a param that was left unset (issue #6655).
    base_metadata_template = (
        _strip_identity_keys(deepcopy(input_metadata), {}, context="add()") if input_metadata else {}
    )
    effective_query_filters = deepcopy(input_filters) if input_filters else {}

    # ---------- validate and add all provided session ids ----------
    session_ids_provided = []

    # Validate and trim entity IDs
    user_id = _validate_and_trim_entity_id(user_id, "user_id")
    agent_id = _validate_and_trim_entity_id(agent_id, "agent_id")
    run_id = _validate_and_trim_entity_id(run_id, "run_id")

    if user_id:
        base_metadata_template["user_id"] = user_id
        effective_query_filters["user_id"] = user_id
        session_ids_provided.append("user_id")

    if agent_id:
        base_metadata_template["agent_id"] = agent_id
        effective_query_filters["agent_id"] = agent_id
        session_ids_provided.append("agent_id")

    if run_id:
        base_metadata_template["run_id"] = run_id
        effective_query_filters["run_id"] = run_id
        session_ids_provided.append("run_id")

    if not session_ids_provided:
        raise Mem0ValidationError(
            message="At least one of 'user_id', 'agent_id', or 'run_id' must be provided.",
            error_code="VALIDATION_001",
            details={"provided_ids": {"user_id": user_id, "agent_id": agent_id, "run_id": run_id}},
            suggestion="Please provide at least one identifier to scope the memory operation."
        )

    # ---------- optional actor filter ----------
    resolved_actor_id = actor_id or effective_query_filters.get("actor_id")
    if resolved_actor_id:
        effective_query_filters["actor_id"] = resolved_actor_id

    return base_metadata_template, effective_query_filters


def _escape_scope_value(val: Any) -> str:
    """Escape the structural delimiters of the session scope key."""
    return str(val).replace("%", "%25").replace("&", "%26").replace("=", "%3D")


def _build_session_scope(filters):
    """Build deterministic session scope string from entity IDs."""
    parts = []
    for key in sorted(["user_id", "agent_id", "run_id"]):
        val = filters.get(key)
        if val:
            parts.append(f"{key}={_escape_scope_value(val)}")
    return "&".join(parts)


def _entity_collection_name(provider: str, collection_name: str) -> str:
    separator = "-" if provider == "s3_vectors" else "_"
    return f"{collection_name}{separator}entities"


def _normalize_expiration_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise ValueError("expiration_date must be a valid date in YYYY-MM-DD format.") from exc
    raise ValueError("expiration_date must be a date string in YYYY-MM-DD format.")


def _payload_is_expired(payload: Optional[Dict[str, Any]]) -> bool:
    if not payload:
        return False
    expiration_date = payload.get("expiration_date")
    if not expiration_date:
        return False
    try:
        return date.fromisoformat(str(expiration_date)) < datetime.now(timezone.utc).date()
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Bi-temporal fact model (design 2026-09-17)
#
# 1a extraction produces `valid_at`; 1b (an independent LLM call) only reports
# contradicting pairs; 1c -- the pure helpers below -- derives every invalidation
# from the records' own time fields, so the same input always yields the same output.
# ---------------------------------------------------------------------------

# Payload keys holding the access footprint of a memory (memory decay): `last_accessed`
# is the moment the record was last returned by a search, `access_count` how many times
# it has been returned. Declared here because the read paths promote them top-level.
DECAY_PAYLOAD_KEYS = ("last_accessed", "access_count")

# Payload keys promoted to the top level of every read result. The four bi-temporal
# fields are ordinary payload fields (no second store), so they belong in the same
# list; one declaration keeps a read path from leaking them into `metadata`. The two
# access-footprint fields (memory decay) are promoted for the same reason: they are
# first-class record attributes, not user metadata.
BI_TEMPORAL_PAYLOAD_KEYS = [
    "user_id",
    "agent_id",
    "run_id",
    "actor_id",
    "role",
    "attributed_to",
    "expiration_date",
    "valid_at",
    "invalid_at",
    "superseded_by",
    "invalid_reason",
    *DECAY_PAYLOAD_KEYS,
]

# Bi-temporal fields, surfaced top-level on every read result (null when not set).
BI_TEMPORAL_FIELDS = ("valid_at", "invalid_at", "superseded_by", "invalid_reason")

# Fields every read result carries at the top level, null when the record has no value.
# The access footprint belongs here for the same reason the bi-temporal quartet does: a
# client must be able to tell "never accessed" from "this endpoint does not report it".
READ_ALWAYS_PRESENT_FIELDS = (*BI_TEMPORAL_FIELDS, *DECAY_PAYLOAD_KEYS)


def _coerce_bitemporal_date(value: Any) -> Optional[str]:
    """Normalize a date-like value to `YYYY-MM-DD`, or None when it is not a date.

    Both sources (LLM `valid_at` output and stored payloads) are untrusted, and only
    date-parsable values are kept: a non-date string in a datetime-indexed field would
    make the validity `Range` filters silently misbehave.
    """
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


def _effective_from(record: Dict[str, Any]) -> str:
    """生效时间：`valid_at` 存在则取之，否则以入库时间 `created_at` 兜底。

    存量记录没有 `valid_at`，它们唯一的生效证据就是入库时间；这里不推测、不回填。
    """
    valid_at = _coerce_bitemporal_date(record.get("valid_at"))
    if valid_at:
        return valid_at
    return _coerce_bitemporal_date(record.get("created_at")) or ""


def _temporal_key(record: Dict[str, Any]) -> tuple:
    """时序比较键 `(生效时间, 入库时间)`，全序，供 1c 的先后判定使用。"""
    return (_effective_from(record), str(record.get("created_at") or ""))


def _apply_contradictions(
    contradictions: List[Dict[str, Any]],
    new_records: Dict[str, Dict[str, Any]],
    old_records: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """1c：按生效时间把矛盾对转成旧记录的失效写入（确定性、无 LLM、无 `now` 依赖）。

    对应方案 §5.3.2 的 R1-R11：
      R1 幻觉 id 防线：`old_id` 不在候选集合内直接丢弃并告警；`new_id` 同理（1b 输出不可信）。
      R2 自反配对丢弃。
      R3 时序守卫：仅当 `k(new) > k(old)` 才处置，相等或更早一律不处置。
      R5 重复失效取最早：已失效记录的 `invalid_at` 更早时三字段保持不变。
      R6 同一批次里一条旧记录只处置一次：先按 R3 过滤，再取 `k(new)` 最大者。
      R9/R10 只产出 payload 三字段，不改文本、不写 history（由调用方执行 payload-only 更新）。

    Args:
        contradictions: 1b 产出的配对 `[{"new_id": str, "old_id": str}]`。
        new_records: 本批次新记录 `{id: {"valid_at": ..., "created_at": ...}}`。
        old_records: 候选旧记录 `{id: 该记录的 payload}`。

    Returns:
        `[{"memory_id", "invalid_at", "superseded_by", "invalid_reason"}]`，顺序确定。
    """
    if not contradictions:
        return []

    # 配对去重 + 排序：处置结果与 1b 的输出顺序无关，同输入必同输出。
    pairs = set()
    for pair in contradictions:
        if not isinstance(pair, dict):
            continue
        new_id, old_id = pair.get("new_id"), pair.get("old_id")
        if not new_id or not old_id:
            continue
        if new_id == old_id:  # R2
            continue
        pairs.add((str(old_id), str(new_id)))

    winners: Dict[str, str] = {}
    for old_id, new_id in sorted(pairs):
        if old_id not in old_records:  # R1
            logger.warning(f"Contradiction dropped: unknown old_id {old_id}")
            continue
        if new_id not in new_records:
            logger.warning(f"Contradiction dropped: unknown new_id {new_id}")
            continue
        new_record = new_records[new_id]
        old_record = old_records[old_id]
        if _temporal_key(new_record) <= _temporal_key(old_record):  # R3
            continue
        current = winners.get(old_id)
        if current is None:
            winners[old_id] = new_id
            continue
        # R6：取生效键最大的新事实；生效键完全相等时取 new_id 字典序最小者。
        if _temporal_key(new_record) > _temporal_key(new_records[current]):
            winners[old_id] = new_id
        elif _temporal_key(new_record) == _temporal_key(new_records[current]) and new_id < current:
            winners[old_id] = new_id

    updates = []
    for old_id in sorted(winners):
        new_id = winners[old_id]
        proposed_invalid_at = _effective_from(new_records[new_id])
        existing_invalid_at = _coerce_bitemporal_date(old_records[old_id].get("invalid_at"))
        # R5：已有失效时间更早时保留原结论，保证三字段始终指向同一次取代。
        if existing_invalid_at and existing_invalid_at <= proposed_invalid_at:
            continue
        updates.append(
            {
                "memory_id": old_id,
                "invalid_at": proposed_invalid_at,
                "superseded_by": new_id,
                "invalid_reason": INVALID_REASON_SUPERSEDED,
            }
        )
    return updates


def _normalize_as_of(as_of: Any) -> str:
    """把 `as_of`（`YYYY-MM-DD` 或 ISO8601）规范化为 UTC ISO8601；None 表示当前时刻。"""
    if as_of is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(as_of, datetime):
        moment = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat()
    if isinstance(as_of, date):
        return datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc).isoformat()
    try:
        moment = datetime.fromisoformat(str(as_of).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("as_of must be 'YYYY-MM-DD' or an ISO8601 datetime.") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _bitemporal_filter(
    as_of: Any = None,
    include_invalidated: bool = False,
) -> Optional[Dict[str, Any]]:
    """有效事实过滤谓词（方案 §5.4.1）。

    - 默认读（`as_of=None`）：只排除已失效记录。
    - point-in-time（给定 T）：生效时间 `<= T`（`valid_at` 缺失时以 `created_at` 兜底），
      且（未失效 或 失效时间 `> T`）。缺失字段由 `NOT[valid_at >= BITEMP_EPOCH]` 判定，
      因为 `Range` 不匹配缺失字段，取反后放行。
    """
    if include_invalidated:
        return None
    moment = _normalize_as_of(as_of)
    if as_of is None:
        return {"NOT": [{"invalid_at": {"lte": moment}}]}
    return {
        "OR": [
            {"valid_at": {"lte": moment}},
            {"AND": [{"created_at": {"lte": moment}}, {"NOT": [{"valid_at": {"gte": BITEMP_EPOCH}}]}]},
        ],
        "NOT": [{"invalid_at": {"lte": moment}}],
    }


def _with_bitemporal_filter(
    filters: Optional[Dict[str, Any]],
    as_of: Any = None,
    include_invalidated: bool = False,
) -> Dict[str, Any]:
    """把有效事实谓词与业务 filters 以 AND 合并，供所有读路径共用。

    合并（而非就地改写）保证调用方传入的 filters 不被污染，也保证 `include_invalidated`
    时原样返回、不加任何时间条件。
    """
    predicate = _bitemporal_filter(as_of, include_invalidated)
    if predicate is None:
        return dict(filters) if filters else {}
    if not filters:
        return predicate
    return {"AND": [dict(filters), predicate]}


def _scope_filters(filters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """取出作用域过滤键（user_id / agent_id / run_id），兼容被 AND 包裹的谓词。

    `Memory.search` 把有效事实谓词以 `{"AND": [作用域, 谓词]}` 的形式合并后传给检索；
    实体加成只需要其中的作用域部分。若直接按顶层键过滤，合并后的 filters 会取到空作用域，
    使实体加成退化成全局（跨用户）搜索——所以这里显式展开 AND 包裹。
    """
    if not filters:
        return {}
    if isinstance(filters.get("AND"), list):
        merged: Dict[str, Any] = {}
        for sub in filters["AND"]:
            merged.update(_scope_filters(sub))
        return merged
    return {k: v for k, v in filters.items() if k in ("user_id", "agent_id", "run_id") and v}


def _parse_contradiction_pairs(response: Any) -> List[Dict[str, str]]:
    """解析 1b 的输出；不可解析时返回空列表（本次不处置任何记录）。"""
    if not response or not str(response).strip():
        return []
    try:
        parsed = json.loads(remove_code_blocks(str(response)), strict=False)
    except json.JSONDecodeError:
        try:
            parsed = json.loads(extract_json(remove_code_blocks(str(response))), strict=False)
        except Exception as e:
            logger.error(f"Error parsing contradiction response: {e}")
            return []
    except Exception as e:
        logger.error(f"Error parsing contradiction response: {e}")
        return []
    if not isinstance(parsed, dict):
        logger.error("Error parsing contradiction response: not a JSON object")
        return []
    pairs = parsed.get("contradictions", [])
    if not isinstance(pairs, list):
        logger.error("Error parsing contradiction response: 'contradictions' is not a list")
        return []
    return [pair for pair in pairs if isinstance(pair, dict)]


# ---------------------------------------------------------------------------
# Memory decay (Ebbinghaus retention + access reinforcement)
#
# Design: `docs/design/memory-decay.md`. The time factor is a pure function of the
# record's own footprint (`last_accessed` / `access_count`, falling back to
# `created_at`) and the injected `now`; it is applied to the hybrid score in the
# ranking layer of `/search` only -- never in the write pipeline, never as a filter.
# All arithmetic is UTC and day-based, so the same footprint at the same instant
# always yields the same factor.
# ---------------------------------------------------------------------------

# Fields promoted out of `metadata` on every read path: the footprint travels with the
# record as a first-class attribute (`last_accessed` / `access_count`), see
# `BI_TEMPORAL_PAYLOAD_KEYS` above.
_SECONDS_PER_DAY = 86400.0

# Payload fields surfaced in `score_details` under `explain=true` when decay is active.
# Kept in sync with `mem0.utils.scoring.DECAY_DETAIL_KEYS`.
DECAY_DETAIL_KEYS = ("decay_weight", "retention", "memory_strength_days", "elapsed_days", "access_count")


def _parse_decay_timestamp(value: Any) -> Optional[datetime]:
    """把足迹 / 入库时间字段解析成带时区的 UTC 时刻；不可解析时返回 None。

    空值与非法值一律视为「无足迹」，由调用方回落到 `created_at` 或 `Δt = 0`。
    小数秒与时区偏移（含 `Z`）都接受；朴素时间按 UTC 解释，与写入侧
    `datetime.now(timezone.utc).isoformat()` 的形态一致。
    """
    if value is None or isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text[-1] in ("Z", "z"):
            # `datetime.fromisoformat` only accepts the `Z` suffix from Python 3.11,
            # and this SDK supports 3.9+.
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_access_count(value: Any) -> int:
    """把 `access_count` 归一为非负整数；缺失或非法取 0（等价于「从未被召回」）。"""
    if isinstance(value, bool) or value is None:
        return 0
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 0
    return count if count > 0 else 0


def _decay_retention(
    elapsed_days: float,
    access_count: int,
    *,
    halflife_days: float,
    strength_step_days: float,
    access_cap: int,
) -> float:
    """Ebbinghaus 保留率 `R = exp(-Δt / S)`，`S = S₀ + ΔS·min(n, N_cap)`（设计 §3.1-3.2）。

    Δt 以天为单位、沿时间轴正向取；负值（时钟回拨）按 0 处理，使因子退化为 1.0。
    `access_count` 超过 `access_cap` 后不再增长强度，行为与恰好 `access_cap` 次一致。
    """
    counted = min(access_count, access_cap) if access_cap > 0 else 0
    strength_days = halflife_days + strength_step_days * counted
    if strength_days <= 0:
        return 0.0
    delta = elapsed_days if elapsed_days > 0 else 0.0
    return math.exp(-delta / strength_days)


def _decay_weight(retention: float, floor: float) -> float:
    """把保留率归一成 [floor, 1] 的时间因子：`FLOOR + (1 - FLOOR)·R`（设计 §3.4）。

    只下调、不放大：`decay_weight <= 1`，因此时间因子永远不能把一条候选的分抬到
    它的原始语义分之上，新的候选也不会获得额外加成。
    """
    return floor + (1.0 - floor) * retention


def _decay_factors_for_candidates(
    candidates: List[Dict[str, Any]],
    config: DecayConfig,
    now: datetime,
) -> Dict[str, Dict[str, Any]]:
    """为整批候选计算时间因子（同一 `now` 基准，设计 §3.5）。

    每条候选的时间起点取 `last_accessed`，缺失时以 `created_at` 兜底；两者皆缺时
    `Δt = 0`，时间因子为中性 1.0（存量记录不回填足迹，设计 §4.5）。

    Args:
        candidates: 打分前的候选，元素含 `id` 与 `payload`。
        config: 衰减配置（参数与开关）。
        now: 本次检索统一的时刻基准（UTC）。

    Returns:
        `{memory_id: {decay_weight, retention, memory_strength_days, elapsed_days, access_count}}`。
    """
    factors: Dict[str, Dict[str, Any]] = {}
    for candidate in candidates:
        mem_id = candidate.get("id")
        if mem_id is None:
            continue
        payload = candidate.get("payload") or {}
        reference = _parse_decay_timestamp(payload.get("last_accessed")) or _parse_decay_timestamp(
            payload.get("created_at")
        )
        if reference is None:
            elapsed_days = 0.0
        else:
            elapsed_days = (now - reference).total_seconds() / _SECONDS_PER_DAY
            if elapsed_days < 0:
                elapsed_days = 0.0
        access_count = _coerce_access_count(payload.get("access_count"))
        retention = _decay_retention(
            elapsed_days,
            access_count,
            halflife_days=config.halflife_days,
            strength_step_days=config.strength_step_days,
            access_cap=config.access_cap,
        )
        kept = min(access_count, config.access_cap) if config.access_cap > 0 else 0
        factors[str(mem_id)] = {
            "decay_weight": _decay_weight(retention, config.floor),
            "retention": retention,
            "memory_strength_days": config.halflife_days + config.strength_step_days * kept,
            "elapsed_days": elapsed_days,
            "access_count": access_count,
        }
    return factors


def _dispatch_decay_reinforcement(vector_store, updates: Dict[str, Dict[str, Any]]) -> None:
    """把强化写入派发到后台线程（设计 §4.3）。

    强化不进入响应时延路径：检索结果先返回，写入在后台线程里补齐；写入失败只记日志，
    不改变已返回的结果。
    """

    def _write() -> None:
        try:
            vector_store.update_payload_batch(updates)
        except Exception as e:
            logger.warning(f"Decay reinforcement write failed for {len(updates)} memories: {e}")

    threading.Thread(target=_write, name="mem0-decay-reinforce", daemon=True).start()


def _decay_config_of(memory) -> Optional[DecayConfig]:
    """取实例的衰减配置段；不是真正的 `DecayConfig` 时返回 None。

    返回 None 在调用方一律等价于「关闭」：本机制对检索路径必须是严格增量的，实例若
    未携带配置段（子类、测试替身、替身式 MagicMock 配置）就不得产生任何副作用，也不
    得让替身的属性值流进算式。
    """
    config = getattr(getattr(memory, "config", None), "decay", None)
    return config if isinstance(config, DecayConfig) else None


def _reinforce_search_hits(memory, results: List[Dict[str, Any]]) -> None:
    """给一次 `/search` 的最终返回列表派发访问足迹写入（设计 §4.2-4.3）。

    一次「访问」定义为该记忆出现在最终返回列表中：候选池内被 `top_k` 截断的条目
    不产生强化。四项写放大控制缺一不可——只动入选条目、合成一次批量写、同一记忆在
    冷却窗口内只写一次、写入异步入后台线程。

    关闭态下直接返回：不产生任何足迹写入，也不触碰冷却表（设计 §6.2）。因此调用方
    无需再次判定开关。

    Args:
        memory: `Memory` / `AsyncMemory` 实例，提供 `config.decay`、`vector_store`
            以及冷却表 `_decay_write_at` / `_decay_write_lock`。
        results: 本次检索最终返回的结果列表（元素含 `id`，可能含 `access_count`）。
    """
    # 缺配置段一律视为关闭：本机制对检索路径必须是严格增量的，实例若未携带 decay 段
    # （旧配置对象、子类、测试替身）时不得产生任何副作用。
    config = _decay_config_of(memory)
    if config is None or not config.enabled or not isinstance(results, list) or not results:
        return

    now = datetime.now(timezone.utc)
    monotonic_now = time.monotonic()
    cooldown = config.cooldown_seconds
    updates: Dict[str, Dict[str, Any]] = {}

    with memory._decay_write_lock:
        for result in results:
            memory_id = result.get("id")
            if not memory_id:
                continue
            memory_id = str(memory_id)
            if memory_id in updates:
                continue
            last_written = memory._decay_write_at.get(memory_id)
            if cooldown > 0 and last_written is not None and monotonic_now - last_written < cooldown:
                # 冷却窗口内的重复命中不累加计数，也不产生写入。
                continue
            memory._decay_write_at[memory_id] = monotonic_now
            updates[memory_id] = {
                "last_accessed": now.isoformat(),
                "access_count": _coerce_access_count(result.get("access_count")) + 1,
            }
        # 冷却表只保留窗口内的条目：窗口为 0 时没有条目可以留在表里，因此表不会随
        # 检索量无限增长。
        for memory_id in [
            mid for mid, at in memory._decay_write_at.items() if monotonic_now - at >= cooldown
        ]:
            memory._decay_write_at.pop(memory_id, None)

    if updates:
        _dispatch_decay_reinforcement(memory.vector_store, updates)


setup_config()
logger = logging.getLogger(__name__)

_UNSET = object()
_PROJECT_UPDATE_UNSUPPORTED_ERROR = "Project updates are not supported by the OSS Memory SDK."


class _OSSProject:
    def update(
        self,
        custom_instructions: Optional[str] = None,
        custom_categories: Optional[list] = None,
        multilingual: Optional[bool] = None,
        decay: Optional[bool] = None,
    ):
        if decay is True:
            raise ValueError(get_decay_feature_error_message("sync", "project.update", "decay"))
        raise ValueError(_PROJECT_UPDATE_UNSUPPORTED_ERROR)


class _AsyncOSSProject:
    async def update(
        self,
        custom_instructions: Optional[str] = None,
        custom_categories: Optional[list] = None,
        multilingual: Optional[bool] = None,
        decay: Optional[bool] = None,
    ):
        if decay is True:
            raise ValueError(await get_decay_feature_error_message_async("async", "project.update", "decay"))
        raise ValueError(_PROJECT_UPDATE_UNSUPPORTED_ERROR)


class Memory(MemoryBase):
    def __init__(self, config: MemoryConfig = MemoryConfig()):
        self.config = config

        self.embedding_model = EmbedderFactory.create(
            self.config.embedder.provider,
            self.config.embedder.config,
            self.config.vector_store.config,
        )
        self.vector_store = VectorStoreFactory.create(
            self.config.vector_store.provider, self.config.vector_store.config
        )
        self.llm = LlmFactory.create(self.config.llm.provider, self.config.llm.config)
        self.db = SQLiteManager(self.config.history_db_path)
        self.collection_name = self.config.vector_store.config.collection_name
        self.api_version = self.config.version
        self.custom_instructions = self.config.custom_instructions

        # Initialize reranker if configured
        self.reranker = None
        if config.reranker:
            self.reranker = RerankerFactory.create(
                config.reranker.provider,
                config.reranker.config
            )

        # Entity store is initialized lazily on first use
        self._entity_store = None

        # 检索强化的进程内冷却表：memory_id -> 上次写入的单调时刻。请求路径只做
        # 查表/登记，真正的 payload 写在后台线程执行（设计 §4.3）。
        self._decay_write_at: Dict[str, float] = {}
        self._decay_write_lock = threading.Lock()

        if MEM0_TELEMETRY:
            # Create telemetry config manually to avoid deepcopy issues with thread locks
            telemetry_config_dict = {}
            if hasattr(self.config.vector_store.config, 'model_dump'):
                # For pydantic models
                telemetry_config_dict = self.config.vector_store.config.model_dump()
            else:
                # For other objects, manually copy common attributes
                for attr in ['host', 'port', 'path', 'api_key', 'index_name', 'dimension', 'metric']:
                    if hasattr(self.config.vector_store.config, attr):
                        telemetry_config_dict[attr] = getattr(self.config.vector_store.config, attr)

            # Override collection name for telemetry
            telemetry_config_dict['collection_name'] = "mem0migrations"

            # Set path for file-based vector stores
            telemetry_config = _safe_deepcopy_config(self.config.vector_store.config)
            if self.config.vector_store.provider in ["faiss", "qdrant"]:
                provider_path = f"migrations_{self.config.vector_store.provider}"
                telemetry_config_dict['path'] = os.path.join(mem0_dir, provider_path)
                os.makedirs(telemetry_config_dict['path'], exist_ok=True)

            # Create the config object using the same class as the original
            telemetry_config = self.config.vector_store.config.__class__(**telemetry_config_dict)
            self._telemetry_vector_store = VectorStoreFactory.create(
                self.config.vector_store.provider, telemetry_config
            )
        if getattr(type(self.vector_store), "keyword_search", None) is VectorStoreBase.keyword_search:
            logger.warning(
                "The '%s' vector store does not support keyword search. "
                "Hybrid (BM25) scoring will be disabled and search will use "
                "semantic similarity only. To enable hybrid search, switch to a "
                "store with keyword_search support (e.g. qdrant, elasticsearch, pgvector).",
                self.config.vector_store.provider,
            )

        capture_event("mem0.init", self, {"sync_type": "sync"})

    @property
    def project(self):
        return _OSSProject()

    @property
    def decay_config(self) -> DecayConfig:
        """记忆衰减配置（设计 `docs/design/memory-decay.md`，配置段 `MemoryConfig.decay`）。"""
        return self.config.decay

    def _decay_factors(self, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Dict[str, Any]]]:
        """本次检索的时间因子表；关闭态返回 None，使打分器走原有的纯混合分路径。"""
        config = _decay_config_of(self)
        if config is None or not config.enabled:
            return None
        return _decay_factors_for_candidates(candidates, config, datetime.now(timezone.utc))

    @property
    def entity_store(self):
        """Lazily initialize entity store on first use."""
        if self._entity_store is None:
            entity_config = _safe_deepcopy_config(self.config.vector_store.config)
            entity_collection = _entity_collection_name(self.config.vector_store.provider, self.collection_name)
            # Set collection name on the cloned config
            if hasattr(entity_config, 'collection_name'):
                entity_config.collection_name = entity_collection
            elif isinstance(entity_config, dict):
                entity_config['collection_name'] = entity_collection
            # For Qdrant, share the existing client to avoid RocksDB lock contention
            # when using embedded mode (path=...). QdrantConfig.client takes precedence
            # over host/port/path.
            if self.config.vector_store.provider == "qdrant" and hasattr(self.vector_store, "client"):
                if hasattr(entity_config, "client"):
                    entity_config.client = self.vector_store.client
                elif isinstance(entity_config, dict):
                    entity_config["client"] = self.vector_store.client
            self._entity_store = VectorStoreFactory.create(
                self.config.vector_store.provider, entity_config
            )
        return self._entity_store

    @staticmethod
    def _normalize_entity_text(value: str) -> str:
        return " ".join(value.strip().lower().split())

    def _existing_entities_by_text(self, filters):
        """Return existing entity rows keyed by normalized payload data."""
        try:
            listed = self.entity_store.list(filters=filters, top_k=10000)
        except Exception as e:
            logger.debug(f"Exact entity lookup failed, falling back to semantic dedup: {e}")
            return {}

        rows_by_text = {}
        for row in _vector_store_list_rows(listed):
            payload = getattr(row, "payload", None) or {}
            text = payload.get("data")
            if not isinstance(text, str):
                continue
            normalized = self._normalize_entity_text(text)
            if normalized and normalized not in rows_by_text:
                rows_by_text[normalized] = row
        return rows_by_text

    def _upsert_entity(self, entity_text, entity_type, memory_id, filters):
        """Upsert an entity into the entity store, linking it to a memory."""
        try:
            entity_embedding = self.embedding_model.embed(entity_text, "add")
            search_filters = {k: v for k, v in filters.items() if k in ("user_id", "agent_id", "run_id") and v}
            exact_match = self._existing_entities_by_text(search_filters).get(self._normalize_entity_text(entity_text))

            existing = []
            if exact_match is None:
                existing = self.entity_store.search(
                    query=entity_text,
                    vectors=entity_embedding,
                    top_k=1,
                    filters=search_filters,
                )

            semantic_match = existing[0] if existing and existing[0].score >= 0.95 else None
            match = exact_match or semantic_match
            if match:
                # Update existing entity's linked_memory_ids
                payload = match.payload or {}
                linked_ids = payload.get("linked_memory_ids", [])
                if memory_id not in linked_ids:
                    linked_ids.append(memory_id)
                    payload["linked_memory_ids"] = linked_ids
                    self.entity_store.update(
                        vector_id=match.id,
                        vector=None,
                        payload=payload,
                    )
            else:
                # Create new entity
                entity_id = str(uuid.uuid4())
                entity_payload = {
                    "data": entity_text,
                    "entity_type": entity_type,
                    "linked_memory_ids": [memory_id],
                    **{k: v for k, v in search_filters.items()},
                }
                self.entity_store.insert(
                    vectors=[entity_embedding],
                    ids=[entity_id],
                    payloads=[entity_payload],
                )
        except Exception as e:
            logger.warning(f"Entity upsert failed for '{entity_text}': {e}")

    def _remove_memory_from_entity_store(self, memory_id, filters):
        """Strip `memory_id` from every entity record scoped to `filters`.

        For each entity whose `linked_memory_ids` contains `memory_id`:
          - remove the id; if the list becomes empty, delete the entity record.
          - otherwise re-embed the entity text and update the payload
            (the vector store's update() requires a vector).

        No-op if the entity store has never been initialized in this process.
        Errors on individual entities are swallowed at debug level; outer
        failures are swallowed at warning level so the primary delete/update
        path is never broken by entity cleanup.
        """
        if self._entity_store is None:
            return
        search_filters = {k: v for k, v in filters.items() if k in ("user_id", "agent_id", "run_id") and v}
        try:
            listed = self.entity_store.list(filters=search_filters, top_k=10000)
            rows = listed[0] if isinstance(listed, (list, tuple)) and listed and isinstance(listed[0], list) else listed
            for row in rows or []:
                try:
                    payload = getattr(row, "payload", None) or {}
                    linked = payload.get("linked_memory_ids", [])
                    if not isinstance(linked, list) or memory_id not in linked:
                        continue
                    remaining = [mid for mid in linked if mid != memory_id]
                    if not remaining:
                        try:
                            self.entity_store.delete(vector_id=row.id)
                        except Exception as e:
                            logger.debug(f"Entity delete failed for id={row.id}: {e}")
                    else:
                        entity_text = payload.get("data")
                        if not isinstance(entity_text, str) or not entity_text:
                            logger.debug(f"Entity id={row.id} missing 'data'; skipping update during cleanup")
                            continue
                        try:
                            vec = self.embedding_model.embed(entity_text, "update")
                        except Exception as e:
                            logger.debug(f"Entity re-embed failed for '{entity_text}': {e}")
                            continue
                        new_payload = {**payload, "linked_memory_ids": remaining}
                        try:
                            self.entity_store.update(
                                vector_id=row.id,
                                vector=vec,
                                payload=new_payload,
                            )
                        except Exception as e:
                            logger.debug(f"Entity update failed for id={row.id}: {e}")
                except Exception as e:
                    logger.debug(f"Entity cleanup error: {e}")
        except Exception as e:
            logger.warning(f"Entity store cleanup failed for memory_id={memory_id}: {e}")

    def _link_entities_for_memory(self, memory_id, text, filters):
        """Extract entities from `text` and link them to `memory_id` in the
        entity store, scoped to `filters`. Simpler single-memory variant of
        Phase 7 in add(): per-entity search-then-update-or-insert via the
        existing `_upsert_entity` helper. Non-fatal on any failure.
        """
        try:
            entities = extract_entities(text)
            if not entities:
                return
            seen = set()
            for entity_type, entity_text in entities:
                key = self._normalize_entity_text(entity_text)
                if not key or key in seen:
                    continue
                seen.add(key)
                try:
                    self._upsert_entity(entity_text, entity_type, memory_id, filters)
                except Exception as e:
                    logger.debug(f"Entity link failed for '{entity_text}': {e}")
        except Exception as e:
            logger.warning(f"Entity linking failed for memory_id={memory_id}: {e}")

    @classmethod
    def from_config(cls, config_dict: Dict[str, Any]):
        try:
            config = MemoryConfig(**config_dict)
        except ValidationError as e:
            logger.error(f"Configuration validation error: {e}")
            raise
        return cls(config)

    def _should_use_agent_memory_extraction(self, messages, metadata):
        """Determine whether to use agent memory extraction based on the logic:
        - If agent_id is present and messages contain assistant role -> True
        - Otherwise -> False

        Args:
            messages: List of message dictionaries
            metadata: Metadata containing user_id, agent_id, etc.

        Returns:
            bool: True if should use agent memory extraction, False for user memory extraction
        """
        # Check if agent_id is present in metadata
        has_agent_id = metadata.get("agent_id") is not None

        # Check if there are assistant role messages
        has_assistant_messages = any(msg.get("role") == "assistant" for msg in messages)

        # Use agent memory extraction if agent_id is present and there are assistant messages
        return has_agent_id and has_assistant_messages

    def add(
        self,
        messages,
        *,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Optional[Any] = None,
        expiration_date: Optional[Any] = None,
        infer: bool = True,
        memory_type: Optional[str] = None,
        prompt: Optional[str] = None,
    ):
        """
        Create a new memory.

        Adds new memories scoped to a single session id (e.g. `user_id`, `agent_id`, or `run_id`). One of those ids is required.

        Args:
            messages (str or List[Dict[str, str]]): The message content or list of messages
                (e.g., `[{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi"}]`)
                to be processed and stored.
            user_id (str, optional): ID of the user creating the memory. Defaults to None.
            agent_id (str, optional): ID of the agent creating the memory. Defaults to None.
            run_id (str, optional): ID of the run creating the memory. Defaults to None.
            metadata (dict, optional): Metadata to store with the memory. Defaults to None.
            timestamp (Any, optional): Platform-only temporal parameter. Not supported in OSS.
            expiration_date (Any, optional): Date in YYYY-MM-DD format. Expired memories are hidden
                from search and get_all unless show_expired is True.
            infer (bool, optional): If True (default), an LLM is used to extract key facts from
                'messages' and decide whether to add, update, or delete related memories.
                If False, 'messages' are added as raw memories directly.
            memory_type (str, optional): Specifies the type of memory. Currently, only
                `MemoryType.PROCEDURAL.value` ("procedural_memory") is explicitly handled for
                creating procedural memories (typically requires 'agent_id'). Otherwise, memories
                are treated as general conversational/factual memories.
            prompt (str, optional): Prompt to use for the memory creation. Defaults to None.

        Note:
            `search()` and `get_all()` scope queries via `filters={"user_id": "...", "agent_id": "...", "run_id": "..."}` —
            they reject top-level `user_id`/`agent_id`/`run_id` arguments. `add()` accepts them top-level, but passing
            the same arguments to `search()`/`get_all()` raises a `ValueError`; use the `filters` form there instead.


        Returns:
            dict: A dictionary containing the result of the memory addition operation, typically
                  including a list of memory items affected (added, updated) under a "results" key.
                  Example for v1.1+: `{"results": [{"id": "...", "memory": "...", "event": "ADD"}]}`

        Raises:
            Mem0ValidationError: If input validation fails (invalid memory_type, messages format, etc.).
            VectorStoreError: If vector store operations fail.
            EmbeddingError: If embedding generation fails.
            LLMError: If LLM operations fail.
            DatabaseError: If database operations fail.
        """
        if timestamp is not None:
            raise ValueError(get_temporal_feature_error_message("sync", "add", "timestamp"))

        normalized_expiration_date = _normalize_expiration_date(expiration_date)
        temporal_usage_notice = detect_temporal_usage_from_metadata(metadata)
        processed_metadata, effective_filters = _build_filters_and_metadata(
            user_id=user_id,
            agent_id=agent_id,
            run_id=run_id,
            input_metadata=metadata,
        )
        if normalized_expiration_date is not None:
            processed_metadata["expiration_date"] = normalized_expiration_date

        if memory_type is not None and memory_type != MemoryType.PROCEDURAL.value:
            raise Mem0ValidationError(
                message=f"Invalid 'memory_type'. Please pass {MemoryType.PROCEDURAL.value} to create procedural memories.",
                error_code="VALIDATION_002",
                details={"provided_type": memory_type, "valid_type": MemoryType.PROCEDURAL.value},
                suggestion=f"Use '{MemoryType.PROCEDURAL.value}' to create procedural memories."
            )

        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]

        elif isinstance(messages, dict):
            messages = [messages]

        elif not isinstance(messages, list):
            raise Mem0ValidationError(
                message="messages must be str, dict, or list[dict]",
                error_code="VALIDATION_003",
                details={"provided_type": type(messages).__name__, "valid_types": ["str", "dict", "list[dict]"]},
                suggestion="Convert your input to a string, dictionary, or list of dictionaries."
            )

        if agent_id is not None and memory_type == MemoryType.PROCEDURAL.value:
            results = self._create_procedural_memory(messages, metadata=processed_metadata, prompt=prompt)
            scale_threshold_notice = detect_scale_threshold_from_add_result(self, results)
            if temporal_usage_notice:
                display_temporal_usage_notice(self, "sync", "add", *temporal_usage_notice)
            elif scale_threshold_notice:
                display_scale_threshold_notice(self, "sync", "add", *scale_threshold_notice)
            else:
                display_first_run_notice(self, "sync", "add")
            return results

        if self.config.llm.config.get("enable_vision"):
            messages = parse_vision_messages(messages, self.llm, self.config.llm.config.get("vision_details"))
        else:
            messages = parse_vision_messages(messages)

        vector_store_result = self._add_to_vector_store(messages, processed_metadata, effective_filters, infer, prompt=prompt)
        scale_threshold_notice = detect_scale_threshold_from_add_result(self, vector_store_result)
        if temporal_usage_notice:
            display_temporal_usage_notice(self, "sync", "add", *temporal_usage_notice)
        elif scale_threshold_notice:
            display_scale_threshold_notice(self, "sync", "add", *scale_threshold_notice)
        else:
            display_first_run_notice(self, "sync", "add")
        return {"results": vector_store_result}

    def _add_to_vector_store(self, messages, metadata, filters, infer, prompt=None):
        if not infer:
            returned_memories = []
            for message_dict in messages:
                if (
                    not isinstance(message_dict, dict)
                    or message_dict.get("role") is None
                    or message_dict.get("content") is None
                ):
                    logger.warning(f"Skipping invalid message format: {message_dict}")
                    continue

                if message_dict["role"] == "system":
                    continue

                per_msg_meta = deepcopy(metadata)
                per_msg_meta["role"] = message_dict["role"]

                actor_name = message_dict.get("name")
                if actor_name:
                    per_msg_meta["actor_id"] = actor_name

                msg_content = message_dict["content"]
                msg_embeddings = self.embedding_model.embed(msg_content, "add")
                mem_id = self._create_memory(msg_content, {msg_content: msg_embeddings}, per_msg_meta)

                returned_memories.append(
                    {
                        "id": mem_id,
                        "memory": msg_content,
                        "event": "ADD",
                        "actor_id": actor_name if actor_name else None,
                        "role": message_dict["role"],
                    }
                )
            return returned_memories

        # === V3 PHASED BATCH PIPELINE ===

        # Phase 0: Context gathering
        session_scope = _build_session_scope(filters)
        last_messages = self.db.get_last_messages(session_scope, limit=10)
        parsed_messages = parse_messages(messages)

        # Phase 1: Existing memory retrieval
        # 只取有效事实，且 top_k 提升为候选池大小：该结果既作 1a 的去重参考，也作 1b 的候选池。
        # 检索必须排除已失效记录，否则 hash 去重会把「同一事实失效后重新出现」挡在门外（方案 3.3）。
        search_filters = {k: v for k, v in filters.items() if k in ("user_id", "agent_id", "run_id") and v}
        query_embedding = self.embedding_model.embed(parsed_messages, "search")
        existing_results = self.vector_store.search(
            query=parsed_messages,
            vectors=query_embedding,
            top_k=BITEMP_CANDIDATE_K,
            filters=_with_bitemporal_filter(search_filters),
        )

        existing_memories = []
        for mem in existing_results:
            mid = mem.id
            score = float(getattr(mem, "score", 0.0) or 0.0)
            existing_memories.append(
                {"id": mid, "text": mem.payload.get("data", ""), "score": round(score, 4)}
            )

        # Phase 2: LLM extraction (single call)
        is_agent_scoped = bool(filters.get("agent_id")) and not filters.get("user_id")
        system_prompt = ADDITIVE_EXTRACTION_PROMPT
        if is_agent_scoped:
            system_prompt += AGENT_CONTEXT_SUFFIX

        custom_instr = prompt or self.custom_instructions

        user_prompt = generate_additive_extraction_prompt(
            existing_memories=existing_memories,
            new_messages=parsed_messages,
            last_k_messages=last_messages,
            custom_instructions=custom_instr,
        )

        try:
            response = self.llm.generate_response(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )
        except Exception as e:
            # Re-raise so callers can implement provider fallback / retry.
            # The original silent ``return []`` made upstream callers unable to
            # distinguish "LLM unavailable" (429/5xx/timeout) from "LLM
            # extracted no facts" -- both surfaced as an empty list.
            logger.error(f"LLM extraction failed: {e}")
            raise LLMError(f"LLM extraction failed: {e}") from e

        # Parse response
        try:
            response = remove_code_blocks(response)
            if not response or not response.strip():
                extracted_memories = []
            else:
                try:
                    extracted_memories = json.loads(response, strict=False).get("memory", [])
                except json.JSONDecodeError:
                    extracted_json = extract_json(response)
                    extracted_memories = json.loads(extracted_json, strict=False).get("memory", [])
        except Exception as e:
            logger.error(f"Error parsing extraction response: {e}")
            extracted_memories = []

        if not extracted_memories:
            # Save messages even if nothing extracted
            self.db.save_messages(messages, session_scope)
            return []

        # Phase 3: Batch embed all extracted memory texts
        mem_texts = [m.get("text", "") for m in extracted_memories if m.get("text")]
        try:
            mem_embeddings_list = self.embedding_model.embed_batch(mem_texts, "add")
            embed_map = dict(zip(mem_texts, mem_embeddings_list))
        except Exception:
            # Fallback: embed individually
            embed_map = {}
            for text in mem_texts:
                try:
                    embed_map[text] = self.embedding_model.embed(text, "add")
                except Exception as e:
                    logger.warning(f"Failed to embed memory text: {e}")

        # Phase 4: Per-memory CPU processing + Phase 5: Hash dedup
        # Build set of existing hashes for dedup
        existing_hashes = set()
        for mem in existing_results:
            h = mem.payload.get("hash") if hasattr(mem, "payload") and mem.payload else None
            if h:
                existing_hashes.add(h)

        records = []  # (memory_id, text, embedding, payload)
        seen_hashes = set()  # dedup within the current batch
        for mem in extracted_memories:
            text = mem.get("text")
            if not text or text not in embed_map:
                continue

            mem_hash = hashlib.md5(text.encode()).hexdigest()
            if mem_hash in existing_hashes or mem_hash in seen_hashes:
                logger.debug(f"Skipping duplicate memory (hash match): {text[:50]}")
                continue
            seen_hashes.add(mem_hash)

            text_lemmatized = lemmatize_for_bm25(text)

            memory_id = str(uuid.uuid4())
            mem_metadata = deepcopy(metadata)
            mem_metadata["data"] = text
            mem_metadata["text_lemmatized"] = text_lemmatized
            mem_metadata["hash"] = mem_hash
            if "created_at" not in mem_metadata:
                mem_metadata["created_at"] = datetime.now(timezone.utc).isoformat()
            mem_metadata["updated_at"] = mem_metadata["created_at"]
            if mem.get("attributed_to"):
                mem_metadata["attributed_to"] = mem["attributed_to"]
            # valid_at 是事实在真实世界的生效日期，不是入库时间；判定不出时保持 null，
            # 读路径由 created_at 兜底（方案 §4.1）。
            mem_metadata["valid_at"] = _coerce_bitemporal_date(mem.get("valid_at"))

            records.append((memory_id, text, embed_map[text], mem_metadata))

        if not records:
            self.db.save_messages(messages, session_scope)
            return []

        # Phase 5: Independent contradiction detection (1b, design §3.2). A second LLM call
        # with its own system prompt; it only reports pairs, never a retention verdict.
        # Skipped when either side of the comparison is empty.
        contradictions = []
        if existing_results:
            contradictions = self._detect_contradictions(records, existing_results)

        # Phase 6: Batch persist
        all_vectors = [r[2] for r in records]
        all_ids = [r[0] for r in records]
        all_payloads = [r[3] for r in records]

        try:
            self.vector_store.insert(
                vectors=all_vectors,
                ids=all_ids,
                payloads=all_payloads,
            )
        except Exception:
            # Fallback: insert one by one
            for mid, vec, pay in zip(all_ids, all_vectors, all_payloads):
                try:
                    self.vector_store.insert(vectors=[vec], ids=[mid], payloads=[pay])
                except Exception as e:
                    logger.error(f"Failed to insert memory {mid}: {e}")

        # Batch history
        history_records = [
            {
                "memory_id": r[0],
                "old_memory": None,
                "new_memory": r[1],
                "event": "ADD",
                "created_at": r[3].get("created_at"),
                "is_deleted": 0,
            }
            for r in records
        ]
        try:
            self.db.batch_add_history(history_records)
        except Exception:
            # Fallback: add one by one
            for hr in history_records:
                try:
                    self.db.add_history(hr["memory_id"], None, hr["new_memory"], "ADD", created_at=hr.get("created_at"))
                except Exception as e:
                    logger.error(f"Failed to add history for {hr['memory_id']}: {e}")

        # Phase 6b: Deterministic disposition (1c, 方案 §3.2 的 Phase 7). Pure code: the
        # pairs from 1b are turned into payload-only invalidations. Text, hash, created_at
        # and the vectors are untouched and no history row is written (R9/R10).
        for update in _apply_contradictions(
            contradictions,
            {r[0]: r[3] for r in records},
            {mem.id: dict(mem.payload or {}) for mem in existing_results},
        ):
            invalidation = {
                "invalid_at": update["invalid_at"],
                "superseded_by": update["superseded_by"],
                "invalid_reason": update["invalid_reason"],
            }
            try:
                self.vector_store.update(vector_id=update["memory_id"], vector=None, payload=invalidation)
            except Exception as e:
                logger.error(f"Failed to invalidate memory {update['memory_id']}: {e}")

        # Phase 7: Batch entity linking
        try:
            all_texts = [r[1] for r in records]
            all_entities = extract_entities_batch(all_texts)

            # 7a: Global dedup — collect unique entities across all memories
            global_entities = {}  # normalized_key -> (entity_type, entity_text, set of memory_ids)
            for idx, (memory_id, text, embedding, payload) in enumerate(records):
                entities = all_entities[idx] if idx < len(all_entities) else []
                for entity_type, entity_text in entities:
                    key = self._normalize_entity_text(entity_text)
                    if key in global_entities:
                        global_entities[key][2].add(memory_id)
                    else:
                        global_entities[key] = [entity_type, entity_text, {memory_id}]

            if global_entities:
                ordered_keys = list(global_entities.keys())
                entity_texts = [global_entities[k][1] for k in ordered_keys]

                # 7b: Single batch embed for all unique entities
                try:
                    entity_embeddings = self.embedding_model.embed_batch(entity_texts, "add")
                except Exception:
                    # Fallback: embed individually, use None for failures
                    entity_embeddings = []
                    for t in entity_texts:
                        try:
                            entity_embeddings.append(self.embedding_model.embed(t, "add"))
                        except Exception:
                            entity_embeddings.append(None)


                if len(entity_embeddings) != len(ordered_keys):
                    logger.warning(
                        "embed_batch returned %d vectors for %d entity texts — "
                        "padding/truncating to avoid dropping entity links",
                        len(entity_embeddings),
                        len(ordered_keys),
                    )
                    entity_embeddings = list(entity_embeddings[: len(ordered_keys)])
                    entity_embeddings += [None] * (len(ordered_keys) - len(entity_embeddings))

                # Filter out entities with failed embeddings
                valid = [(i, k) for i, k in enumerate(ordered_keys) if entity_embeddings[i] is not None]
                if valid:
                    valid_indices, valid_keys = zip(*valid)
                    valid_vectors = [entity_embeddings[i] for i in valid_indices]
                    exact_matches = self._existing_entities_by_text(search_filters)

                    # 7c: Batch search for existing entities
                    valid_texts = [global_entities[k][1] for k in valid_keys]
                    existing_matches = self.entity_store.search_batch(
                        queries=valid_texts,
                        vectors_list=valid_vectors,
                        top_k=1,
                        filters=search_filters,
                    )

                    # 7d: Separate into inserts vs updates
                    to_insert_vectors, to_insert_ids, to_insert_payloads = [], [], []
                    for j, key in enumerate(valid_keys):
                        entity_type, entity_text, memory_ids = global_entities[key]
                        matches = existing_matches[j] if j < len(existing_matches) else []
                        exact_match = exact_matches.get(key)

                        semantic_match = matches[0] if matches and matches[0].score >= 0.95 else None
                        match = exact_match or semantic_match
                        if match:
                            # Update existing entity
                            payload = match.payload or {}
                            linked = set(payload.get("linked_memory_ids", []))
                            linked |= memory_ids
                            payload["linked_memory_ids"] = sorted(linked)
                            try:
                                self.entity_store.update(
                                    vector_id=match.id,
                                    vector=None,
                                    payload=payload,
                                )
                            except Exception as e:
                                logger.debug(f"Entity update failed for '{entity_text}': {e}")
                        else:
                            # New entity — collect for batch insert
                            to_insert_vectors.append(valid_vectors[j])
                            to_insert_ids.append(str(uuid.uuid4()))
                            to_insert_payloads.append({
                                "data": entity_text,
                                "entity_type": entity_type,
                                "linked_memory_ids": sorted(memory_ids),
                                **search_filters,
                            })

                    # 7e: Single batch insert for all new entities
                    if to_insert_vectors:
                        try:
                            self.entity_store.insert(
                                vectors=to_insert_vectors,
                                ids=to_insert_ids,
                                payloads=to_insert_payloads,
                            )
                        except Exception as e:
                            logger.warning(f"Batch entity insert failed: {e}")
        except Exception as e:
            logger.warning(f"Batch entity linking failed: {e}")

        # Phase 8: Save messages + return
        self.db.save_messages(messages, session_scope)

        returned_memories = [
            {"id": r[0], "memory": r[1], "event": "ADD"}
            for r in records
        ]

        keys, encoded_ids = process_telemetry_filters(filters)
        capture_event(
            "mem0.add",
            self,
            {"version": self.api_version, "keys": keys, "encoded_ids": encoded_ids, "sync_type": "sync"},
        )
        return returned_memories

    def _detect_contradictions(self, records, existing_results) -> List[Dict[str, str]]:
        """1b：一次独立的 LLM 调用，只取回矛盾对 `[{new_id, old_id}]`。

        与 1a 的 system prompt 互不共享。失败（异常或不可解析输出）不中断写入，只是本次
        不处置任何记录——1b 是辅助判定，不能让用户的新事实因它而丢失。
        """
        new_facts = [
            {"id": record[0], "text": record[1], "valid_at": record[3].get("valid_at")}
            for record in records
        ]
        existing_facts = [
            {
                "id": mem.id,
                "text": (mem.payload or {}).get("data", ""),
                "valid_at": (mem.payload or {}).get("valid_at"),
                "created_at": (mem.payload or {}).get("created_at"),
            }
            for mem in existing_results
        ]
        user_prompt = generate_contradiction_detection_prompt(
            new_facts=new_facts, existing_facts=existing_facts
        )
        try:
            response = self.llm.generate_response(
                messages=[
                    {"role": "system", "content": CONTRADICTION_DETECTION_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )
        except Exception as e:
            logger.error(f"LLM contradiction detection failed: {e}")
            return []
        return _parse_contradiction_pairs(response)

    def get(self, memory_id):
        """
        Retrieve a memory by ID.

        Args:
            memory_id (str): ID of the memory to retrieve.

        Returns:
            dict: Retrieved memory.
        """
        capture_event("mem0.get", self, {"memory_id": memory_id, "sync_type": "sync"})
        memory = self.vector_store.get(vector_id=memory_id)
        if not memory:
            display_first_run_notice(self, "sync", "get")
            return None

        promoted_payload_keys = BI_TEMPORAL_PAYLOAD_KEYS

        core_and_promoted_keys = {"data", "hash", "created_at", "updated_at", "id", "text_lemmatized", "attributed_to", *promoted_payload_keys}

        result_item = MemoryItem(
            id=memory.id,
            memory=memory.payload.get("data", ""),
            hash=memory.payload.get("hash"),
            created_at=memory.payload.get("created_at"),
            updated_at=memory.payload.get("updated_at"),
        ).model_dump()

        for key in promoted_payload_keys:
            if key in memory.payload:
                result_item[key] = memory.payload[key]

        # 一等字段：始终在顶层出现，未赋值时为 null（与 bi-temporal 四字段同规则，设计 F7）。
        for key in READ_ALWAYS_PRESENT_FIELDS:
            result_item.setdefault(key, None)

        additional_metadata = {k: v for k, v in memory.payload.items() if k not in core_and_promoted_keys}
        if additional_metadata:
            result_item["metadata"] = additional_metadata

        display_first_run_notice(self, "sync", "get")
        return result_item

    def get_all(
        self,
        *,
        filters: Optional[Dict[str, Any]] = None,
        top_k: int = 20,
        show_expired: bool = False,
        as_of: Optional[str] = None,
        include_invalidated: bool = False,
        **kwargs,
    ):
        """
        List all memories.

        Args:
            filters (dict): Filter dict containing entity IDs and optional metadata filters.
                Must contain at least one of: user_id, agent_id, run_id.
                Example: filters={"user_id": "u1", "agent_id": "a1"}
            top_k (int, optional): The maximum number of memories to return. Defaults to 20.
            show_expired (bool, optional): Include expired memories. Defaults to False.
            as_of (str, optional): `YYYY-MM-DD` or ISO8601 instant T. Returns the facts that
                were valid at T (point-in-time). None means the current moment.
            include_invalidated (bool, optional): Return invalidated facts too, ignoring the
                validity filter. Defaults to False.

        Returns:
            dict: A dictionary containing a list of memories under the "results" key.
                  Example for v1.1+: `{"results": [{"id": "...", "memory": "...", ...}]}`

        Raises:
            ValueError: If filters doesn't contain at least one of user_id, agent_id, run_id,
                or if top_k is invalid.
        """
        # Reject top-level entity params - must use filters instead
        _reject_top_level_entity_params(kwargs, "get_all")

        # Validate top_k
        _validate_search_params(top_k=top_k)

        # Validate and trim entity IDs in filters
        effective_filters = dict(filters) if filters else {}
        if "user_id" in effective_filters:
            effective_filters["user_id"] = _validate_and_trim_entity_id(
                effective_filters["user_id"], "user_id"
            )
        if "agent_id" in effective_filters:
            effective_filters["agent_id"] = _validate_and_trim_entity_id(
                effective_filters["agent_id"], "agent_id"
            )
        if "run_id" in effective_filters:
            effective_filters["run_id"] = _validate_and_trim_entity_id(
                effective_filters["run_id"], "run_id"
            )

        # Validate filters contains at least one entity ID
        if not any(key in effective_filters for key in ("user_id", "agent_id", "run_id")):
            raise ValueError(
                "filters must contain at least one of: user_id, agent_id, run_id. "
                "Example: filters={'user_id': 'u1'}"
            )

        limit = top_k
        fetch_limit = limit if show_expired else max(limit * 4, 60)
        scale_threshold_notice = detect_scale_threshold_from_top_k(top_k)

        keys, encoded_ids = process_telemetry_filters(effective_filters)
        capture_event(
            "mem0.get_all", self, {"limit": limit, "keys": keys, "encoded_ids": encoded_ids, "sync_type": "sync"}
        )

        all_memories_result = self._get_all_from_vector_store(effective_filters, fetch_limit, show_expired, limit, as_of, include_invalidated)

        if scale_threshold_notice:
            display_scale_threshold_notice(self, "sync", "get_all", *scale_threshold_notice)
        else:
            display_first_run_notice(self, "sync", "get_all")
        return {"results": all_memories_result}

    def _get_all_from_vector_store(
        self, filters, limit, show_expired=False, output_limit=None, as_of=None, include_invalidated=False
    ):
        memories_result = self.vector_store.list(
            filters=_with_bitemporal_filter(filters, as_of, include_invalidated), top_k=limit
        )

        # Handle different vector store return formats by inspecting first element
        if isinstance(memories_result, (tuple, list)) and len(memories_result) > 0:
            first_element = memories_result[0]

            # If first element is a container, unwrap one level
            if isinstance(first_element, (list, tuple)):
                actual_memories = first_element
            else:
                # First element is a memory object, structure is already flat
                actual_memories = memories_result
        else:
            actual_memories = memories_result

        promoted_payload_keys = BI_TEMPORAL_PAYLOAD_KEYS
        core_and_promoted_keys = {"data", "hash", "created_at", "updated_at", "id", "text_lemmatized", "attributed_to", *promoted_payload_keys}

        formatted_memories = []
        for mem in actual_memories:
            if not show_expired and _payload_is_expired(mem.payload):
                continue
            memory_item_dict = MemoryItem(
                id=mem.id,
                memory=mem.payload.get("data", ""),
                hash=mem.payload.get("hash"),
                created_at=mem.payload.get("created_at"),
                updated_at=mem.payload.get("updated_at"),
            ).model_dump(exclude={"score"})

            for key in promoted_payload_keys:
                if key in mem.payload:
                    memory_item_dict[key] = mem.payload[key]

            for key in READ_ALWAYS_PRESENT_FIELDS:
                memory_item_dict.setdefault(key, None)

            additional_metadata = {k: v for k, v in mem.payload.items() if k not in core_and_promoted_keys}
            if additional_metadata:
                memory_item_dict["metadata"] = additional_metadata

            formatted_memories.append(memory_item_dict)
            if output_limit is not None and len(formatted_memories) >= output_limit:
                break

        return formatted_memories

    def search(
        self,
        query: str,
        *,
        top_k: int = 20,
        filters: Optional[Dict[str, Any]] = None,
        threshold: float = 0.1,
        rerank: bool = False,
        explain: bool = False,
        reference_date: Optional[Any] = None,
        show_expired: bool = False,
        as_of: Optional[str] = None,
        include_invalidated: bool = False,
        **kwargs,
    ):
        """
        Searches for memories based on a query.

        Args:
            query (str): Query to search for.
            top_k (int, optional): Maximum number of results to return. Defaults to 20.
            filters (dict): Filter dict containing entity IDs and optional metadata filters.
                Must contain at least one of: user_id, agent_id, run_id.
                Example: filters={"user_id": "u1", "agent_id": "a1"}

                Enhanced metadata filtering with operators:
                - {"key": "value"} - exact match
                - {"key": {"eq": "value"}} - equals
                - {"key": {"ne": "value"}} - not equals
                - {"key": {"in": ["val1", "val2"]}} - in list
                - {"key": {"nin": ["val1", "val2"]}} - not in list
                - {"key": {"gt": 10}} - greater than
                - {"key": {"gte": 10}} - greater than or equal
                - {"key": {"lt": 10}} - less than
                - {"key": {"lte": 10}} - less than or equal
                - {"key": {"contains": "text"}} - contains text
                - {"key": {"icontains": "text"}} - case-insensitive contains
                - {"key": "*"} - wildcard match (any value)
                - {"AND": [filter1, filter2]} - logical AND
                - {"OR": [filter1, filter2]} - logical OR
                - {"NOT": [filter1]} - logical NOT
            threshold (float, optional): Minimum score for a memory to be included. Defaults to 0.1.
            rerank (bool, optional): Whether to rerank results. Defaults to False.
            explain (bool, optional): Whether to include score_details for each result. Defaults to False.
            reference_date (Any, optional): Platform-only temporal parameter. Not supported in OSS.
            show_expired (bool, optional): Include expired memories. Defaults to False.
            as_of (str, optional): `YYYY-MM-DD` or ISO8601 instant T. Returns the facts that
                were valid at T (point-in-time). None means the current moment.
            include_invalidated (bool, optional): Return invalidated facts too, ignoring the
                validity filter. Defaults to False.

        Returns:
            dict: A dictionary containing the search results under a "results" key.
                  Example for v1.1+: `{"results": [{"id": "...", "memory": "...", "score": 0.8, ...}]}`

        Raises:
            ValueError: If filters doesn't contain at least one of user_id, agent_id, run_id,
                or if threshold/top_k values are invalid.
        """
        if reference_date is not None:
            raise ValueError(get_temporal_feature_error_message("sync", "search", "reference_date"))

        # Reject top-level entity params - must use filters instead
        _reject_top_level_entity_params(kwargs, "search")

        # Validate search parameters (before applying defaults)
        _validate_search_params(threshold=threshold, top_k=top_k)
        query = _validate_and_trim_search_query(query)
        temporal_usage_notice = detect_temporal_usage_from_search(query, filters)

        # Validate and trim entity IDs in filters
        effective_filters = filters.copy() if filters else {}
        if "user_id" in effective_filters:
            effective_filters["user_id"] = _validate_and_trim_entity_id(
                effective_filters["user_id"], "user_id"
            )
        if "agent_id" in effective_filters:
            effective_filters["agent_id"] = _validate_and_trim_entity_id(
                effective_filters["agent_id"], "agent_id"
            )
        if "run_id" in effective_filters:
            effective_filters["run_id"] = _validate_and_trim_entity_id(
                effective_filters["run_id"], "run_id"
            )
        if not any(key in effective_filters for key in ("user_id", "agent_id", "run_id")):
            raise ValueError(
                "filters must contain at least one of: user_id, agent_id, run_id. "
                "Example: filters={'user_id': 'u1'}"
            )

        limit = top_k
        scale_threshold_notice = detect_scale_threshold_from_top_k(top_k)

        # Apply enhanced metadata filtering if advanced operators are detected
        if self._has_advanced_operators(effective_filters):
            processed_filters = self._process_metadata_filters(effective_filters)
            # Remove logical/operator keys that have been reprocessed
            for logical_key in ("AND", "OR", "NOT"):
                effective_filters.pop(logical_key, None)
            for fk in list(effective_filters.keys()):
                if fk not in ("AND", "OR", "NOT", "user_id", "agent_id", "run_id") and isinstance(effective_filters.get(fk), dict):
                    effective_filters.pop(fk, None)
            effective_filters.update(processed_filters)

        keys, encoded_ids = process_telemetry_filters(effective_filters)
        capture_event(
            "mem0.search",
            self,
            {
                "limit": limit,
                "version": self.api_version,
                "keys": keys,
                "encoded_ids": encoded_ids,
                "sync_type": "sync",
                "threshold": threshold,
                "explain": explain,
                "advanced_filters": bool(filters and self._has_advanced_operators(filters)),
            },
        )

        search_start = time.perf_counter()
        original_memories = self._search_vector_store(
            query,
            _with_bitemporal_filter(effective_filters, as_of, include_invalidated),
            limit,
            threshold,
            explain=explain,
            show_expired=show_expired,
        )
        search_elapsed_seconds = time.perf_counter() - search_start

        # Apply reranking if enabled and reranker is available
        if rerank and self.reranker and original_memories:
            try:
                reranked_memories = self.reranker.rerank(query, original_memories, limit)
                original_memories = reranked_memories
            except Exception as e:
                logger.warning(f"Reranking failed, using original results: {e}")

        # 强化只针对最终入选（本次 rerank 之后）的条目；关闭态下为空操作，且异步派发
        # 不进入响应时延路径（设计 §4.2-4.3）。
        _reinforce_search_hits(self, original_memories)

        if temporal_usage_notice:
            display_temporal_usage_notice(self, "sync", "search", *temporal_usage_notice)
        elif scale_threshold_notice:
            display_scale_threshold_notice(self, "sync", "search", *scale_threshold_notice)
        elif search_elapsed_seconds > PERFORMANCE_SLOW_QUERY_THRESHOLD_SECONDS:
            display_performance_slow_query_notice(
                self,
                "sync",
                "search",
                search_elapsed_seconds,
                top_k,
                len(original_memories),
            )
        else:
            display_first_run_notice(self, "sync", "search")
        return {"results": original_memories}

    def _process_metadata_filters(self, metadata_filters: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process enhanced metadata filters and convert them to vector store compatible format.

        Args:
            metadata_filters: Enhanced metadata filters with operators

        Returns:
            Dict of processed filters compatible with vector store
        """
        processed_filters = {}

        def process_condition(key: str, condition: Any) -> Dict[str, Any]:
            if not isinstance(condition, dict):
                # Simple equality: {"key": "value"}
                if condition == "*":
                    # Wildcard: match everything for this field (implementation depends on vector store)
                    return {key: "*"}
                return {key: condition}

            result = {}
            for operator, value in condition.items():
                # Map platform operators to universal format that can be translated by each vector store
                operator_map = {
                    "eq": "eq", "ne": "ne", "gt": "gt", "gte": "gte",
                    "lt": "lt", "lte": "lte", "in": "in", "nin": "nin",
                    "contains": "contains", "icontains": "icontains"
                }

                if operator in operator_map:
                    result.setdefault(key, {})[operator_map[operator]] = value
                else:
                    raise ValueError(f"Unsupported metadata filter operator: {operator}")
            return result

        def merge_filters(target: Dict[str, Any], source: Dict[str, Any]) -> None:
            """Merge source into target, deep-merging nested operator dicts for the same key."""
            for key, value in source.items():
                if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                    target[key].update(value)
                else:
                    target[key] = value

        for key, value in metadata_filters.items():
            if key == "AND":
                # Logical AND: combine multiple conditions
                if not isinstance(value, list):
                    raise ValueError("AND operator requires a list of conditions")
                for condition in value:
                    for sub_key, sub_value in condition.items():
                        merge_filters(processed_filters, process_condition(sub_key, sub_value))
            elif key == "OR":
                # Logical OR: Pass through to vector store for implementation-specific handling
                if not isinstance(value, list) or not value:
                    raise ValueError("OR operator requires a non-empty list of conditions")
                # Store OR conditions in a way that vector stores can interpret
                processed_filters["$or"] = []
                for condition in value:
                    or_condition = {}
                    for sub_key, sub_value in condition.items():
                        merge_filters(or_condition, process_condition(sub_key, sub_value))
                    processed_filters["$or"].append(or_condition)
            elif key == "NOT":
                # Logical NOT: Pass through to vector store for implementation-specific handling
                if not isinstance(value, list) or not value:
                    raise ValueError("NOT operator requires a non-empty list of conditions")
                processed_filters["$not"] = []
                for condition in value:
                    not_condition = {}
                    for sub_key, sub_value in condition.items():
                        merge_filters(not_condition, process_condition(sub_key, sub_value))
                    processed_filters["$not"].append(not_condition)
            else:
                merge_filters(processed_filters, process_condition(key, value))

        return processed_filters

    def _has_advanced_operators(self, filters: Dict[str, Any]) -> bool:
        """
        Check if filters contain advanced operators that need special processing.
        
        Args:
            filters: Dictionary of filters to check
            
        Returns:
            bool: True if advanced operators are detected
        """
        if not isinstance(filters, dict):
            return False
            
        for key, value in filters.items():
            # Check for platform-style logical operators
            if key in ["AND", "OR", "NOT"]:
                return True
            # Check for comparison operators (without $ prefix for universal compatibility)
            if isinstance(value, dict):
                for op in value.keys():
                    if op in ["eq", "ne", "gt", "gte", "lt", "lte", "in", "nin", "contains", "icontains"]:
                        return True
            # Check for wildcard values
            if value == "*":
                return True
        return False

    def _search_vector_store(self, query, filters, limit, threshold=0.1, explain=False, show_expired=False):
        # Guard against None threshold (backward compat)
        if threshold is None:
            threshold = 0.1

        # Step 1: Preprocess query
        query_lemmatized = lemmatize_for_bm25(query)
        query_entities = extract_entities(query)

        # Step 2: Embed query
        embeddings = self.embedding_model.embed(query, "search")

        # Step 3: Semantic search (over-fetch for scoring pool)
        internal_limit = max(limit * 4, 60)
        semantic_results = self.vector_store.search(
            query=query, vectors=embeddings, top_k=internal_limit, filters=filters
        )

        # Step 4: Keyword search (if store supports it)
        keyword_results = self.vector_store.keyword_search(
            query=query_lemmatized, top_k=internal_limit, filters=filters
        )

        # Step 5: Compute BM25 scores from keyword results
        bm25_scores = {}
        if keyword_results is not None:
            midpoint, steepness = get_bm25_params(query, lemmatized=query_lemmatized)
            for mem in keyword_results:
                mem_id = str(mem.id) if hasattr(mem, 'id') else str(mem.get('id', ''))
                raw_score = mem.score if hasattr(mem, 'score') else mem.get('score', 0)
                if raw_score and raw_score > 0:
                    bm25_scores[mem_id] = normalize_bm25(raw_score, midpoint, steepness)

        # Step 6: Compute entity boosts
        entity_boosts = {}
        if query_entities:
            entity_boosts = self._compute_entity_boosts(query_entities, filters)

        # Step 7: Build candidate set from semantic results
        candidates = []
        for mem in semantic_results:
            payload = mem.payload if hasattr(mem, 'payload') else {}
            if not show_expired and _payload_is_expired(payload):
                continue
            mem_id = str(mem.id)
            candidates.append({
                "id": mem_id,
                "score": mem.score,
                "payload": payload,
            })

        # Step 8: Score and rank
        # 时间因子只在本检索路径生效；整批候选共用同一个 now 基准（设计 §3.5）。
        scored_results = score_and_rank(
            semantic_results=candidates,
            bm25_scores=bm25_scores,
            entity_boosts=entity_boosts,
            threshold=threshold,
            top_k=limit,
            explain=explain,
            decay_factors=self._decay_factors(candidates),
        )

        # Step 9: Format results
        promoted_payload_keys = BI_TEMPORAL_PAYLOAD_KEYS
        core_and_promoted_keys = {"data", "hash", "created_at", "updated_at", "id", "text_lemmatized", "attributed_to", *promoted_payload_keys}

        original_memories = []
        for scored in scored_results:
            payload = scored.get("payload") or {}

            if not payload.get("data"):
                continue  # Skip candidates with no payload data

            memory_item_dict = MemoryItem(
                id=scored["id"],
                memory=payload.get("data", ""),
                hash=payload.get("hash"),
                created_at=payload.get("created_at"),
                updated_at=payload.get("updated_at"),
                score=scored["score"],
            ).model_dump()

            for key in promoted_payload_keys:
                if key in payload:
                    memory_item_dict[key] = payload[key]

            for key in READ_ALWAYS_PRESENT_FIELDS:
                memory_item_dict.setdefault(key, None)

            additional_metadata = {k: v for k, v in payload.items() if k not in core_and_promoted_keys}
            if additional_metadata:
                if not memory_item_dict.get("metadata"):
                    memory_item_dict["metadata"] = {}
                memory_item_dict["metadata"].update(additional_metadata)
            if explain and "score_details" in scored:
                memory_item_dict["score_details"] = scored["score_details"]

            original_memories.append(memory_item_dict)

        return original_memories

    def _compute_entity_boosts(self, query_entities, filters):
        """Compute per-memory entity boosts from entity store search.

        For each extracted entity from the query:
        1. Embed the entity text
        2. Search the entity store (threshold >= 0.5)
        3. For each matched entity, boost its linked memories

        Returns:
            Dict mapping memory_id (str) -> max entity boost [0, 0.5].
        """
        # Deduplicate entities (max 8)
        seen = set()
        deduped = []
        for entity_type, entity_text in query_entities[:8]:
            key = self._normalize_entity_text(entity_text)
            if key and key not in seen:
                seen.add(key)
                deduped.append((entity_type, entity_text))

        if not deduped:
            return {}

        search_filters = _scope_filters(filters)
        memory_boosts = {}

        try:
            entity_texts = [text for _, text in deduped]
            embeddings = self.embedding_model.embed_batch(entity_texts, "search")

            if len(embeddings) != len(entity_texts):
                logger.warning(
                    "embed_batch returned %d vectors for %d texts — skipping entity boost",
                    len(embeddings),
                    len(entity_texts),
                )
                return memory_boosts

            entity_store = self.entity_store

            def _search_entity(entity_text, embedding):
                return entity_store.search(
                    query=entity_text, vectors=embedding, top_k=500, filters=search_filters
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = {
                    pool.submit(_search_entity, text, emb): text
                    for text, emb in zip(entity_texts, embeddings)
                }

                for future in concurrent.futures.as_completed(futures):
                    try:
                        matches = future.result()
                    except Exception as e:
                        logger.warning("Entity boost search failed for one entity: %s", e)
                        continue

                    for match in matches:
                        similarity = match.score if hasattr(match, 'score') else 0.0
                        if similarity < 0.5:
                            continue

                        payload = match.payload if hasattr(match, 'payload') else {}
                        linked_memory_ids = payload.get("linked_memory_ids", [])
                        if not isinstance(linked_memory_ids, list):
                            continue

                        num_linked = max(len(linked_memory_ids), 1)
                        memory_count_weight = 1.0 / (1.0 + 0.001 * ((num_linked - 1) ** 2))
                        boost = similarity * ENTITY_BOOST_WEIGHT * memory_count_weight

                        for memory_id in linked_memory_ids:
                            if memory_id:
                                memory_key = str(memory_id)
                                memory_boosts[memory_key] = max(memory_boosts.get(memory_key, 0.0), boost)

        except Exception as e:
            logger.warning(f"Entity boost computation failed: {e}")

        return memory_boosts

    def update(
        self,
        memory_id,
        text: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        expiration_date: Any = _UNSET,
        data: Optional[str] = None,
    ):
        """
        Update a memory by ID.

        Args:
            memory_id (str): ID of the memory to update.
            text (str, optional): New content to update the memory with.
            metadata (dict, optional): Metadata to update with the memory. Defaults to None.
                ``user_id``/``agent_id``/``run_id``/``actor_id`` are ignored here - they are
                immutable after creation.
            expiration_date (Any, optional): Date in YYYY-MM-DD format, or None to clear it.
            data (str, optional): Deprecated alias for ``text``. Will be removed in the next
                major release; use ``text`` instead.

        Returns:
            dict: Success message indicating the memory was updated.

        Example:
            >>> m.update(memory_id="mem_123", text="Likes to play tennis on weekends")
            {'message': 'Memory updated successfully!'}
        """
        capture_event("mem0.update", self, {"memory_id": memory_id, "sync_type": "sync"})

        if data is not None:
            logger.warning(
                "The `data` argument to update() is deprecated and will be removed in the "
                "next major release. Use `text` instead."
            )
            if text is None:
                text = data

        if text is None and metadata is None and expiration_date is _UNSET:
            raise ValueError("At least one of text, metadata, or expiration_date must be provided.")

        update_metadata = deepcopy(metadata) if metadata is not None else None
        if expiration_date is not _UNSET:
            update_metadata = update_metadata or {}
            update_metadata["expiration_date"] = _normalize_expiration_date(expiration_date)

        existing_embeddings = {}
        if text is not None:
            existing_embeddings[text] = self.embedding_model.embed(text, "update")

        self._update_memory(memory_id, text, existing_embeddings, update_metadata)
        display_first_run_notice(self, "sync", "update")
        return {"message": "Memory updated successfully!"}

    def delete(self, memory_id):
        """
        Delete a memory by ID.

        Args:
            memory_id (str): ID of the memory to delete.
        """
        capture_event("mem0.delete", self, {"memory_id": memory_id, "sync_type": "sync"})

        existing_memory = self.vector_store.get(vector_id=memory_id)
        if existing_memory is None:
            raise ValueError(f"Memory with id {memory_id} not found")

        self._delete_memory(memory_id, existing_memory)
        decay_usage_notice = detect_decay_usage_from_delete()
        if decay_usage_notice:
            display_decay_usage_notice(self, "sync", "delete", *decay_usage_notice)
        else:
            display_first_run_notice(self, "sync", "delete")
        return {"message": "Memory deleted successfully!"}

    def delete_all(self, user_id: Optional[str] = None, agent_id: Optional[str] = None, run_id: Optional[str] = None):
        """
        Delete all memories.

        Args:
            user_id (str, optional): ID of the user to delete memories for. Defaults to None.
            agent_id (str, optional): ID of the agent to delete memories for. Defaults to None.
            run_id (str, optional): ID of the run to delete memories for. Defaults to None.
        """
        user_id = _validate_and_trim_entity_id(user_id, "user_id")
        agent_id = _validate_and_trim_entity_id(agent_id, "agent_id")
        run_id = _validate_and_trim_entity_id(run_id, "run_id")

        filters: Dict[str, Any] = {}
        if user_id:
            filters["user_id"] = user_id
        if agent_id:
            filters["agent_id"] = agent_id
        if run_id:
            filters["run_id"] = run_id

        if not filters:
            raise ValueError(
                "At least one filter is required to delete all memories. If you want to delete all memories, use the `reset()` method."
            )

        keys, encoded_ids = process_telemetry_filters(filters)
        capture_event("mem0.delete_all", self, {"keys": keys, "encoded_ids": encoded_ids, "sync_type": "sync"})
        # Keep listing after each batch is deleted. Most vector stores cap
        # list() at 100 results by default, which silently truncates deletes.
        deleted_count = 0
        seen_batches = set()
        while True:
            memories = self.vector_store.list(
                filters=filters, top_k=DELETE_ALL_BATCH_SIZE
            )[0]
            if not memories:
                break
            batch_ids = tuple(sorted(str(memory.id) for memory in memories))
            if batch_ids in seen_batches:
                logger.warning("Stopping delete_all after a repeated memory batch")
                break
            seen_batches.add(batch_ids)
            for memory in memories:
                self._delete_memory(memory.id)
            deleted_count += len(memories)

        logger.info(f"Deleted {deleted_count} memories")

        decay_usage_notice = detect_decay_usage_from_delete_all(deleted_count)
        if decay_usage_notice:
            display_decay_usage_notice(self, "sync", "delete_all", *decay_usage_notice)
        else:
            display_first_run_notice(self, "sync", "delete_all")
        return {"message": "Memories deleted successfully!"}

    def history(self, memory_id):
        """
        Get the history of changes for a memory by ID.

        Args:
            memory_id (str): ID of the memory to get history for.

        Returns:
            list: List of changes for the memory.
        """
        capture_event("mem0.history", self, {"memory_id": memory_id, "sync_type": "sync"})
        history = self.db.get_history(memory_id)
        display_first_run_notice(self, "sync", "history")
        return history

    def _create_memory(self, data, existing_embeddings, metadata=None):
        logger.debug(f"Creating memory with {data=}")
        if data in existing_embeddings:
            embeddings = existing_embeddings[data]
        else:
            embeddings = self.embedding_model.embed(data, memory_action="add")
        memory_id = str(uuid.uuid4())
        new_metadata = deepcopy(metadata) if metadata is not None else {}
        new_metadata["data"] = data
        new_metadata["hash"] = hashlib.md5(data.encode()).hexdigest()
        if "created_at" not in new_metadata:
            new_metadata["created_at"] = datetime.now(timezone.utc).isoformat()
        new_metadata["updated_at"] = new_metadata["created_at"]
        new_metadata["text_lemmatized"] = lemmatize_for_bm25(data)

        self.vector_store.insert(
            vectors=[embeddings],
            ids=[memory_id],
            payloads=[new_metadata],
        )
        self.db.add_history(
            memory_id,
            None,
            data,
            "ADD",
            created_at=new_metadata.get("created_at"),
            updated_at=new_metadata.get("updated_at"),
            actor_id=new_metadata.get("actor_id"),
            role=new_metadata.get("role"),
        )
        return memory_id

    def _create_procedural_memory(self, messages, metadata=None, prompt=None):
        """
        Create a procedural memory

        Args:
            messages (list): List of messages to create a procedural memory from.
            metadata (dict): Metadata to create a procedural memory from.
            prompt (str, optional): Prompt to use for the procedural memory creation. Defaults to None.
        """
        logger.info("Creating procedural memory")

        parsed_messages = [
            {"role": "system", "content": prompt or PROCEDURAL_MEMORY_SYSTEM_PROMPT},
            *messages,
            {
                "role": "user",
                "content": "Create procedural memory of the above conversation.",
            },
        ]

        try:
            procedural_memory = self.llm.generate_response(messages=parsed_messages)
            procedural_memory = remove_code_blocks(procedural_memory)
        except Exception as e:
            logger.error(f"Error generating procedural memory summary: {e}")
            raise

        if not procedural_memory:
            raise ValueError(
                "The LLM returned no content for the procedural memory summary. "
                "The model may have declined the request or returned an empty response."
            )

        if metadata is None:
            raise ValueError("Metadata cannot be done for procedural memory.")

        metadata = {**metadata, "memory_type": MemoryType.PROCEDURAL.value}
        embeddings = self.embedding_model.embed(procedural_memory, memory_action="add")
        memory_id = self._create_memory(procedural_memory, {procedural_memory: embeddings}, metadata=metadata)
        capture_event("mem0._create_procedural_memory", self, {"memory_id": memory_id, "sync_type": "sync"})

        result = {"results": [{"id": memory_id, "memory": procedural_memory, "event": "ADD"}]}

        return result

    def _update_memory(self, memory_id, data, existing_embeddings, metadata=None):
        logger.info(f"Updating memory with {data=}")

        try:
            existing_memory = self.vector_store.get(vector_id=memory_id)
        except Exception:
            # Backing-store failure, not a bad memory_id: re-raise the original so the REST layer maps it to 5xx, not 4xx.
            logger.error(f"Error getting memory with ID {memory_id} during update.")
            raise

        if existing_memory is None:
            raise ValueError(f"Memory with id {memory_id} not found. Please provide a valid 'memory_id'")

        prev_value = existing_memory.payload.get("data")
        if data is None:
            data = prev_value
        if not isinstance(data, str):
            raise ValueError(f"Memory with id {memory_id} does not have text content to update")
        text_changed = data != prev_value

        new_metadata = deepcopy(existing_memory.payload)
        if metadata is not None:
            new_metadata.update(_strip_identity_keys(metadata, existing_memory.payload))

        new_metadata["data"] = data
        new_metadata["hash"] = hashlib.md5(data.encode()).hexdigest()
        new_metadata["text_lemmatized"] = lemmatize_for_bm25(data)
        new_metadata["created_at"] = existing_memory.payload.get("created_at")
        new_metadata["updated_at"] = datetime.now(timezone.utc).isoformat()

        if data in existing_embeddings:
            embeddings = existing_embeddings[data]
        else:
            embeddings = self.embedding_model.embed(data, "update")

        self.vector_store.update(
            vector_id=memory_id,
            vector=embeddings,
            payload=new_metadata,
        )
        logger.info(f"Updating memory with ID {memory_id=} with {data=}")

        self.db.add_history(
            memory_id,
            prev_value,
            data,
            "UPDATE",
            created_at=new_metadata["created_at"],
            updated_at=new_metadata["updated_at"],
            actor_id=new_metadata.get("actor_id"),
            role=new_metadata.get("role"),
        )

        # Entity-store cleanup: strip this memory's id from old-text entities,
        # then re-extract entities from the new text and link them back.
        session_filters = {k: new_metadata[k] for k in ("user_id", "agent_id", "run_id") if new_metadata.get(k)}
        if text_changed:
            self._remove_memory_from_entity_store(memory_id, session_filters)
            self._link_entities_for_memory(memory_id, data, session_filters)

        return memory_id

    def _delete_memory(self, memory_id, existing_memory=None):
        logger.info(f"Deleting memory with {memory_id=}")
        if existing_memory is None:
            existing_memory = self.vector_store.get(vector_id=memory_id)
            if existing_memory is None:
                raise ValueError(f"Memory with id {memory_id} not found. Please provide a valid 'memory_id'")
        prev_value = existing_memory.payload.get("data", "")
        created_at = _normalize_iso_timestamp_to_utc(existing_memory.payload.get("created_at"))
        updated_at = datetime.now(timezone.utc).isoformat()
        payload = existing_memory.payload or {}
        session_filters = {k: payload[k] for k in ("user_id", "agent_id", "run_id") if payload.get(k)}
        self.vector_store.delete(vector_id=memory_id)
        self.db.add_history(
            memory_id,
            prev_value,
            None,
            "DELETE",
            created_at=created_at,
            updated_at=updated_at,
            actor_id=existing_memory.payload.get("actor_id"),
            role=existing_memory.payload.get("role"),
            is_deleted=1,
        )

        # Entity-store cleanup: strip this memory's id from any entity records
        # that linked to it. Non-fatal — the helper swallows errors.
        self._remove_memory_from_entity_store(memory_id, session_filters)

        return memory_id

    def reset(self):
        """
        Reset the memory store by:
            Deletes the vector store collection
            Resets the database
            Recreates the vector store with a new client
        """
        logger.warning("Resetting all memories")

        self.db.reset()
        self.db.close()
        self.db = SQLiteManager(self.config.history_db_path)

        if hasattr(self.vector_store, "reset"):
            self.vector_store = VectorStoreFactory.reset(self.vector_store)
        else:
            logger.warning("Vector store does not support reset. Skipping.")
            self.vector_store.delete_col()
            self.vector_store = VectorStoreFactory.create(
                self.config.vector_store.provider, self.config.vector_store.config
            )
        # Reset entity store if initialized
        if self._entity_store is not None:
            try:
                self._entity_store.reset()
            except Exception as e:
                logger.warning(f"Failed to reset entity store: {e}")
            self._entity_store = None

        capture_event("mem0.reset", self, {"sync_type": "sync"})
        display_first_run_notice(self, "sync", "reset")

    def close(self):
        """Release resources held by this Memory instance (SQLite connections, etc.)."""
        if hasattr(self, "db") and self.db is not None:
            self.db.close()
            self.db = None

    def chat(self, query):
        raise NotImplementedError("Chat function not implemented yet.")


class AsyncMemory(MemoryBase):
    def __init__(self, config: MemoryConfig = MemoryConfig()):
        self.config = config

        self.embedding_model = EmbedderFactory.create(
            self.config.embedder.provider,
            self.config.embedder.config,
            self.config.vector_store.config,
        )
        self.vector_store = VectorStoreFactory.create(
            self.config.vector_store.provider, self.config.vector_store.config
        )
        self.llm = LlmFactory.create(self.config.llm.provider, self.config.llm.config)
        self.db = SQLiteManager(self.config.history_db_path)
        self.collection_name = self.config.vector_store.config.collection_name
        self.api_version = self.config.version
        self.custom_instructions = self.config.custom_instructions
        self._entity_store = None

        # 检索强化的进程内冷却表：memory_id -> 上次写入的单调时刻（设计 §4.3，与
        # 同步实现的语义一致）。
        self._decay_write_at: Dict[str, float] = {}
        self._decay_write_lock = threading.Lock()

        # Initialize reranker if configured
        self.reranker = None
        if config.reranker:
            self.reranker = RerankerFactory.create(
                config.reranker.provider,
                config.reranker.config
            )

        if MEM0_TELEMETRY:
            telemetry_config = _safe_deepcopy_config(self.config.vector_store.config)
            telemetry_config.collection_name = "mem0migrations"
            if self.config.vector_store.provider in ["faiss", "qdrant"]:
                provider_path = f"migrations_{self.config.vector_store.provider}"
                telemetry_config.path = os.path.join(mem0_dir, provider_path)
                os.makedirs(telemetry_config.path, exist_ok=True)
            self._telemetry_vector_store = VectorStoreFactory.create(self.config.vector_store.provider, telemetry_config)

        if getattr(type(self.vector_store), "keyword_search", None) is VectorStoreBase.keyword_search:
            logger.warning(
                "The '%s' vector store does not support keyword search. "
                "Hybrid (BM25) scoring will be disabled and search will use "
                "semantic similarity only. To enable hybrid search, switch to a "
                "store with keyword_search support (e.g. qdrant, elasticsearch, pgvector).",
                self.config.vector_store.provider,
            )

        capture_event("mem0.init", self, {"sync_type": "async"})

    @property
    def project(self):
        return _AsyncOSSProject()

    @property
    def decay_config(self) -> DecayConfig:
        """记忆衰减配置（设计 `docs/design/memory-decay.md`，配置段 `MemoryConfig.decay`）。"""
        return self.config.decay

    def _decay_factors(self, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Dict[str, Any]]]:
        """本次检索的时间因子表；关闭态返回 None，使打分器走原有的纯混合分路径。"""
        config = _decay_config_of(self)
        if config is None or not config.enabled:
            return None
        return _decay_factors_for_candidates(candidates, config, datetime.now(timezone.utc))

    @property
    def entity_store(self):
        """Lazily initialize entity store on first use."""
        if self._entity_store is None:
            entity_config = _safe_deepcopy_config(self.config.vector_store.config)
            entity_collection = _entity_collection_name(self.config.vector_store.provider, self.collection_name)
            if hasattr(entity_config, 'collection_name'):
                entity_config.collection_name = entity_collection
            elif isinstance(entity_config, dict):
                entity_config['collection_name'] = entity_collection
            # For Qdrant, share the existing client to avoid RocksDB lock contention
            # when using embedded mode (path=...). QdrantConfig.client takes precedence
            # over host/port/path.
            if self.config.vector_store.provider == "qdrant" and hasattr(self.vector_store, "client"):
                if hasattr(entity_config, "client"):
                    entity_config.client = self.vector_store.client
                elif isinstance(entity_config, dict):
                    entity_config["client"] = self.vector_store.client
            self._entity_store = VectorStoreFactory.create(
                self.config.vector_store.provider, entity_config
            )
        return self._entity_store

    @staticmethod
    def _normalize_entity_text(value: str) -> str:
        return " ".join(value.strip().lower().split())

    def _existing_entities_by_text(self, filters):
        """Return existing entity rows keyed by normalized payload data."""
        try:
            listed = self.entity_store.list(filters=filters, top_k=10000)
        except Exception as e:
            logger.debug(f"Exact entity lookup failed, falling back to semantic dedup: {e}")
            return {}

        rows_by_text = {}
        for row in _vector_store_list_rows(listed):
            payload = getattr(row, "payload", None) or {}
            text = payload.get("data")
            if not isinstance(text, str):
                continue
            normalized = self._normalize_entity_text(text)
            if normalized and normalized not in rows_by_text:
                rows_by_text[normalized] = row
        return rows_by_text

    async def _upsert_entity_async(self, entity_text, entity_type, memory_id, filters):
        """Async variant of `_upsert_entity` — per-entity search-then-update-or-insert."""
        try:
            entity_embedding = await asyncio.to_thread(self.embedding_model.embed, entity_text, "add")
            search_filters = {k: v for k, v in filters.items() if k in ("user_id", "agent_id", "run_id") and v}
            exact_match = (
                await asyncio.to_thread(self._existing_entities_by_text, search_filters)
            ).get(self._normalize_entity_text(entity_text))

            existing = []
            if exact_match is None:
                existing = await asyncio.to_thread(
                    self.entity_store.search,
                    query=entity_text,
                    vectors=entity_embedding,
                    top_k=1,
                    filters=search_filters,
                )

            semantic_match = existing[0] if existing and existing[0].score >= 0.95 else None
            match = exact_match or semantic_match
            if match:
                payload = match.payload or {}
                linked_ids = payload.get("linked_memory_ids", [])
                if memory_id not in linked_ids:
                    linked_ids.append(memory_id)
                    payload["linked_memory_ids"] = linked_ids
                    await asyncio.to_thread(
                        self.entity_store.update,
                        vector_id=match.id,
                        vector=None,
                        payload=payload,
                    )
            else:
                entity_id = str(uuid.uuid4())
                entity_payload = {
                    "data": entity_text,
                    "entity_type": entity_type,
                    "linked_memory_ids": [memory_id],
                    **{k: v for k, v in search_filters.items()},
                }
                await asyncio.to_thread(
                    self.entity_store.insert,
                    vectors=[entity_embedding],
                    ids=[entity_id],
                    payloads=[entity_payload],
                )
        except Exception as e:
            logger.warning(f"Entity upsert failed for '{entity_text}' (async): {e}")

    async def _bulk_clear_entity_store(self, filters):
        """Delete all entity records matching the given scope filters.

        Used by delete_all to avoid the race condition that occurs when
        concurrent _delete_memory coroutines each try to read-modify-write
        the same entity rows' linked_memory_ids lists.
        """
        if self._entity_store is None:
            return
        search_filters = {k: v for k, v in filters.items() if k in ("user_id", "agent_id", "run_id") and v}
        try:
            listed = await asyncio.to_thread(self.entity_store.list, filters=search_filters, top_k=10000)
            rows = listed[0] if isinstance(listed, (list, tuple)) and listed and isinstance(listed[0], list) else listed
            for row in rows or []:
                try:
                    await asyncio.to_thread(self.entity_store.delete, vector_id=row.id)
                except Exception as e:
                    logger.debug(f"Bulk entity delete failed for id={row.id}: {e}")
        except Exception as e:
            logger.warning(f"Bulk entity store cleanup failed: {e}")

    async def _remove_memory_from_entity_store(self, memory_id, filters):
        """Async variant of `Memory._remove_memory_from_entity_store`."""
        if self._entity_store is None:
            return
        search_filters = {k: v for k, v in filters.items() if k in ("user_id", "agent_id", "run_id") and v}
        try:
            listed = await asyncio.to_thread(self.entity_store.list, filters=search_filters, top_k=10000)
            rows = listed[0] if isinstance(listed, (list, tuple)) and listed and isinstance(listed[0], list) else listed
            for row in rows or []:
                try:
                    payload = getattr(row, "payload", None) or {}
                    linked = payload.get("linked_memory_ids", [])
                    if not isinstance(linked, list) or memory_id not in linked:
                        continue
                    remaining = [mid for mid in linked if mid != memory_id]
                    if not remaining:
                        try:
                            await asyncio.to_thread(self.entity_store.delete, vector_id=row.id)
                        except Exception as e:
                            logger.debug(f"Entity delete failed for id={row.id} (async): {e}")
                    else:
                        entity_text = payload.get("data")
                        if not isinstance(entity_text, str) or not entity_text:
                            logger.debug(f"Entity id={row.id} missing 'data'; skipping update during cleanup (async)")
                            continue
                        try:
                            vec = await asyncio.to_thread(self.embedding_model.embed, entity_text, "update")
                        except Exception as e:
                            logger.debug(f"Entity re-embed failed for '{entity_text}' (async): {e}")
                            continue
                        new_payload = {**payload, "linked_memory_ids": remaining}
                        try:
                            await asyncio.to_thread(
                                self.entity_store.update,
                                vector_id=row.id,
                                vector=vec,
                                payload=new_payload,
                            )
                        except Exception as e:
                            logger.debug(f"Entity update failed for id={row.id} (async): {e}")
                except Exception as e:
                    logger.debug(f"Entity cleanup error (async): {e}")
        except Exception as e:
            logger.warning(f"Entity store cleanup failed for memory_id={memory_id} (async): {e}")

    async def _link_entities_for_memory(self, memory_id, text, filters):
        """Async variant of `Memory._link_entities_for_memory`."""
        try:
            entities = await asyncio.to_thread(extract_entities, text)
            if not entities:
                return
            seen = set()
            for entity_type, entity_text in entities:
                key = self._normalize_entity_text(entity_text)
                if not key or key in seen:
                    continue
                seen.add(key)
                try:
                    await self._upsert_entity_async(entity_text, entity_type, memory_id, filters)
                except Exception as e:
                    logger.debug(f"Entity link failed for '{entity_text}' (async): {e}")
        except Exception as e:
            logger.warning(f"Entity linking failed for memory_id={memory_id} (async): {e}")

    @classmethod
    def from_config(cls, config_dict: Dict[str, Any]):
        try:
            config = MemoryConfig(**config_dict)
        except ValidationError as e:
            logger.error(f"Configuration validation error: {e}")
            raise
        return cls(config)

    def _should_use_agent_memory_extraction(self, messages, metadata):
        """Determine whether to use agent memory extraction based on the logic:
        - If agent_id is present and messages contain assistant role -> True
        - Otherwise -> False

        Args:
            messages: List of message dictionaries
            metadata: Metadata containing user_id, agent_id, etc.

        Returns:
            bool: True if should use agent memory extraction, False for user memory extraction
        """
        # Check if agent_id is present in metadata
        has_agent_id = metadata.get("agent_id") is not None

        # Check if there are assistant role messages
        has_assistant_messages = any(msg.get("role") == "assistant" for msg in messages)

        # Use agent memory extraction if agent_id is present and there are assistant messages
        return has_agent_id and has_assistant_messages

    async def add(
        self,
        messages,
        *,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Optional[Any] = None,
        expiration_date: Optional[Any] = None,
        infer: bool = True,
        memory_type: Optional[str] = None,
        prompt: Optional[str] = None,
        llm=None,
    ):
        """
        Create a new memory asynchronously.

        Args:
            messages (str or List[Dict[str, str]]): Messages to store in the memory.
            user_id (str, optional): ID of the user creating the memory.
            agent_id (str, optional): ID of the agent creating the memory. Defaults to None.
            run_id (str, optional): ID of the run creating the memory. Defaults to None.
            metadata (dict, optional): Metadata to store with the memory. Defaults to None.
            timestamp (Any, optional): Platform-only temporal parameter. Not supported in OSS.
            expiration_date (Any, optional): Date in YYYY-MM-DD format. Expired memories are hidden
                from search and get_all unless show_expired is True.
            infer (bool, optional): Whether to infer the memories. Defaults to True.
            memory_type (str, optional): Type of memory to create. Defaults to None.
                                         Pass "procedural_memory" to create procedural memories.
            prompt (str, optional): Prompt to use for the memory creation. Defaults to None.
            llm (BaseChatModel, optional): LLM class to use for generating procedural memories. Defaults to None. Useful when user is using LangChain ChatModel.

        Note:
            `search()` and `get_all()` scope queries via `filters={"user_id": "...", "agent_id": "...", "run_id": "..."}` —
            they reject top-level `user_id`/`agent_id`/`run_id` arguments. `add()` accepts them top-level, but passing
            the same arguments to `search()`/`get_all()` raises a `ValueError`; use the `filters` form there instead.

        Returns:
            dict: A dictionary containing the result of the memory addition operation.
        """
        if timestamp is not None:
            raise ValueError(await get_temporal_feature_error_message_async("async", "add", "timestamp"))

        normalized_expiration_date = _normalize_expiration_date(expiration_date)
        temporal_usage_notice = detect_temporal_usage_from_metadata(metadata)
        processed_metadata, effective_filters = _build_filters_and_metadata(
            user_id=user_id, agent_id=agent_id, run_id=run_id, input_metadata=metadata
        )
        if normalized_expiration_date is not None:
            processed_metadata["expiration_date"] = normalized_expiration_date

        if memory_type is not None and memory_type != MemoryType.PROCEDURAL.value:
            raise ValueError(
                f"Invalid 'memory_type'. Please pass {MemoryType.PROCEDURAL.value} to create procedural memories."
            )

        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]

        elif isinstance(messages, dict):
            messages = [messages]

        elif not isinstance(messages, list):
            raise Mem0ValidationError(
                message="messages must be str, dict, or list[dict]",
                error_code="VALIDATION_003",
                details={"provided_type": type(messages).__name__, "valid_types": ["str", "dict", "list[dict]"]},
                suggestion="Convert your input to a string, dictionary, or list of dictionaries."
            )

        if agent_id is not None and memory_type == MemoryType.PROCEDURAL.value:
            results = await self._create_procedural_memory(
                messages, metadata=processed_metadata, prompt=prompt, llm=llm
            )
            scale_threshold_notice = await asyncio.to_thread(detect_scale_threshold_from_add_result, self, results)
            if temporal_usage_notice:
                await display_temporal_usage_notice_async(self, "async", "add", *temporal_usage_notice)
            elif scale_threshold_notice:
                await display_scale_threshold_notice_async(self, "async", "add", *scale_threshold_notice)
            else:
                await display_first_run_notice_async(self, "async", "add")
            return results

        if self.config.llm.config.get("enable_vision"):
            messages = parse_vision_messages(messages, self.llm, self.config.llm.config.get("vision_details"))
        else:
            messages = parse_vision_messages(messages)

        vector_store_result = await self._add_to_vector_store(messages, processed_metadata, effective_filters, infer, prompt=prompt)
        scale_threshold_notice = await asyncio.to_thread(detect_scale_threshold_from_add_result, self, vector_store_result)
        if temporal_usage_notice:
            await display_temporal_usage_notice_async(self, "async", "add", *temporal_usage_notice)
        elif scale_threshold_notice:
            await display_scale_threshold_notice_async(self, "async", "add", *scale_threshold_notice)
        else:
            await display_first_run_notice_async(self, "async", "add")
        return {"results": vector_store_result}

    async def _add_to_vector_store(
        self,
        messages: list,
        metadata: dict,
        effective_filters: dict,
        infer: bool,
        prompt: Optional[str] = None,
    ):
        if not infer:
            returned_memories = []
            for message_dict in messages:
                if (
                    not isinstance(message_dict, dict)
                    or message_dict.get("role") is None
                    or message_dict.get("content") is None
                ):
                    logger.warning(f"Skipping invalid message format (async): {message_dict}")
                    continue

                if message_dict["role"] == "system":
                    continue

                per_msg_meta = deepcopy(metadata)
                per_msg_meta["role"] = message_dict["role"]

                actor_name = message_dict.get("name")
                if actor_name:
                    per_msg_meta["actor_id"] = actor_name

                msg_content = message_dict["content"]
                msg_embeddings = await asyncio.to_thread(self.embedding_model.embed, msg_content, "add")
                mem_id = await self._create_memory(msg_content, {msg_content: msg_embeddings}, per_msg_meta)

                returned_memories.append(
                    {
                        "id": mem_id,
                        "memory": msg_content,
                        "event": "ADD",
                        "actor_id": actor_name if actor_name else None,
                        "role": message_dict["role"],
                    }
                )
            return returned_memories

        # === V3 PHASED BATCH PIPELINE (async) ===

        # Phase 0: Context gathering
        session_scope = _build_session_scope(effective_filters)
        last_messages = await asyncio.to_thread(self.db.get_last_messages, session_scope, 10)
        parsed_messages = parse_messages(messages)

        # Phase 1: Existing memory retrieval
        # 只取有效事实，且 top_k 提升为候选池大小（详见 sync 同名阶段的说明）。
        search_filters = {k: v for k, v in effective_filters.items() if k in ("user_id", "agent_id", "run_id") and v}
        query_embedding = await asyncio.to_thread(self.embedding_model.embed, parsed_messages, "search")
        existing_results = await asyncio.to_thread(
            self.vector_store.search,
            query=parsed_messages,
            vectors=query_embedding,
            top_k=BITEMP_CANDIDATE_K,
            filters=_with_bitemporal_filter(search_filters),
        )

        # Map UUIDs to integers (anti-hallucination)
        existing_memories = []
        uuid_mapping = {}
        for idx, mem in enumerate(existing_results):
            uuid_mapping[str(idx)] = mem.id
            existing_memories.append({"id": str(idx), "text": mem.payload.get("data", "")})

        # Phase 2: LLM extraction (single call)
        is_agent_scoped = bool(effective_filters.get("agent_id")) and not effective_filters.get("user_id")
        system_prompt = ADDITIVE_EXTRACTION_PROMPT
        if is_agent_scoped:
            system_prompt += AGENT_CONTEXT_SUFFIX

        custom_instr = prompt or self.custom_instructions

        user_prompt = generate_additive_extraction_prompt(
            existing_memories=existing_memories,
            new_messages=parsed_messages,
            last_k_messages=last_messages,
            custom_instructions=custom_instr,
        )

        try:
            response = await asyncio.to_thread(
                self.llm.generate_response,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )
        except Exception as e:
            # Re-raise so callers can implement provider fallback / retry
            # (see sync counterpart for rationale).
            logger.error(f"LLM extraction failed (async): {e}")
            raise LLMError(f"LLM extraction failed: {e}") from e

        # Parse response
        try:
            response = remove_code_blocks(response)
            if not response or not response.strip():
                extracted_memories = []
            else:
                try:
                    extracted_memories = json.loads(response, strict=False).get("memory", [])
                except json.JSONDecodeError:
                    extracted_json = extract_json(response)
                    extracted_memories = json.loads(extracted_json, strict=False).get("memory", [])
        except Exception as e:
            logger.error(f"Error parsing extraction response (async): {e}")
            extracted_memories = []

        if not extracted_memories:
            await asyncio.to_thread(self.db.save_messages, messages, session_scope)
            return []

        # Phase 3: Batch embed all extracted memory texts
        mem_texts = [m.get("text", "") for m in extracted_memories if m.get("text")]
        try:
            mem_embeddings_list = await asyncio.to_thread(self.embedding_model.embed_batch, mem_texts, "add")
            embed_map = dict(zip(mem_texts, mem_embeddings_list))
        except Exception:
            embed_map = {}
            for text in mem_texts:
                try:
                    embed_map[text] = await asyncio.to_thread(self.embedding_model.embed, text, "add")
                except Exception as e:
                    logger.warning(f"Failed to embed memory text (async): {e}")

        # Phase 4: Per-memory CPU processing + Phase 5: Hash dedup
        existing_hashes = set()
        for mem in existing_results:
            h = mem.payload.get("hash") if hasattr(mem, "payload") and mem.payload else None
            if h:
                existing_hashes.add(h)

        records = []
        seen_hashes = set()
        for mem in extracted_memories:
            text = mem.get("text")
            if not text or text not in embed_map:
                continue

            mem_hash = hashlib.md5(text.encode()).hexdigest()
            if mem_hash in existing_hashes or mem_hash in seen_hashes:
                logger.debug(f"Skipping duplicate memory (hash match, async): {text[:50]}")
                continue
            seen_hashes.add(mem_hash)

            text_lemmatized = lemmatize_for_bm25(text)

            memory_id = str(uuid.uuid4())
            mem_metadata = deepcopy(metadata)
            mem_metadata["data"] = text
            mem_metadata["text_lemmatized"] = text_lemmatized
            mem_metadata["hash"] = mem_hash
            if "created_at" not in mem_metadata:
                mem_metadata["created_at"] = datetime.now(timezone.utc).isoformat()
            mem_metadata["updated_at"] = mem_metadata["created_at"]
            if mem.get("attributed_to"):
                mem_metadata["attributed_to"] = mem["attributed_to"]
            # valid_at 是事实在真实世界的生效日期，不是入库时间（方案 §4.1）。
            mem_metadata["valid_at"] = _coerce_bitemporal_date(mem.get("valid_at"))

            records.append((memory_id, text, embed_map[text], mem_metadata))

        if not records:
            await asyncio.to_thread(self.db.save_messages, messages, session_scope)
            return []

        # Phase 5: Independent contradiction detection (1b). Second LLM call with its own
        # system prompt; skipped when either side of the comparison is empty.
        contradictions = []
        if existing_results:
            contradictions = await self._detect_contradictions_async(records, existing_results)

        # Phase 6: Batch persist
        all_vectors = [r[2] for r in records]
        all_ids = [r[0] for r in records]
        all_payloads = [r[3] for r in records]

        try:
            await asyncio.to_thread(
                self.vector_store.insert,
                vectors=all_vectors,
                ids=all_ids,
                payloads=all_payloads,
            )
        except Exception:
            for mid, vec, pay in zip(all_ids, all_vectors, all_payloads):
                try:
                    await asyncio.to_thread(self.vector_store.insert, vectors=[vec], ids=[mid], payloads=[pay])
                except Exception as e:
                    logger.error(f"Failed to insert memory {mid} (async): {e}")

        # Batch history
        history_records = [
            {
                "memory_id": r[0],
                "old_memory": None,
                "new_memory": r[1],
                "event": "ADD",
                "created_at": r[3].get("created_at"),
                "is_deleted": 0,
            }
            for r in records
        ]
        try:
            await asyncio.to_thread(self.db.batch_add_history, history_records)
        except Exception:
            for hr in history_records:
                try:
                    await asyncio.to_thread(
                        self.db.add_history, hr["memory_id"], None, hr["new_memory"], "ADD",
                        created_at=hr.get("created_at")
                    )
                except Exception as e:
                    logger.error(f"Failed to add history for {hr['memory_id']} (async): {e}")

        # Phase 6b: Deterministic disposition (1c, 方案 §3.2 的 Phase 7) — pure code, the
        # pairs from 1b become payload-only invalidations (R9/R10).
        for update in _apply_contradictions(
            contradictions,
            {r[0]: r[3] for r in records},
            {mem.id: dict(mem.payload or {}) for mem in existing_results},
        ):
            invalidation = {
                "invalid_at": update["invalid_at"],
                "superseded_by": update["superseded_by"],
                "invalid_reason": update["invalid_reason"],
            }
            try:
                await asyncio.to_thread(
                    self.vector_store.update,
                    vector_id=update["memory_id"],
                    vector=None,
                    payload=invalidation,
                )
            except Exception as e:
                logger.error(f"Failed to invalidate memory {update['memory_id']} (async): {e}")

        # Phase 7: Batch entity linking
        try:
            all_texts = [r[1] for r in records]
            all_entities = await asyncio.to_thread(extract_entities_batch, all_texts)

            # 7a: Global dedup
            global_entities = {}
            for idx, (memory_id, text, embedding, payload) in enumerate(records):
                entities = all_entities[idx] if idx < len(all_entities) else []
                for entity_type, entity_text in entities:
                    key = self._normalize_entity_text(entity_text)
                    if key in global_entities:
                        global_entities[key][2].add(memory_id)
                    else:
                        global_entities[key] = [entity_type, entity_text, {memory_id}]

            if global_entities:
                ordered_keys = list(global_entities.keys())
                entity_texts = [global_entities[k][1] for k in ordered_keys]

                # 7b: Batch embed entities
                try:
                    entity_embeddings = await asyncio.to_thread(self.embedding_model.embed_batch, entity_texts, "add")
                except Exception:
                    entity_embeddings = []
                    for t in entity_texts:
                        try:
                            entity_embeddings.append(await asyncio.to_thread(self.embedding_model.embed, t, "add"))
                        except Exception:
                            entity_embeddings.append(None)

                if len(entity_embeddings) != len(ordered_keys):
                    logger.warning(
                        "embed_batch returned %d vectors for %d entity texts — "
                        "padding/truncating to avoid dropping entity links",
                        len(entity_embeddings),
                        len(ordered_keys),
                    )
                    entity_embeddings = list(entity_embeddings[: len(ordered_keys)])
                    entity_embeddings += [None] * (len(ordered_keys) - len(entity_embeddings))

                valid = [(i, k) for i, k in enumerate(ordered_keys) if entity_embeddings[i] is not None]
                if valid:
                    valid_indices, valid_keys = zip(*valid)
                    valid_vectors = [entity_embeddings[i] for i in valid_indices]
                    exact_matches = await asyncio.to_thread(self._existing_entities_by_text, search_filters)

                    # 7c: Batch search for existing entities
                    valid_texts = [global_entities[k][1] for k in valid_keys]
                    existing_matches = await asyncio.to_thread(
                        self.entity_store.search_batch,
                        queries=valid_texts,
                        vectors_list=valid_vectors,
                        top_k=1,
                        filters=search_filters,
                    )

                    # 7d: Separate into inserts vs updates
                    to_insert_vectors, to_insert_ids, to_insert_payloads = [], [], []
                    for j, key in enumerate(valid_keys):
                        entity_type, entity_text, memory_ids = global_entities[key]
                        matches = existing_matches[j] if j < len(existing_matches) else []
                        exact_match = exact_matches.get(key)

                        semantic_match = matches[0] if matches and matches[0].score >= 0.95 else None
                        match = exact_match or semantic_match
                        if match:
                            payload = match.payload or {}
                            linked = set(payload.get("linked_memory_ids", []))
                            linked |= memory_ids
                            payload["linked_memory_ids"] = sorted(linked)
                            try:
                                await asyncio.to_thread(
                                    self.entity_store.update,
                                    vector_id=match.id,
                                    vector=None,
                                    payload=payload,
                                )
                            except Exception as e:
                                logger.debug(f"Entity update failed for '{entity_text}' (async): {e}")
                        else:
                            to_insert_vectors.append(valid_vectors[j])
                            to_insert_ids.append(str(uuid.uuid4()))
                            to_insert_payloads.append({
                                "data": entity_text,
                                "entity_type": entity_type,
                                "linked_memory_ids": sorted(memory_ids),
                                **search_filters,
                            })

                    # 7e: Batch insert new entities
                    if to_insert_vectors:
                        try:
                            await asyncio.to_thread(
                                self.entity_store.insert,
                                vectors=to_insert_vectors,
                                ids=to_insert_ids,
                                payloads=to_insert_payloads,
                            )
                        except Exception as e:
                            logger.warning(f"Batch entity insert failed (async): {e}")
        except Exception as e:
            logger.warning(f"Batch entity linking failed (async): {e}")

        # Phase 8: Save messages + return
        await asyncio.to_thread(self.db.save_messages, messages, session_scope)

        returned_memories = [
            {"id": r[0], "memory": r[1], "event": "ADD"}
            for r in records
        ]

        keys, encoded_ids = process_telemetry_filters(effective_filters)
        capture_event(
            "mem0.add",
            self,
            {"version": self.api_version, "keys": keys, "encoded_ids": encoded_ids, "sync_type": "async"},
        )
        return returned_memories

    async def _detect_contradictions_async(self, records, existing_results) -> List[Dict[str, str]]:
        """1b 的异步版本：独立 LLM 调用，只取回矛盾对（详见 sync 同名方法）。"""
        new_facts = [
            {"id": record[0], "text": record[1], "valid_at": record[3].get("valid_at")}
            for record in records
        ]
        existing_facts = [
            {
                "id": mem.id,
                "text": (mem.payload or {}).get("data", ""),
                "valid_at": (mem.payload or {}).get("valid_at"),
                "created_at": (mem.payload or {}).get("created_at"),
            }
            for mem in existing_results
        ]
        user_prompt = generate_contradiction_detection_prompt(
            new_facts=new_facts, existing_facts=existing_facts
        )
        try:
            response = await asyncio.to_thread(
                self.llm.generate_response,
                messages=[
                    {"role": "system", "content": CONTRADICTION_DETECTION_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )
        except Exception as e:
            logger.error(f"LLM contradiction detection failed (async): {e}")
            return []
        return _parse_contradiction_pairs(response)

    async def get(self, memory_id):
        """
        Retrieve a memory by ID asynchronously.

        Args:
            memory_id (str): ID of the memory to retrieve.

        Returns:
            dict: Retrieved memory.
        """
        capture_event("mem0.get", self, {"memory_id": memory_id, "sync_type": "async"})
        memory = await asyncio.to_thread(self.vector_store.get, vector_id=memory_id)
        if not memory:
            await display_first_run_notice_async(self, "async", "get")
            return None

        promoted_payload_keys = BI_TEMPORAL_PAYLOAD_KEYS

        core_and_promoted_keys = {"data", "hash", "created_at", "updated_at", "id", "text_lemmatized", "attributed_to", *promoted_payload_keys}

        result_item = MemoryItem(
            id=memory.id,
            memory=memory.payload.get("data", ""),
            hash=memory.payload.get("hash"),
            created_at=memory.payload.get("created_at"),
            updated_at=memory.payload.get("updated_at"),
        ).model_dump()

        for key in promoted_payload_keys:
            if key in memory.payload:
                result_item[key] = memory.payload[key]

        # 一等字段：始终在顶层出现，未赋值时为 null（与 bi-temporal 四字段同规则，设计 F7）。
        for key in READ_ALWAYS_PRESENT_FIELDS:
            result_item.setdefault(key, None)

        additional_metadata = {k: v for k, v in memory.payload.items() if k not in core_and_promoted_keys}
        if additional_metadata:
            result_item["metadata"] = additional_metadata

        await display_first_run_notice_async(self, "async", "get")
        return result_item

    async def get_all(
        self,
        *,
        filters: Optional[Dict[str, Any]] = None,
        top_k: int = 20,
        show_expired: bool = False,
        as_of: Optional[str] = None,
        include_invalidated: bool = False,
        **kwargs,
    ):
        """
        List all memories.

        Args:
            filters (dict): Filter dict containing entity IDs and optional metadata filters.
                Must contain at least one of: user_id, agent_id, run_id.
                Example: filters={"user_id": "u1", "agent_id": "a1"}
            top_k (int, optional): The maximum number of memories to return. Defaults to 20.
            show_expired (bool, optional): Include expired memories. Defaults to False.
            as_of (str, optional): `YYYY-MM-DD` or ISO8601 instant T. Returns the facts that
                were valid at T (point-in-time). None means the current moment.
            include_invalidated (bool, optional): Return invalidated facts too, ignoring the
                validity filter. Defaults to False.

        Returns:
            dict: A dictionary containing a list of memories under the "results" key.
                  Example for v1.1+: `{"results": [{"id": "...", "memory": "...", ...}]}`

        Raises:
            ValueError: If filters doesn't contain at least one of user_id, agent_id, run_id,
                or if top_k is invalid.
        """
        # Reject top-level entity params - must use filters instead
        _reject_top_level_entity_params(kwargs, "get_all")

        # Validate top_k
        _validate_search_params(top_k=top_k)

        # Validate and trim entity IDs in filters
        effective_filters = dict(filters) if filters else {}
        if "user_id" in effective_filters:
            effective_filters["user_id"] = _validate_and_trim_entity_id(
                effective_filters["user_id"], "user_id"
            )
        if "agent_id" in effective_filters:
            effective_filters["agent_id"] = _validate_and_trim_entity_id(
                effective_filters["agent_id"], "agent_id"
            )
        if "run_id" in effective_filters:
            effective_filters["run_id"] = _validate_and_trim_entity_id(
                effective_filters["run_id"], "run_id"
            )

        # Validate filters contains at least one entity ID
        if not any(key in effective_filters for key in ("user_id", "agent_id", "run_id")):
            raise ValueError(
                "filters must contain at least one of: user_id, agent_id, run_id. "
                "Example: filters={'user_id': 'u1'}"
            )

        limit = top_k
        fetch_limit = limit if show_expired else max(limit * 4, 60)
        scale_threshold_notice = detect_scale_threshold_from_top_k(top_k)

        keys, encoded_ids = process_telemetry_filters(effective_filters)
        capture_event(
            "mem0.get_all", self, {"limit": limit, "keys": keys, "encoded_ids": encoded_ids, "sync_type": "async"}
        )

        all_memories_result = await self._get_all_from_vector_store(effective_filters, fetch_limit, show_expired, limit, as_of, include_invalidated)

        if scale_threshold_notice:
            await display_scale_threshold_notice_async(self, "async", "get_all", *scale_threshold_notice)
        else:
            await display_first_run_notice_async(self, "async", "get_all")
        return {"results": all_memories_result}

    async def _get_all_from_vector_store(
        self, filters, limit, show_expired=False, output_limit=None, as_of=None, include_invalidated=False
    ):
        memories_result = await asyncio.to_thread(
            self.vector_store.list,
            filters=_with_bitemporal_filter(filters, as_of, include_invalidated),
            top_k=limit,
        )

        # Handle different vector store return formats by inspecting first element
        if isinstance(memories_result, (tuple, list)) and len(memories_result) > 0:
            first_element = memories_result[0]

            # If first element is a container, unwrap one level
            if isinstance(first_element, (list, tuple)):
                actual_memories = first_element
            else:
                # First element is a memory object, structure is already flat
                actual_memories = memories_result
        else:
            actual_memories = memories_result

        promoted_payload_keys = BI_TEMPORAL_PAYLOAD_KEYS
        core_and_promoted_keys = {"data", "hash", "created_at", "updated_at", "id", "text_lemmatized", "attributed_to", *promoted_payload_keys}

        formatted_memories = []
        for mem in actual_memories:
            if not show_expired and _payload_is_expired(mem.payload):
                continue
            memory_item_dict = MemoryItem(
                id=mem.id,
                memory=mem.payload.get("data", ""),
                hash=mem.payload.get("hash"),
                created_at=mem.payload.get("created_at"),
                updated_at=mem.payload.get("updated_at"),
            ).model_dump(exclude={"score"})

            for key in promoted_payload_keys:
                if key in mem.payload:
                    memory_item_dict[key] = mem.payload[key]

            for key in READ_ALWAYS_PRESENT_FIELDS:
                memory_item_dict.setdefault(key, None)

            additional_metadata = {k: v for k, v in mem.payload.items() if k not in core_and_promoted_keys}
            if additional_metadata:
                memory_item_dict["metadata"] = additional_metadata

            formatted_memories.append(memory_item_dict)
            if output_limit is not None and len(formatted_memories) >= output_limit:
                break

        return formatted_memories

    async def search(
        self,
        query: str,
        *,
        top_k: int = 20,
        filters: Optional[Dict[str, Any]] = None,
        threshold: float = 0.1,
        rerank: bool = False,
        explain: bool = False,
        reference_date: Optional[Any] = None,
        show_expired: bool = False,
        as_of: Optional[str] = None,
        include_invalidated: bool = False,
        **kwargs,
    ):
        """
        Searches for memories based on a query.

        Args:
            query (str): Query to search for.
            top_k (int, optional): Maximum number of results to return. Defaults to 20.
            filters (dict): Filter dict containing entity IDs and optional metadata filters.
                Must contain at least one of: user_id, agent_id, run_id.
                Example: filters={"user_id": "u1", "agent_id": "a1"}

                Enhanced metadata filtering with operators:
                - {"key": "value"} - exact match
                - {"key": {"eq": "value"}} - equals
                - {"key": {"ne": "value"}} - not equals
                - {"key": {"in": ["val1", "val2"]}} - in list
                - {"key": {"nin": ["val1", "val2"]}} - not in list
                - {"key": {"gt": 10}} - greater than
                - {"key": {"gte": 10}} - greater than or equal
                - {"key": {"lt": 10}} - less than
                - {"key": {"lte": 10}} - less than or equal
                - {"key": {"contains": "text"}} - contains text
                - {"key": {"icontains": "text"}} - case-insensitive contains
                - {"key": "*"} - wildcard match (any value)
                - {"AND": [filter1, filter2]} - logical AND
                - {"OR": [filter1, filter2]} - logical OR
                - {"NOT": [filter1]} - logical NOT
            threshold (float, optional): Minimum score for a memory to be included. Defaults to 0.1.
            rerank (bool, optional): Whether to rerank results. Defaults to False.
            explain (bool, optional): Whether to include score_details for each result. Defaults to False.
            reference_date (Any, optional): Platform-only temporal parameter. Not supported in OSS.
            show_expired (bool, optional): Include expired memories. Defaults to False.
            as_of (str, optional): `YYYY-MM-DD` or ISO8601 instant T. Returns the facts that
                were valid at T (point-in-time). None means the current moment.
            include_invalidated (bool, optional): Return invalidated facts too, ignoring the
                validity filter. Defaults to False.

        Returns:
            dict: A dictionary containing the search results under a "results" key.
                  Example for v1.1+: `{"results": [{"id": "...", "memory": "...", "score": 0.8, ...}]}`

        Raises:
            ValueError: If filters doesn't contain at least one of user_id, agent_id, run_id,
                or if threshold/top_k values are invalid.
        """
        if reference_date is not None:
            raise ValueError(
                await get_temporal_feature_error_message_async("async", "search", "reference_date")
            )

        # Reject top-level entity params - must use filters instead
        _reject_top_level_entity_params(kwargs, "search")

        # Validate search parameters (before applying defaults)
        _validate_search_params(threshold=threshold, top_k=top_k)
        query = _validate_and_trim_search_query(query)
        temporal_usage_notice = detect_temporal_usage_from_search(query, filters)

        # Validate and trim entity IDs in filters
        effective_filters = filters.copy() if filters else {}
        if "user_id" in effective_filters:
            effective_filters["user_id"] = _validate_and_trim_entity_id(
                effective_filters["user_id"], "user_id"
            )
        if "agent_id" in effective_filters:
            effective_filters["agent_id"] = _validate_and_trim_entity_id(
                effective_filters["agent_id"], "agent_id"
            )
        if "run_id" in effective_filters:
            effective_filters["run_id"] = _validate_and_trim_entity_id(
                effective_filters["run_id"], "run_id"
            )

        # Validate filters contains at least one entity ID
        if not any(key in effective_filters for key in ("user_id", "agent_id", "run_id")):
            raise ValueError(
                "filters must contain at least one of: user_id, agent_id, run_id. "
                "Example: filters={'user_id': 'u1'}"
            )

        limit = top_k
        scale_threshold_notice = detect_scale_threshold_from_top_k(top_k)

        # Apply enhanced metadata filtering if advanced operators are detected
        if self._has_advanced_operators(effective_filters):
            processed_filters = self._process_metadata_filters(effective_filters)
            # Remove logical/operator keys that have been reprocessed
            for logical_key in ("AND", "OR", "NOT"):
                effective_filters.pop(logical_key, None)
            for fk in list(effective_filters.keys()):
                if fk not in ("AND", "OR", "NOT", "user_id", "agent_id", "run_id") and isinstance(effective_filters.get(fk), dict):
                    effective_filters.pop(fk, None)
            effective_filters.update(processed_filters)

        keys, encoded_ids = process_telemetry_filters(effective_filters)
        capture_event(
            "mem0.search",
            self,
            {
                "limit": limit,
                "version": self.api_version,
                "keys": keys,
                "encoded_ids": encoded_ids,
                "sync_type": "async",
                "threshold": threshold,
                "explain": explain,
                "advanced_filters": bool(filters and self._has_advanced_operators(filters)),
            },
        )

        search_start = time.perf_counter()
        original_memories = await self._search_vector_store(
            query,
            _with_bitemporal_filter(effective_filters, as_of, include_invalidated),
            limit,
            threshold,
            explain=explain,
            show_expired=show_expired,
        )
        search_elapsed_seconds = time.perf_counter() - search_start

        # Apply reranking if enabled and reranker is available
        if rerank and self.reranker and original_memories:
            try:
                # Run reranking in thread pool to avoid blocking async loop
                reranked_memories = await asyncio.to_thread(
                    self.reranker.rerank, query, original_memories, limit
                )
                original_memories = reranked_memories
            except Exception as e:
                logger.warning(f"Reranking failed, using original results: {e}")

        # 强化只针对最终入选（本次 rerank 之后）的条目；关闭态下为空操作，且异步派发
        # 不进入响应时延路径（设计 §4.2-4.3）。
        _reinforce_search_hits(self, original_memories)

        if temporal_usage_notice:
            await display_temporal_usage_notice_async(self, "async", "search", *temporal_usage_notice)
        elif scale_threshold_notice:
            await display_scale_threshold_notice_async(self, "async", "search", *scale_threshold_notice)
        elif search_elapsed_seconds > PERFORMANCE_SLOW_QUERY_THRESHOLD_SECONDS:
            await display_performance_slow_query_notice_async(
                self,
                "async",
                "search",
                search_elapsed_seconds,
                top_k,
                len(original_memories),
            )
        else:
            await display_first_run_notice_async(self, "async", "search")
        return {"results": original_memories}

    def _process_metadata_filters(self, metadata_filters: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process enhanced metadata filters and convert them to vector store compatible format.

        Args:
            metadata_filters: Enhanced metadata filters with operators

        Returns:
            Dict of processed filters compatible with vector store
        """
        processed_filters = {}

        def process_condition(key: str, condition: Any) -> Dict[str, Any]:
            if not isinstance(condition, dict):
                # Simple equality: {"key": "value"}
                if condition == "*":
                    # Wildcard: match everything for this field (implementation depends on vector store)
                    return {key: "*"}
                return {key: condition}

            result = {}
            for operator, value in condition.items():
                # Map platform operators to universal format that can be translated by each vector store
                operator_map = {
                    "eq": "eq", "ne": "ne", "gt": "gt", "gte": "gte",
                    "lt": "lt", "lte": "lte", "in": "in", "nin": "nin",
                    "contains": "contains", "icontains": "icontains"
                }

                if operator in operator_map:
                    result.setdefault(key, {})[operator_map[operator]] = value
                else:
                    raise ValueError(f"Unsupported metadata filter operator: {operator}")
            return result

        def merge_filters(target: Dict[str, Any], source: Dict[str, Any]) -> None:
            """Merge source into target, deep-merging nested operator dicts for the same key."""
            for key, value in source.items():
                if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                    target[key].update(value)
                else:
                    target[key] = value

        for key, value in metadata_filters.items():
            if key == "AND":
                # Logical AND: combine multiple conditions
                if not isinstance(value, list):
                    raise ValueError("AND operator requires a list of conditions")
                for condition in value:
                    for sub_key, sub_value in condition.items():
                        merge_filters(processed_filters, process_condition(sub_key, sub_value))
            elif key == "OR":
                # Logical OR: Pass through to vector store for implementation-specific handling
                if not isinstance(value, list) or not value:
                    raise ValueError("OR operator requires a non-empty list of conditions")
                # Store OR conditions in a way that vector stores can interpret
                processed_filters["$or"] = []
                for condition in value:
                    or_condition = {}
                    for sub_key, sub_value in condition.items():
                        merge_filters(or_condition, process_condition(sub_key, sub_value))
                    processed_filters["$or"].append(or_condition)
            elif key == "NOT":
                # Logical NOT: Pass through to vector store for implementation-specific handling
                if not isinstance(value, list) or not value:
                    raise ValueError("NOT operator requires a non-empty list of conditions")
                processed_filters["$not"] = []
                for condition in value:
                    not_condition = {}
                    for sub_key, sub_value in condition.items():
                        merge_filters(not_condition, process_condition(sub_key, sub_value))
                    processed_filters["$not"].append(not_condition)
            else:
                merge_filters(processed_filters, process_condition(key, value))

        return processed_filters

    def _has_advanced_operators(self, filters: Dict[str, Any]) -> bool:
        """
        Check if filters contain advanced operators that need special processing.

        Args:
            filters: Dictionary of filters to check

        Returns:
            bool: True if advanced operators are detected
        """
        if not isinstance(filters, dict):
            return False

        for key, value in filters.items():
            # Check for platform-style logical operators
            if key in ["AND", "OR", "NOT"]:
                return True
            # Check for comparison operators (without $ prefix for universal compatibility)
            if isinstance(value, dict):
                for op in value.keys():
                    if op in ["eq", "ne", "gt", "gte", "lt", "lte", "in", "nin", "contains", "icontains"]:
                        return True
            # Check for wildcard values
            if value == "*":
                return True
        return False

    async def _search_vector_store(self, query, filters, limit, threshold=0.1, explain=False, show_expired=False):
        if threshold is None:
            threshold = 0.1

        # Step 1: Preprocess query (CPU-bound)
        query_lemmatized = await asyncio.to_thread(lemmatize_for_bm25, query)
        query_entities = await asyncio.to_thread(extract_entities, query)

        # Step 2: Embed query
        embeddings = await asyncio.to_thread(self.embedding_model.embed, query, "search")

        # Step 3: Semantic search (over-fetch)
        internal_limit = max(limit * 4, 60)
        semantic_results = await asyncio.to_thread(
            self.vector_store.search, query=query, vectors=embeddings, top_k=internal_limit, filters=filters
        )

        # Step 4: Keyword search (if store supports it)
        keyword_results = await asyncio.to_thread(
            self.vector_store.keyword_search, query=query_lemmatized, top_k=internal_limit, filters=filters
        )

        # Step 5: Compute BM25 scores
        bm25_scores = {}
        if keyword_results is not None:
            midpoint, steepness = get_bm25_params(query, lemmatized=query_lemmatized)
            for mem in keyword_results:
                mem_id = str(mem.id) if hasattr(mem, 'id') else str(mem.get('id', ''))
                raw_score = mem.score if hasattr(mem, 'score') else mem.get('score', 0)
                if raw_score and raw_score > 0:
                    bm25_scores[mem_id] = normalize_bm25(raw_score, midpoint, steepness)

        # Step 6: Compute entity boosts
        entity_boosts = {}
        if query_entities:
            entity_boosts = await self._compute_entity_boosts_async(query_entities, filters)

        # Step 7: Build candidate set from semantic results
        candidates = []
        for mem in semantic_results:
            payload = mem.payload if hasattr(mem, 'payload') else {}
            if not show_expired and _payload_is_expired(payload):
                continue
            mem_id = str(mem.id)
            candidates.append({
                "id": mem_id,
                "score": mem.score,
                "payload": payload,
            })

        # Step 8: Score and rank
        # 时间因子只在本检索路径生效；整批候选共用同一个 now 基准（设计 §3.5）。
        scored_results = score_and_rank(
            semantic_results=candidates,
            bm25_scores=bm25_scores,
            entity_boosts=entity_boosts,
            threshold=threshold,
            top_k=limit,
            explain=explain,
            decay_factors=self._decay_factors(candidates),
        )

        # Step 9: Format results
        promoted_payload_keys = BI_TEMPORAL_PAYLOAD_KEYS
        core_and_promoted_keys = {"data", "hash", "created_at", "updated_at", "id", "text_lemmatized", "attributed_to", *promoted_payload_keys}

        original_memories = []
        for scored in scored_results:
            payload = scored.get("payload") or {}
            if not payload.get("data"):
                continue

            memory_item_dict = MemoryItem(
                id=scored["id"],
                memory=payload.get("data", ""),
                hash=payload.get("hash"),
                created_at=payload.get("created_at"),
                updated_at=payload.get("updated_at"),
                score=scored["score"],
            ).model_dump()

            for key in promoted_payload_keys:
                if key in payload:
                    memory_item_dict[key] = payload[key]

            for key in READ_ALWAYS_PRESENT_FIELDS:
                memory_item_dict.setdefault(key, None)

            additional_metadata = {k: v for k, v in payload.items() if k not in core_and_promoted_keys}
            if additional_metadata:
                if not memory_item_dict.get("metadata"):
                    memory_item_dict["metadata"] = {}
                memory_item_dict["metadata"].update(additional_metadata)
            if explain and "score_details" in scored:
                memory_item_dict["score_details"] = scored["score_details"]

            original_memories.append(memory_item_dict)

        return original_memories

    async def _compute_entity_boosts_async(self, query_entities, filters):
        """Async version of entity boost computation."""
        seen = set()
        deduped = []
        for entity_type, entity_text in query_entities[:8]:
            key = self._normalize_entity_text(entity_text)
            if key and key not in seen:
                seen.add(key)
                deduped.append((entity_type, entity_text))

        if not deduped:
            return {}

        search_filters = _scope_filters(filters)
        memory_boosts = {}

        try:
            entity_texts = [text for _, text in deduped]
            embeddings = await asyncio.to_thread(self.embedding_model.embed_batch, entity_texts, "search")

            if len(embeddings) != len(entity_texts):
                logger.warning(
                    "embed_batch returned %d vectors for %d texts — skipping entity boost",
                    len(embeddings),
                    len(entity_texts),
                )
                return memory_boosts

            sem = asyncio.Semaphore(4)

            async def _search_entity(entity_text, embedding):
                async with sem:
                    return await asyncio.to_thread(
                        self.entity_store.search,
                        query=entity_text,
                        vectors=embedding,
                        top_k=500,
                        filters=search_filters,
                    )

            results = await asyncio.gather(
                *(_search_entity(text, emb) for text, emb in zip(entity_texts, embeddings)),
                return_exceptions=True,
            )

            for matches in results:
                if isinstance(matches, BaseException):
                    logger.warning("Entity boost search failed for one entity: %s", matches)
                    continue

                for match in matches:
                    similarity = match.score if hasattr(match, 'score') else 0.0
                    if similarity < 0.5:
                        continue

                    payload = match.payload if hasattr(match, 'payload') else {}
                    linked_memory_ids = payload.get("linked_memory_ids", [])
                    if not isinstance(linked_memory_ids, list):
                        continue

                    num_linked = max(len(linked_memory_ids), 1)
                    memory_count_weight = 1.0 / (1.0 + 0.001 * ((num_linked - 1) ** 2))
                    boost = similarity * ENTITY_BOOST_WEIGHT * memory_count_weight

                    for memory_id in linked_memory_ids:
                        if memory_id:
                            memory_key = str(memory_id)
                            memory_boosts[memory_key] = max(memory_boosts.get(memory_key, 0.0), boost)

        except Exception as e:
            logger.warning(f"Entity boost computation failed: {e}")

        return memory_boosts

    async def update(
        self,
        memory_id,
        text: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        expiration_date: Any = _UNSET,
        data: Optional[str] = None,
    ):
        """
        Update a memory by ID asynchronously.

        Args:
            memory_id (str): ID of the memory to update.
            text (str, optional): New content to update the memory with.
            metadata (dict, optional): Metadata to update with the memory. Defaults to None.
                ``user_id``/``agent_id``/``run_id``/``actor_id`` are ignored here - they are
                immutable after creation.
            expiration_date (Any, optional): Date in YYYY-MM-DD format, or None to clear it.
            data (str, optional): Deprecated alias for ``text``. Will be removed in the next
                major release; use ``text`` instead.

        Returns:
            dict: Success message indicating the memory was updated.

        Example:
            >>> await m.update(memory_id="mem_123", text="Likes to play tennis on weekends")
            {'message': 'Memory updated successfully!'}
        """
        capture_event("mem0.update", self, {"memory_id": memory_id, "sync_type": "async"})

        if data is not None:
            logger.warning(
                "The `data` argument to update() is deprecated and will be removed in the "
                "next major release. Use `text` instead."
            )
            if text is None:
                text = data

        if text is None and metadata is None and expiration_date is _UNSET:
            raise ValueError("At least one of text, metadata, or expiration_date must be provided.")

        update_metadata = deepcopy(metadata) if metadata is not None else None
        if expiration_date is not _UNSET:
            update_metadata = update_metadata or {}
            update_metadata["expiration_date"] = _normalize_expiration_date(expiration_date)

        existing_embeddings = {}
        if text is not None:
            embeddings = await asyncio.to_thread(self.embedding_model.embed, text, "update")
            existing_embeddings[text] = embeddings

        await self._update_memory(memory_id, text, existing_embeddings, update_metadata)
        await display_first_run_notice_async(self, "async", "update")
        return {"message": "Memory updated successfully!"}

    async def delete(self, memory_id):
        """
        Delete a memory by ID asynchronously.

        Args:
            memory_id (str): ID of the memory to delete.
        """
        capture_event("mem0.delete", self, {"memory_id": memory_id, "sync_type": "async"})

        existing_memory = await asyncio.to_thread(self.vector_store.get, vector_id=memory_id)
        if existing_memory is None:
            raise ValueError(f"Memory with id {memory_id} not found")

        await self._delete_memory(memory_id, existing_memory)
        decay_usage_notice = detect_decay_usage_from_delete()
        if decay_usage_notice:
            await display_decay_usage_notice_async(self, "async", "delete", *decay_usage_notice)
        else:
            await display_first_run_notice_async(self, "async", "delete")
        return {"message": "Memory deleted successfully!"}

    async def delete_all(self, user_id=None, agent_id=None, run_id=None):
        """
        Delete all memories asynchronously.

        Args:
            user_id (str, optional): ID of the user to delete memories for. Defaults to None.
            agent_id (str, optional): ID of the agent to delete memories for. Defaults to None.
            run_id (str, optional): ID of the run to delete memories for. Defaults to None.
        """
        user_id = _validate_and_trim_entity_id(user_id, "user_id")
        agent_id = _validate_and_trim_entity_id(agent_id, "agent_id")
        run_id = _validate_and_trim_entity_id(run_id, "run_id")

        filters = {}
        if user_id:
            filters["user_id"] = user_id
        if agent_id:
            filters["agent_id"] = agent_id
        if run_id:
            filters["run_id"] = run_id

        if not filters:
            raise ValueError(
                "At least one filter is required to delete all memories. If you want to delete all memories, use the `reset()` method."
            )

        keys, encoded_ids = process_telemetry_filters(filters)
        capture_event("mem0.delete_all", self, {"keys": keys, "encoded_ids": encoded_ids, "sync_type": "async"})
        deleted_count = 0
        errors = []
        seen_batches = set()
        while True:
            memories = await asyncio.to_thread(
                self.vector_store.list,
                filters=filters,
                top_k=DELETE_ALL_BATCH_SIZE,
            )
            batch = memories[0] if memories else []
            if not batch:
                break
            batch_ids = tuple(sorted(str(memory.id) for memory in batch))
            if batch_ids in seen_batches:
                logger.warning("Stopping delete_all after a repeated memory batch")
                break
            seen_batches.add(batch_ids)
            delete_tasks = [
                self._delete_memory(memory.id, skip_entity_cleanup=True)
                for memory in batch
            ]
            results = await asyncio.gather(*delete_tasks, return_exceptions=True)
            batch_errors = [
                result for result in results if isinstance(result, BaseException)
            ]
            errors.extend(batch_errors)
            deleted_count += len(results) - len(batch_errors)

        if self._entity_store is not None:
            await self._bulk_clear_entity_store(filters)

        if errors:
            logger.warning("Failed to delete %d memories", len(errors))
            for err in errors:
                logger.warning("Delete error: %s", err)

        logger.info(f"Deleted {deleted_count} memories")

        decay_usage_notice = detect_decay_usage_from_delete_all(deleted_count)
        if decay_usage_notice:
            await display_decay_usage_notice_async(self, "async", "delete_all", *decay_usage_notice)
        else:
            await display_first_run_notice_async(self, "async", "delete_all")
        return {"message": "Memories deleted successfully!"}

    async def history(self, memory_id):
        """
        Get the history of changes for a memory by ID asynchronously.

        Args:
            memory_id (str): ID of the memory to get history for.

        Returns:
            list: List of changes for the memory.
        """
        capture_event("mem0.history", self, {"memory_id": memory_id, "sync_type": "async"})
        history = await asyncio.to_thread(self.db.get_history, memory_id)
        await display_first_run_notice_async(self, "async", "history")
        return history

    async def _create_memory(self, data, existing_embeddings, metadata=None):
        logger.debug(f"Creating memory with {data=}")
        if data in existing_embeddings:
            embeddings = existing_embeddings[data]
        else:
            embeddings = await asyncio.to_thread(self.embedding_model.embed, data, memory_action="add")

        memory_id = str(uuid.uuid4())
        new_metadata = deepcopy(metadata) if metadata is not None else {}
        new_metadata["data"] = data
        new_metadata["hash"] = hashlib.md5(data.encode()).hexdigest()
        if "created_at" not in new_metadata:
            new_metadata["created_at"] = datetime.now(timezone.utc).isoformat()
        new_metadata["updated_at"] = new_metadata["created_at"]
        new_metadata["text_lemmatized"] = lemmatize_for_bm25(data)

        await asyncio.to_thread(
            self.vector_store.insert,
            vectors=[embeddings],
            ids=[memory_id],
            payloads=[new_metadata],
        )

        await asyncio.to_thread(
            self.db.add_history,
            memory_id,
            None,
            data,
            "ADD",
            created_at=new_metadata.get("created_at"),
            updated_at=new_metadata.get("updated_at"),
            actor_id=new_metadata.get("actor_id"),
            role=new_metadata.get("role"),
        )

        return memory_id

    async def _create_procedural_memory(self, messages, metadata=None, llm=None, prompt=None):
        """
        Create a procedural memory asynchronously

        Args:
            messages (list): List of messages to create a procedural memory from.
            metadata (dict): Metadata to create a procedural memory from.
            llm (llm, optional): LLM to use for the procedural memory creation. Defaults to None.
            prompt (str, optional): Prompt to use for the procedural memory creation. Defaults to None.
        """
        logger.info("Creating procedural memory")

        parsed_messages = [
            {"role": "system", "content": prompt or PROCEDURAL_MEMORY_SYSTEM_PROMPT},
            *messages,
            {"role": "user", "content": "Create procedural memory of the above conversation."},
        ]

        try:
            if llm is not None:
                # langchain-core is only needed to adapt messages for a custom
                # LangChain LLM. The default path uses self.llm and must not
                # require the optional dependency, mirroring the sync version.
                try:
                    from langchain_core.messages.utils import (
                        convert_to_messages,  # type: ignore
                    )
                except ImportError as e:
                    raise ImportError(
                        "langchain-core is required to pass a custom LLM to procedural memory. "
                        "Install it with 'pip install langchain-core'."
                    ) from e

                parsed_messages = convert_to_messages(parsed_messages)
                response = await asyncio.to_thread(llm.invoke, input=parsed_messages)
                procedural_memory = remove_code_blocks(response.content)
            else:
                procedural_memory = await asyncio.to_thread(self.llm.generate_response, messages=parsed_messages)
                procedural_memory = remove_code_blocks(procedural_memory)

        except Exception as e:
            logger.error(f"Error generating procedural memory summary: {e}")
            raise

        if not procedural_memory:
            raise ValueError(
                "The LLM returned no content for the procedural memory summary. "
                "The model may have declined the request or returned an empty response."
            )

        if metadata is None:
            raise ValueError("Metadata cannot be done for procedural memory.")

        metadata = {**metadata, "memory_type": MemoryType.PROCEDURAL.value}
        embeddings = await asyncio.to_thread(self.embedding_model.embed, procedural_memory, memory_action="add")
        memory_id = await self._create_memory(procedural_memory, {procedural_memory: embeddings}, metadata=metadata)
        capture_event("mem0._create_procedural_memory", self, {"memory_id": memory_id, "sync_type": "async"})

        result = {"results": [{"id": memory_id, "memory": procedural_memory, "event": "ADD"}]}

        return result

    async def _update_memory(self, memory_id, data, existing_embeddings, metadata=None):
        logger.info(f"Updating memory with {data=}")

        try:
            existing_memory = await asyncio.to_thread(self.vector_store.get, vector_id=memory_id)
        except Exception:
            # Backing-store failure, not a bad memory_id: re-raise the original so the REST layer maps it to 5xx, not 4xx.
            logger.error(f"Error getting memory with ID {memory_id} during update.")
            raise

        if existing_memory is None:
            raise ValueError(f"Memory with id {memory_id} not found. Please provide a valid 'memory_id'")

        prev_value = existing_memory.payload.get("data")
        if data is None:
            data = prev_value
        if not isinstance(data, str):
            raise ValueError(f"Memory with id {memory_id} does not have text content to update")
        text_changed = data != prev_value

        new_metadata = deepcopy(existing_memory.payload)
        if metadata is not None:
            new_metadata.update(_strip_identity_keys(metadata, existing_memory.payload))

        new_metadata["data"] = data
        new_metadata["hash"] = hashlib.md5(data.encode()).hexdigest()
        new_metadata["text_lemmatized"] = lemmatize_for_bm25(data)
        new_metadata["created_at"] = existing_memory.payload.get("created_at")
        new_metadata["updated_at"] = datetime.now(timezone.utc).isoformat()

        if data in existing_embeddings:
            embeddings = existing_embeddings[data]
        else:
            embeddings = await asyncio.to_thread(self.embedding_model.embed, data, "update")

        await asyncio.to_thread(
            self.vector_store.update,
            vector_id=memory_id,
            vector=embeddings,
            payload=new_metadata,
        )
        logger.info(f"Updating memory with ID {memory_id=} with {data=}")

        await asyncio.to_thread(
            self.db.add_history,
            memory_id,
            prev_value,
            data,
            "UPDATE",
            created_at=new_metadata["created_at"],
            updated_at=new_metadata["updated_at"],
            actor_id=new_metadata.get("actor_id"),
            role=new_metadata.get("role"),
        )

        # Entity-store cleanup: strip this memory's id from old-text entities,
        # then re-extract entities from the new text and link them back.
        session_filters = {k: new_metadata[k] for k in ("user_id", "agent_id", "run_id") if new_metadata.get(k)}
        if text_changed:
            await self._remove_memory_from_entity_store(memory_id, session_filters)
            await self._link_entities_for_memory(memory_id, data, session_filters)

        return memory_id

    async def _delete_memory(self, memory_id, existing_memory=None, skip_entity_cleanup=False):
        logger.info(f"Deleting memory with {memory_id=}")
        if existing_memory is None:
            existing_memory = await asyncio.to_thread(self.vector_store.get, vector_id=memory_id)
            if existing_memory is None:
                raise ValueError(f"Memory with id {memory_id} not found. Please provide a valid 'memory_id'")
        prev_value = existing_memory.payload.get("data", "")
        created_at = _normalize_iso_timestamp_to_utc(existing_memory.payload.get("created_at"))
        updated_at = datetime.now(timezone.utc).isoformat()
        payload = existing_memory.payload or {}
        session_filters = {k: payload[k] for k in ("user_id", "agent_id", "run_id") if payload.get(k)}

        await asyncio.to_thread(self.vector_store.delete, vector_id=memory_id)
        await asyncio.to_thread(
            self.db.add_history,
            memory_id,
            prev_value,
            None,
            "DELETE",
            created_at=created_at,
            updated_at=updated_at,
            actor_id=existing_memory.payload.get("actor_id"),
            role=existing_memory.payload.get("role"),
            is_deleted=1,
        )

        if not skip_entity_cleanup:
            await self._remove_memory_from_entity_store(memory_id, session_filters)

        return memory_id

    async def reset(self):
        """
        Reset the memory store asynchronously by:
            Deletes the vector store collection
            Resets the database
            Recreates the vector store with a new client
        """
        logger.warning("Resetting all memories")
        await asyncio.to_thread(self.vector_store.delete_col)

        gc.collect()

        if hasattr(self.vector_store, "client") and hasattr(self.vector_store.client, "close"):
            await asyncio.to_thread(self.vector_store.client.close)

        await asyncio.to_thread(self.db.reset)
        await asyncio.to_thread(self.db.close)
        self.db = SQLiteManager(self.config.history_db_path)

        self.vector_store = VectorStoreFactory.create(
            self.config.vector_store.provider, self.config.vector_store.config
        )

        if self._entity_store is not None:
            try:
                await asyncio.to_thread(self._entity_store.reset)
            except Exception as e:
                logger.warning(f"Failed to reset entity store: {e}")
            self._entity_store = None

        capture_event("mem0.reset", self, {"sync_type": "async"})
        await display_first_run_notice_async(self, "async", "reset")

    def close(self):
        """Release resources held by this AsyncMemory instance."""
        if hasattr(self, "db") and self.db is not None:
            self.db.close()
            self.db = None

    async def chat(self, query):
        raise NotImplementedError("Chat function not implemented yet.")
