"""图能力的只读观测端点（设计 `docs/design/graph-memory.md` §8）。

两个端点：

* `GET /graph/stats` 返回图派发计数器与熔断状态的当前值，使「队列有界且可观测」
  这类判据可以由实际读数判定，而不必拿图桥的访问日志当代理（日志里看不到冷却窗口内的
  「跳过」）。
* `GET /graph/keys` 代理图桥的图键清单与每个键的规模（设计
  `dashboard-mechanism-visibility.md` §6.3）：图规模与图键只在图桥侧可得，而图桥未映射
  宿主端口、且无鉴权，由 mem0 侧代理是唯一不新增前端可达面的通道。

只读：不触发派发、不发起图调用、不改配置、不建图键、不删图键。计数来自进程内的
`GraphSync`（首次派发时惰性创建），因此关闭态——以及开启但本进程尚未派发过任何事实
时——全部读数为 0。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx
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

# 代理图桥的读超时：清单一次、每个已存在的键一次规模读数。两者都是毫秒级读数
# （实测 `/graphs` 4ms、`/stats` 1–2ms），预算给足以覆盖图桥冷启动，又不至于把
# dashboard 的这张卡片拖成漫长的等待。
_BRIDGE_LIST_TIMEOUT_SECONDS = 5.0
_BRIDGE_STATS_TIMEOUT_SECONDS = 10.0

# 图规模的四个字段；顺序即响应里的字段顺序。
KEY_SCALE_FIELDS = ("episodes", "entity_nodes", "entity_edges")


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


def _require_memory() -> Any:
    try:
        return get_memory_instance()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Mem0 runtime has not been initialized.") from exc


def _bridge_endpoint(memory: Any) -> str:
    """图桥根地址：与派发/检索共用同一份配置，不新增配置项。"""
    config = getattr(getattr(memory, "config", None), "graph", None)
    return str(getattr(config, "endpoint", "") or "").rstrip("/")


@router.get("/graph/stats", summary="Graph dispatch counters")
def graph_stats(_auth=Depends(verify_auth)) -> Dict[str, Any]:
    """图派发计数与熔断状态（只读快照）。

    返回 `enabled`（总开关）、`timeout_seconds`（生效的图检索预算，超时因此可解释）、
    五项 `graph_*` 计数，以及熔断状态、连续失败数与队列当前长度。
    """
    memory = _require_memory()

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


@router.get("/graph/keys", summary="Graph keys and their sizes")
def graph_keys(_auth=Depends(verify_auth)) -> Dict[str, Any]:
    """图键清单与每个键的规模（只读代理）。

    **硬约束**：规模读数**只对清单内已存在的键**发起。图桥的 `GET /stats?group_id=`
    经 `registry.get()` 实例化 driver，FalkorDB 会惰性建出同名空图键——实测对一个不存在
    的键读一次规模，它就以 `episodes 0` 重新出现在 `GRAPH.LIST` 里。因此必须先取
    `GET /graphs`、再只对清单里的键逐个读规模；任何情况下都不得用试探键读数。

    降级语义与图侧其余路径一致（图侧故障不外溢）：图桥不可用（连接失败 / 非 2xx /
    畸形响应）时返回 `keys: []` 且 `degraded: true`；单个键的规模读失败时该键不进
    响应，并同样置 `degraded: true`——读失败与「规模为 0」必须不同形。
    """
    memory = _require_memory()
    endpoint = _bridge_endpoint(memory)
    if not endpoint:
        return {"keys": [], "degraded": True}

    try:
        with httpx.Client(base_url=endpoint, timeout=_BRIDGE_LIST_TIMEOUT_SECONDS) as client:
            listed = client.get("/graphs")
            listed.raise_for_status()
            body = listed.json()
            graph_keys_list: List[str] = [str(key) for key in (body.get("graphs") or []) if key]

            keys: List[Dict[str, Any]] = []
            degraded = False
            for group_id in graph_keys_list:
                try:
                    scale = client.get(
                        "/stats",
                        params={"group_id": group_id},
                        timeout=_BRIDGE_STATS_TIMEOUT_SECONDS,
                    )
                    scale.raise_for_status()
                    scale_body = scale.json()
                    keys.append(
                        {
                            "group_id": group_id,
                            **{field: int(scale_body.get(field, 0) or 0) for field in KEY_SCALE_FIELDS},
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - 单个键读失败不得让整页读数失败
                    logger.warning("Reading graph size for %s failed: %s", group_id, exc)
                    degraded = True
    except Exception as exc:  # noqa: BLE001 - 图桥故障按降级返回，不影响其它端点
        logger.warning("Listing graph keys failed: %s", exc)
        return {"keys": [], "degraded": True}

    return {"keys": keys, "degraded": degraded}
