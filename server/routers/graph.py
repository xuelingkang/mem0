"""图能力的只读观测端点（设计 `docs/design/graph-memory.md` §8）。

一个端点：`GET /graph/stats` 返回图派发计数器与熔断状态的当前值，使「队列有界且可观测」
这类判据可以由实际读数判定，而不必拿图桥的访问日志当代理（日志里看不到冷却窗口内的
「跳过」）。

只读：不触发派发、不发起图调用、不改配置。计数来自进程内的 `GraphSync`（首次派发时
惰性创建），因此关闭态——以及开启但本进程尚未派发过任何事实时——全部读数为 0。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from auth import verify_auth
from fastapi import APIRouter, Depends, HTTPException
from server_state import get_memory_instance

logger = logging.getLogger(__name__)

router = APIRouter(tags=["graph"])

# 计数器键（设计 §8）：派发 / 同步成功 / 幂等跳过 / 失败 / 丢弃。
COUNTER_KEYS = (
    "graph_dispatched",
    "graph_synced",
    "graph_already_synced",
    "graph_failed",
    "graph_dropped",
)

# 能力开启但本进程尚未派发时，`GraphSync.stats()` 不存在，此时给出的零读数。
_EMPTY_STATS: Dict[str, Any] = {}


def _stats_of(memory: Any) -> Dict[str, Any]:
    """取实例上图派发器的计数快照。

    这里**不**调用 `_graph_sync_of`：观测端点不得因为被读取就创建派发器（那会连带
    建出图桥 HTTP 客户端）。未创建即视为零读数。
    """
    sync = getattr(memory, "_graph_sync", None)
    if sync is None:
        return _EMPTY_STATS
    try:
        return sync.stats()
    except Exception as exc:  # noqa: BLE001 - 观测面不得因读数失败而 5xx
        logger.warning("Reading graph dispatch counters failed: %s", exc)
        return _EMPTY_STATS


@router.get("/graph/stats", summary="Graph dispatch counters")
def graph_stats(_auth=Depends(verify_auth)) -> Dict[str, Any]:
    """图派发计数与熔断状态（只读快照）。

    返回 `enabled`（总开关）、`timeout_seconds`（生效的图检索预算，超时因此可解释）、
    五项 `graph_*` 计数，以及熔断状态、连续失败数与队列当前长度。
    """
    try:
        memory = get_memory_instance()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Mem0 runtime has not been initialized.") from exc

    config = getattr(getattr(memory, "config", None), "graph", None)
    stats = _stats_of(memory)

    payload: Dict[str, Any] = {
        "enabled": bool(getattr(config, "enabled", False)),
        "timeout_seconds": getattr(config, "timeout_seconds", None),
        "circuit_open": bool(stats.get("circuit_open", False)),
        "consecutive_failures": int(stats.get("consecutive_failures", 0)),
        "queue_size": int(stats.get("queue_size", 0)),
    }
    payload.update({key: int(stats.get(key, 0)) for key in COUNTER_KEYS})
    return payload
