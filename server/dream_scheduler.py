"""Dream 后台调度的服务侧载体（设计 `docs/design/memory-dream.md` §5.6）。

职责边界：把 SDK 的四阶段整合（`mem0.memory.dream`）挂到 server 进程上——

* 周期触发（进程内后台线程，不新增容器、不引入外部调度服务）；
* 并发互斥（`history/dream.lock` 上的 `fcntl.flock`，跨进程与同进程异 fd 都生效）；
* 成本跳过表与运行审计表的持久化（Postgres，唯一写入方）。

整合的**正确性不依赖这里**：状态表丢失只导致重复调用 LLM，锁丢失只导致并发开销。
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Sequence

from db import SessionLocal
from mem0.memory.dream import (
    DreamClusterStateStore,
    DreamObserver,
    DreamSettings,
    run_dream,
    write_report,
)
from models import DreamClusterState, DreamRun
from sqlalchemy import func, select

logger = logging.getLogger(__name__)

MODE_DRY_RUN = "dry_run"
MODE_LIVE = "live"
STATUS_SKIPPED = "skipped"

DEFAULT_LOCK_PATH = "/app/history/dream.lock"
DEFAULT_REPORT_DIR = "/app/history/dream-reports"
DEFAULT_INTERVAL_SECONDS = 86400.0
DEFAULT_INITIAL_DELAY_SECONDS = 1800.0
DEFAULT_RUN_TIMEOUT_SECONDS = 1800.0
DEFAULT_PER_CLUSTER_TIMEOUT_SECONDS = 60.0


def _positive(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def settings_from_config(config: Dict[str, Any]) -> DreamSettings:
    """从配置的 `dream` 段构造整合参数，缺项一律回落到设计默认值。

    配置链路沿用既有机制（`.env` → `DEFAULT_CONFIG` → 可运行时覆盖），因此运行中的
    参数调整无需改代码；非法值不报错，按缺省处理。
    """
    section = config.get("dream") if isinstance(config, dict) else None
    section = section if isinstance(section, dict) else {}
    defaults = DreamSettings()
    return DreamSettings(
        tau=_positive(section.get("tau"), defaults.tau),
        min_cluster_size=int(_positive(section.get("min_cluster_size"), defaults.min_cluster_size)),
        max_cluster_size=int(_positive(section.get("max_cluster_size"), defaults.max_cluster_size)),
        max_clusters_per_run=int(_positive(section.get("max_clusters_per_run"), defaults.max_clusters_per_run)),
        per_cluster_timeout_seconds=_positive(
            section.get("per_cluster_timeout_seconds"), defaults.per_cluster_timeout_seconds
        ),
        run_timeout_seconds=_positive(section.get("run_timeout_seconds"), defaults.run_timeout_seconds),
        report_dir=str(section.get("report_dir") or defaults.report_dir),
    )


def scheduler_options_from_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """取出调度本身（开关、周期、锁位置）的参数。"""
    section = config.get("dream") if isinstance(config, dict) else None
    section = section if isinstance(section, dict) else {}
    return {
        "enabled": bool(section.get("enabled", False)),
        "interval_seconds": _positive(section.get("interval_seconds"), DEFAULT_INTERVAL_SECONDS),
        "initial_delay_seconds": _positive(
            section.get("initial_delay_seconds"), DEFAULT_INITIAL_DELAY_SECONDS
        ),
        "lock_path": str(section.get("lock_path") or DEFAULT_LOCK_PATH),
        "report_dir": str(section.get("report_dir") or DEFAULT_REPORT_DIR),
    }


class SqlClusterStateStore(DreamClusterStateStore):
    """`dream_cluster_states` 的 Postgres 实现（成本跳过）。

    同一 key 再次判定时更新 `decision` / `last_evaluated_at` / `evaluations`；任何数据库
    异常都只记日志，不向上抛出——状态表不可用必须退化为「重复判定」（贵），而不是
    让整轮整合失败。
    """

    def __init__(self, session_factory: Callable = SessionLocal) -> None:
        self._session_factory = session_factory

    def known_keys(self) -> set:
        session = self._session_factory()
        try:
            return {row[0] for row in session.execute(select(DreamClusterState.observation_key)).all()}
        finally:
            session.close()

    def record(
        self,
        *,
        observation_key: str,
        scope_user_id: str,
        scope_agent_id: str,
        decision: str,
        observed_point_id: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        session = self._session_factory()
        try:
            row = session.get(DreamClusterState, observation_key)
            if row is None:
                session.add(
                    DreamClusterState(
                        observation_key=observation_key,
                        scope_user_id=str(scope_user_id),
                        scope_agent_id=str(scope_agent_id),
                        decision=decision,
                        observed_point_id=observed_point_id,
                        member_ids_hash=observation_key,
                        first_seen_at=now,
                        last_evaluated_at=now,
                        evaluations=1,
                    )
                )
            else:
                row.decision = decision
                row.observed_point_id = observed_point_id
                row.last_evaluated_at = now
                row.evaluations = int(row.evaluations or 0) + 1
            session.commit()
        except Exception:
            session.rollback()
            logger.warning("Dream cluster state write failed for %s", observation_key, exc_info=True)
        finally:
            session.close()

    def count(self) -> int:
        """状态表行数（供干跑/实测核对；不参与判定）。"""
        session = self._session_factory()
        try:
            return int(session.scalar(select(func.count()).select_from(DreamClusterState)) or 0)
        finally:
            session.close()

    def touch(self, *, observation_key: str) -> None:
        """记一次「该簇本轮被判定为无需重算」：不动 `decision`，只更新时间与计数。"""
        now = datetime.now(timezone.utc)
        session = self._session_factory()
        try:
            row = session.get(DreamClusterState, observation_key)
            if row is None:
                # 状态表缺失该行（例如行被清理）：不回填——回填需要决策内容，而那正是
                # 本轮没有做的判定。缺失退化为下一轮重新判定（更贵，不错）。
                return
            row.last_evaluated_at = now
            row.evaluations = int(row.evaluations or 0) + 1
            session.commit()
        except Exception:
            session.rollback()
            logger.warning("Dream cluster state touch failed for %s", observation_key, exc_info=True)
        finally:
            session.close()


class SqlRunObserver(DreamObserver):
    """`dream_runs` 的 Postgres 实现（运行审计）。dry-run 不会构造它。"""

    def __init__(self, session_factory: Callable = SessionLocal) -> None:
        self._session_factory = session_factory

    def start_run(self, *, run_id: str, mode: str, started_at: str) -> None:
        session = self._session_factory()
        try:
            session.add(
                DreamRun(
                    id=run_id,
                    mode=mode,
                    started_at=datetime.fromisoformat(started_at),
                    status="running",
                )
            )
            session.commit()
        except Exception:
            session.rollback()
            logger.warning("Dream run row create failed for %s", run_id, exc_info=True)
        finally:
            session.close()

    def finish_run(
        self,
        *,
        run_id: str,
        status: str,
        stats: Dict[str, Any],
        report_path: Optional[str] = None,
    ) -> None:
        totals = (stats or {}).get("totals") or {}
        session = self._session_factory()
        try:
            row = session.get(DreamRun, run_id)
            if row is None:
                return
            row.status = status
            row.finished_at = datetime.now(timezone.utc)
            row.scopes = len((stats or {}).get("scopes") or [])
            row.clusters = int(totals.get("clusters") or 0)
            row.llm_calls = int(totals.get("llm_calls") or 0)
            row.failed_clusters = int(totals.get("failed_clusters") or 0)
            row.observations_written = int((stats or {}).get("observations_written") or 0)
            row.observations_superseded = int((stats or {}).get("observations_superseded") or 0)
            row.prompt_tokens = int(totals.get("prompt_tokens") or 0)
            row.completion_tokens = int(totals.get("completion_tokens") or 0)
            row.duration_seconds = float((stats or {}).get("duration_seconds") or 0.0)
            if report_path:
                row.report_path = report_path
            session.commit()
        except Exception:
            session.rollback()
            logger.warning("Dream run row update failed for %s", run_id, exc_info=True)
        finally:
            session.close()

    def attach_report(self, run_id: str, report_path: str) -> None:
        """报告落盘后回填路径（报告内容依赖 run 的返回值，只能事后补写）。"""
        session = self._session_factory()
        try:
            row = session.get(DreamRun, run_id)
            if row is not None:
                row.report_path = report_path
                session.commit()
        except Exception:
            session.rollback()
            logger.warning("Dream report path update failed for %s", run_id, exc_info=True)
        finally:
            session.close()

    def record_skipped(self, *, run_id: str, mode: str, started_at: str) -> None:
        """锁未获取到的一轮：记 `skipped`、零写入（设计 §5.6）。"""
        session = self._session_factory()
        try:
            session.add(
                DreamRun(
                    id=run_id,
                    mode=mode,
                    started_at=datetime.fromisoformat(started_at),
                    finished_at=datetime.now(timezone.utc),
                    status=STATUS_SKIPPED,
                )
            )
            session.commit()
        except Exception:
            session.rollback()
            logger.warning("Dream skipped-run row failed for %s", run_id, exc_info=True)
        finally:
            session.close()


class RunLock:
    """`history/dream.lock` 上的独占锁（`flock(LOCK_EX | LOCK_NB)`）。

    线程退出即释放（进程退出由内核释放）；lock 文件本身保留在卷上，不删除——删除会
    引入「unlink 后另一个进程在旧 inode 上取锁」的竞态。同进程内以不同 fd 重复获取
    同样被挡（flock 的锁属于 open file description）。
    """

    def __init__(self, path: str) -> None:
        self.path = str(path)
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            logger.info("Dream lock not acquired (%s): %s", self.path, exc)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:  # 进程退出时内核已释放，无需再处理
            logger.debug("Dream lock release skipped", exc_info=True)
        finally:
            os.close(self._fd)
            self._fd = None


class DreamScheduler:
    """进程内后台调度：周期触发、手动触发，共用同一把锁与同一套流程。"""

    def __init__(
        self,
        *,
        get_config: Callable[[], Dict[str, Any]],
        get_memory: Callable[[], Any],
        state_store: Optional[DreamClusterStateStore] = None,
        observer: Optional[DreamObserver] = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS,
        lock_path: str = DEFAULT_LOCK_PATH,
        enabled: bool = False,
    ) -> None:
        self._get_config = get_config
        self._get_memory = get_memory
        self._state_store = state_store or SqlClusterStateStore()
        self._observer = observer
        self._interval_seconds = interval_seconds
        self._initial_delay_seconds = initial_delay_seconds
        self._lock = RunLock(lock_path)
        self._enabled = enabled
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def enabled(self) -> bool:
        return bool(scheduler_options_from_config(self._get_config()).get("enabled", self._enabled))

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """启动周期线程。总开关关闭时不启动（设计 §5.6 的降级行为）。"""
        options = scheduler_options_from_config(self._get_config())
        if not options["enabled"]:
            logger.info("Dream scheduler disabled (dream_enabled=false); periodic thread not started")
            return
        if self.running:
            return
        self._interval_seconds = options["interval_seconds"]
        self._initial_delay_seconds = options["initial_delay_seconds"]
        self._lock = RunLock(options["lock_path"])
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mem0-dream-scheduler", daemon=True)
        self._thread.start()
        logger.info(
            "Dream scheduler started: first run in %.0fs, then every %.0fs",
            self._initial_delay_seconds,
            self._interval_seconds,
        )

    def stop(self) -> None:
        """关停：置停止事件，线程在单簇边界处退出。"""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
        self._thread = None

    def _loop(self) -> None:
        if self._stop.wait(self._initial_delay_seconds):
            return
        while not self._stop.is_set():
            try:
                self.run_once(mode=MODE_LIVE, trigger="schedule")
            except Exception:
                logger.exception("Dream scheduled run failed; the next cycle retries")
            if self._stop.wait(self._interval_seconds):
                return

    def run_once(
        self,
        *,
        mode: str = MODE_LIVE,
        scopes: Optional[Sequence[str]] = None,
        trigger: str = "manual",
    ) -> Dict[str, Any]:
        """执行一轮整合。

        锁未获取到时返回 `{"status": "skipped", ...}` 且零写入（实跑还会落一行
        `skipped` 审计）。总开关关闭时返回 `{"status": "disabled"}`，由调用方决定
        HTTP 语义（端点返回 409）。
        """
        options = scheduler_options_from_config(self._get_config())
        if not options["enabled"]:
            return {"status": "disabled", "mode": mode, "trigger": trigger}

        run_id = None
        started_at = datetime.now(timezone.utc).isoformat()
        if not self._lock.acquire():
            if mode == MODE_LIVE and self._observer is not None:
                run_id = str(uuid.uuid4())
                self._observer.record_skipped(run_id=run_id, mode=mode, started_at=started_at)
            return {
                "status": STATUS_SKIPPED,
                "mode": mode,
                "run_id": run_id,
                "reason": "another dream run holds the lock",
            }

        try:
            settings = settings_from_config(self._get_config())
            observer = self._observer if mode == MODE_LIVE else None
            report = run_dream(
                self._get_memory(),
                mode=mode,
                settings=settings,
                state_store=self._state_store,
                observer=observer,
                scopes=scopes,
            )
            report_path = write_report(report, settings.report_dir)
            report["report_path"] = report_path
            if observer is not None:
                observer.attach_report(report["run_id"], report_path)
            report["trigger"] = trigger
            return report
        finally:
            self._lock.release()


_scheduler: Optional[DreamScheduler] = None
_scheduler_lock = threading.Lock()


def configure_scheduler(scheduler: Optional[DreamScheduler]) -> None:
    """登记进程级调度器实例（由应用生命周期钩子调用）。"""
    global _scheduler
    with _scheduler_lock:
        _scheduler = scheduler


def get_scheduler() -> Optional[DreamScheduler]:
    with _scheduler_lock:
        return _scheduler
