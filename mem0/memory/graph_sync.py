"""图记忆同步：把新事实派发入图（Graphiti 图桥），并把图关联折算为补充加分。

设计见 `docs/design/graph-memory.md`。本模块只负责与图桥相关的三件事，事实主存
（Qdrant）在任何路径上都不被改写：

| 职责 | 入口 | 关键约束 |
| --- | --- | --- |
| 作用域 → 图键 | `derive_group_id` | 纯函数，写入派发与检索查询共用同一来源 |
| 事实入图 | `GraphSync.dispatch` / `GraphSync` 的串行 worker | 内存队列投递 O(1) 且不抛异常；失败重试、连续失败熔断 |
| 图检索与折算 | `search_graph_facts` / `compute_graph_boosts` | 硬超时预算；纯函数折算，不依赖 LLM |

两条不变量：

* **降级**：图桥不可用时写入与检索照常，图分支静默跳过——派发失败只记计数器，
  图检索异常/超时一律按「本次无图信号」返回。
* **写路径零阻塞**：`dispatch` 只做一次内存入队；HTTP 调用发生在独立线程的事件循环里，
  与 FastAPI 的请求线程池互不占用。
"""

from __future__ import annotations

import asyncio
import logging
import queue
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

from mem0.configs.base import GraphConfig

logger = logging.getLogger(__name__)

# 作用域键的优先级：user_id 优先，其次 agent_id，再次 run_id（与 mem0 的作用域语义一致）。
SCOPE_KEYS = ("user_id", "agent_id", "run_id")

# 图键前缀。graphiti 侧 `validate_group_id` 只接受 [A-Za-z0-9_-]，因此分隔符取下划线
# 而非冒号，作用域值里的其它字符一并归一为下划线（同一作用域恒得同一图键）。
GROUP_ID_PREFIX = "mem0"
_GROUP_ID_MAX_LENGTH = 96
_GROUP_ID_INVALID = re.compile(r"[^0-9A-Za-z_-]")

# 图检索事实的排序权重：w(k) = 1 / (1 + 0.5 × (k − 1))，k 从 1 起。
_RANK_DECAY_STEP = 0.5

# 入队的最大尝试次数：队列被并发排空时的兜底上界，保证派发是 O(1) 且必然返回。
_QUEUE_PUSH_ATTEMPTS = 8


def sanitize_group_id(value: Any) -> str:
    """把作用域值归一为 graphiti 接受的图键片段。

    只保留 ASCII 字母/数字/下划线/连字符，其余字符替换为下划线；超长时截断（图键是
    Redis key 名，长度受键名限制约束）。
    """
    text = _GROUP_ID_INVALID.sub("_", str(value).strip())
    if len(text) > _GROUP_ID_MAX_LENGTH:
        text = text[:_GROUP_ID_MAX_LENGTH]
    return text


def _scope_from_filters(filters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """取出作用域键，兼容被 `AND` 包裹的谓词（与 `_scope_filters` 同形）。"""
    if not filters:
        return {}
    if isinstance(filters.get("AND"), list):
        merged: Dict[str, Any] = {}
        for sub in filters["AND"]:
            merged.update(_scope_from_filters(sub))
        return merged
    return {k: v for k, v in filters.items() if k in SCOPE_KEYS and v}


def derive_group_id(filters: Optional[Dict[str, Any]]) -> Optional[str]:
    """由作用域派生图键；派生不出作用域时返回 None（调用方一律跳过图分支）。

    写入派发与检索查询共用本函数，保证「写进哪个图」与「从哪里检索」永不漂移。
    """
    scope = _scope_from_filters(filters)
    for key in SCOPE_KEYS:
        value = scope.get(key)
        if value:
            return f"{GROUP_ID_PREFIX}_{sanitize_group_id(value)}"
    return None


def compute_graph_boosts(
    facts: Sequence[Dict[str, Any]],
    candidate_ids: Iterable[str],
    weight: float,
    include_invalidated: bool = False,
) -> Tuple[Dict[str, float], Dict[str, int]]:
    """把图检索结果折算为候选池内的加分（纯函数，设计 §6.2）。

    Args:
        facts: 图桥返回的事实列表，**顺序即相关度序**（第 k 条权重 `1/(1+0.5(k-1))`）。
        candidate_ids: 本次检索的候选池 id 集合；池外 id 一律忽略。
        weight: 图加分上限 `W_g`。
        include_invalidated: 是否采用 `invalid_at` 非空的事实。

    Returns:
        `(boosts, fact_counts)`：前者仅含加分 > 0 的候选（用于判断分母是否增长），
        后者记录每个候选被多少条事实命中（可解释输出用）。
    """
    candidates = {str(mid) for mid in candidate_ids}
    boosts: Dict[str, float] = {}
    fact_counts: Dict[str, int] = {}
    if weight <= 0:
        return boosts, fact_counts

    for index, fact in enumerate(facts):
        if not isinstance(fact, dict):
            continue
        # 图侧已失效的事实默认不参与加分，与主通道的失效语义保持一致（设计 §2.1 F8）。
        if not include_invalidated and fact.get("invalid_at"):
            continue
        episodes = fact.get("episodes")
        if not isinstance(episodes, list):
            continue
        rank_weight = 1.0 / (1.0 + _RANK_DECAY_STEP * index)
        for episode in episodes:
            if episode is None:
                continue
            memory_id = str(episode)
            if memory_id not in candidates:
                continue
            boost = weight * rank_weight
            if boost > boosts.get(memory_id, 0.0):
                boosts[memory_id] = boost
            fact_counts[memory_id] = fact_counts.get(memory_id, 0) + 1

    return boosts, fact_counts


def _parse_reference_time(value: Any) -> datetime:
    """把 ISO 字符串/日期时间解析为 UTC datetime；不可解析时取当前时刻。"""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
    else:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class GraphBridgeClient:
    """图桥的同步 HTTP 客户端（写入侧在 worker 线程使用，检索侧在请求线程使用）。"""

    def __init__(self, endpoint: str, timeout_seconds: float = 120.0):
        self.endpoint = endpoint.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = httpx.Client(base_url=self.endpoint, timeout=timeout_seconds)

    def post_episode(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """投递一条 episode；非 2xx 抛异常（由调用方按失败处理）。"""
        response = self._client.post("/episodes", json=payload)
        response.raise_for_status()
        return response.json()

    def search(self, payload: Dict[str, Any], timeout_seconds: float) -> Dict[str, Any]:
        """图检索；超时预算由调用方按次给出。"""
        response = self._client.post("/search", json=payload, timeout=timeout_seconds)
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()


def search_graph_facts(
    client: Any,
    group_id: Optional[str],
    query: str,
    max_facts: int,
    timeout_seconds: float,
) -> List[Dict[str, Any]]:
    """取回一次图检索的事实列表；任何失败（含超时、畸形响应）都返回空列表。

    检索缺图信号按「无信号」打分，因此本函数**不抛异常**：图侧故障的可见面是日志与
    计数器，而不是响应内容或响应时延（设计 §6.4）。
    """
    if not group_id or not query:
        return []
    try:
        payload = client.search(
            {"group_ids": [group_id], "query": query, "max_facts": max_facts},
            timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - 图侧任何异常都不得外溢到主检索
        logger.debug("Graph search skipped (bridge unavailable): %s", exc)
        return []

    facts = payload.get("facts") if isinstance(payload, dict) else None
    if not isinstance(facts, list):
        return []
    return [fact for fact in facts if isinstance(fact, dict)]


class GraphSync:
    """把新写入的事实异步派发入图（设计 §5.1–5.3）。

    队列为进程内有界队列；worker 为单线程 + 独立事件循环，串行调用图桥（Graphiti 要求
    同一分区的 episode 顺序摄入）。派发路径只做内存入队，不做任何 I/O。
    """

    def __init__(self, config: GraphConfig, client: Optional[Any] = None):
        self.config = config
        self._client = client if client is not None else GraphBridgeClient(config.endpoint, config.request_timeout_seconds)
        self._queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=config.queue_size)
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = {
            "graph_dispatched": 0,
            "graph_synced": 0,
            "graph_already_synced": 0,
            "graph_failed": 0,
            "graph_dropped": 0,
        }
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._worker: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    # ------------------------------------------------------------------ counters

    def stats(self) -> Dict[str, Any]:
        """返回派发计数与熔断状态（`/stats` 与测试的观测面）。"""
        with self._lock:
            snapshot = dict(self._counters)
            snapshot["circuit_open"] = self._circuit_open_until > time.monotonic()
            snapshot["queue_size"] = self._queue.qsize()
            snapshot["consecutive_failures"] = self._consecutive_failures
        return snapshot

    def _bump(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    # ------------------------------------------------------------------ dispatch

    def dispatch(self, memory_id: str, text: str, created_at: Any, group_id: Optional[str]) -> bool:
        """把一个新事实投递进队列；返回是否入队。

        入队是 O(1) 的内存操作且不抛异常：队列满时丢弃最旧任务并累加 `graph_dropped`，
        任何异常都被吞掉（写入结果不因图侧而改变）。
        """
        if not self.config.enabled or not group_id or not memory_id:
            return False
        task = {
            "uuid": str(memory_id),
            "group_id": group_id,
            "text": text,
            "reference_time": _parse_reference_time(created_at).isoformat(),
            "source_description": "mem0 事实同步",
        }
        try:
            # 队列满时丢弃最旧任务再入队；重试次数有界，避免队列被并发排空时死循环。
            for _ in range(_QUEUE_PUSH_ATTEMPTS):
                try:
                    self._queue.put_nowait(task)
                    break
                except queue.Full:
                    try:
                        self._queue.get_nowait()
                        self._bump("graph_dropped")
                    except queue.Empty:
                        continue
            else:
                self._bump("graph_dropped")
                return False
            self._bump("graph_dispatched")
            self.start()
            return True
        except Exception as exc:  # noqa: BLE001 - 派发失败不得影响写入结果
            logger.warning("Graph dispatch failed for %s: %s", memory_id, exc)
            self._bump("graph_dropped")
            return False

    # ------------------------------------------------------------------ worker

    def start(self) -> None:
        """惰性启动 worker 线程（首次派发时）。"""
        if self._worker is not None and self._worker.is_alive():
            return
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stopping.clear()
            self._worker = threading.Thread(target=self._run, name="mem0-graph-sync", daemon=True)
            self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        """停止 worker（测试与关停用）；队列中未处理的任务不保证完成。"""
        self._stopping.set()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=timeout)
        self._worker = None

    def _run(self) -> None:
        """worker 主体：在独立事件循环里串行处理队列。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while not self._stopping.is_set():
                try:
                    task = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if task is None:
                    continue
                try:
                    loop.run_until_complete(self._sync_one(task))
                except Exception as exc:  # noqa: BLE001 - worker 不得因单条任务退出
                    logger.warning("Graph sync worker error: %s", exc)
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    def _circuit_is_open(self) -> bool:
        return self._circuit_open_until > time.monotonic()

    def _register_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.config.circuit_breaker_failures:
                self._circuit_open_until = time.monotonic() + self.config.circuit_cooldown_seconds
                logger.warning(
                    "Graph sync circuit opened for %.1fs after %d consecutive failures",
                    self.config.circuit_cooldown_seconds,
                    self._consecutive_failures,
                )

    def _register_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0

    async def _sync_one(self, task: Dict[str, Any]) -> None:
        """投递单条 episode，带重试；冷却窗口内的任务直接丢弃并计数（设计 §5.2）。"""
        if self._circuit_is_open():
            self._bump("graph_dropped")
            return
        attempts = self.config.max_retries + 1
        for attempt in range(attempts):
            if attempt > 0:
                await asyncio.sleep(attempt * self.config.retry_backoff_seconds)
            try:
                result = await asyncio.to_thread(self._client.post_episode, task)
            except Exception as exc:  # noqa: BLE001 - 非 2xx / 连接失败 / 超时一律视为失败
                logger.debug("Graph episode sync attempt %d failed: %s", attempt + 1, exc)
                continue
            self._register_success()
            status = (result or {}).get("status")
            self._bump("graph_already_synced" if status == "already_synced" else "graph_synced")
            return
        self._bump("graph_failed")
        self._register_failure()

    def close(self) -> None:
        """停止 worker 并关闭 HTTP 客户端。"""
        self.stop()
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass
