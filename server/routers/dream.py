"""Dream 的 HTTP 端点（设计 `docs/design/memory-dream.md` §5.9）。

四组端点：

* 触发：`POST /dream/preview`（dry-run）、`POST /dream/run`（实跑）；
* 运行记录：`GET /dream/runs`、`GET /dream/runs/{run_id}`；
* 证据链反向追溯：`GET /memories/{memory_id}/observations`；
* 证据链正向追溯：`GET /memories/{observation_id}/sources`。

触发端点共用同一把锁与同一套流程；`dream_enabled=false` 或锁未获取到时返回 409。
追溯端点复用既有 `/memories/{id}` 的序列化口径（观察字段是顶层一等字段）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from auth import verify_auth
from db import SessionLocal
from dream_scheduler import MODE_DRY_RUN, MODE_LIVE, STATUS_SKIPPED, get_scheduler
from errors import upstream_error
from fastapi import APIRouter, Depends, HTTPException, Query
from models import DreamRun
from pydantic import BaseModel, Field
from server_state import get_memory_instance
from sqlalchemy import desc, func, select

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dream"])

# 观察条目标记（与 `mem0.configs.prompts.MEMORY_KIND_OBSERVATION` 同值；此处不 import
# SDK 常量，避免端点模块为取一个字符串而依赖整合内核）。
MEMORY_KIND_OBSERVATION = "observation"
_OBSERVATION_FIELDS = ("memory_kind", "observation_key", "source_memory_ids", "evidence_count", "dream_run_id")


class DreamRunRequest(BaseModel):
    """一轮整合的触发参数。"""

    scopes: Optional[List[str]] = Field(
        None,
        description=(
            "可选：只处理指定作用域，取值 `user_id` 或 `user_id:agent_id`。"
            "缺省为全部作用域（周期任务即走缺省）。"
        ),
    )


def _require_scheduler():
    scheduler = get_scheduler()
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Dream scheduler is not initialized.")
    return scheduler


def _run(mode: str, scopes: Optional[List[str]] = None) -> Dict[str, Any]:
    """执行一轮整合并按 HTTP 语义翻译结果。"""
    scheduler = _require_scheduler()
    try:
        report = scheduler.run_once(mode=mode, scopes=scopes, trigger="manual")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Dream %s run failed", mode)
        raise upstream_error()

    status = report.get("status")
    if status == "disabled":
        raise HTTPException(status_code=409, detail="Dream is disabled. Set dream_enabled=true to run it.")
    if status == STATUS_SKIPPED:
        raise HTTPException(status_code=409, detail="Another dream run is in progress.")
    # 报告体量大（含全部候选），但它是审阅的唯一入口，与落盘内容逐字段一致。
    return report


@router.post("/dream/preview", summary="Preview a dream run (dry-run)")
def preview_dream(request: Optional[DreamRunRequest] = None, _auth=Depends(verify_auth)):
    """执行一轮 dry-run：完整走四阶段，零数据写入，返回候选报告（同落盘）。"""
    scopes = request.scopes if request else None
    return _run(MODE_DRY_RUN, scopes)


@router.post("/dream/run", summary="Run dream (live)")
def run_dream_endpoint(request: Optional[DreamRunRequest] = None, _auth=Depends(verify_auth)):
    """执行一轮实跑：生成观察条目并保留证据链。未启用或锁未获取时返回 409。"""
    scopes = request.scopes if request else None
    return _run(MODE_LIVE, scopes)


def _serialize_run(row: DreamRun) -> Dict[str, Any]:
    return {
        "id": str(row.id),
        "mode": row.mode,
        "status": row.status,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "scopes": row.scopes,
        "clusters": row.clusters,
        "llm_calls": row.llm_calls,
        "failed_clusters": row.failed_clusters,
        "observations_written": row.observations_written,
        "observations_superseded": row.observations_superseded,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "duration_seconds": row.duration_seconds,
        "report_path": row.report_path,
    }


@router.get("/dream/runs", summary="List dream runs")
def list_dream_runs(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _auth=Depends(verify_auth),
):
    """运行记录列表，按 `started_at` 倒序，附总数便于翻页。"""
    session = SessionLocal()
    try:
        total = int(session.scalar(select(func.count()).select_from(DreamRun)) or 0)
        rows = session.scalars(
            select(DreamRun).order_by(desc(DreamRun.started_at)).limit(limit).offset(offset)
        ).all()
        return {"total": total, "limit": limit, "offset": offset, "results": [_serialize_run(row) for row in rows]}
    finally:
        session.close()


@router.get("/dream/runs/{run_id}", summary="Get one dream run")
def get_dream_run(run_id: str, _auth=Depends(verify_auth)):
    """单轮统计与报告路径。"""
    session = SessionLocal()
    try:
        row = session.get(DreamRun, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Dream run not found.")
        return _serialize_run(row)
    finally:
        session.close()


def _observation_rows(payload: Dict[str, Any]) -> bool:
    return payload.get("memory_kind") == MEMORY_KIND_OBSERVATION


def _iter_observations() -> List[Dict[str, Any]]:
    """遍历集合内全部观察条目的 `(id, payload)`（只读，游标分页）。"""
    vector_store = get_memory_instance().vector_store
    rows: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        page = vector_store.list(
            filters={"memory_kind": {"eq": MEMORY_KIND_OBSERVATION}}, top_k=1000, cursor=cursor
        )
        page = page[0] if page and isinstance(page, (list, tuple)) and isinstance(page[0], (list, tuple)) else page
        page = list(page or [])
        if not page:
            break
        for item in page:
            payload = getattr(item, "payload", None) or {}
            if _observation_rows(payload):
                rows.append({"id": str(item.id), "payload": payload})
        last_created = (getattr(page[-1], "payload", None) or {}).get("created_at")
        if not last_created or len(page) < 1000:
            break
        cursor = str(last_created)
    return rows


@router.get("/memories/{memory_id}/observations", summary="Observations backed by a source memory")
def observations_for_memory(memory_id: str, _auth=Depends(verify_auth)):
    """反向追溯：返回 `source_memory_ids` 含该 id 的全部观察（含已失效版本）。"""
    try:
        matches = [
            row
            for row in _iter_observations()
            if str(memory_id) in [str(sid) for sid in (row["payload"].get("source_memory_ids") or [])]
        ]
        matches.sort(key=lambda row: str(row["payload"].get("created_at") or ""), reverse=True)
        return {
            "memory_id": memory_id,
            "total": len(matches),
            "results": [{field: row["payload"].get(field) for field in _OBSERVATION_FIELDS} | {"id": row["id"]} for row in matches],
        }
    except HTTPException:
        raise
    except Exception:
        raise upstream_error()


@router.get("/memories/{observation_id}/sources", summary="Source facts of an observation")
def sources_of_observation(observation_id: str, _auth=Depends(verify_auth)):
    """正向追溯：返回该观察的全部源事实条目（缺失的源事实原样跳过，不做清洗）。"""
    try:
        memory = get_memory_instance()
        observation = memory.get(observation_id)
        if not observation:
            raise HTTPException(status_code=404, detail="Observation not found.")
        if not _observation_rows(observation):
            raise HTTPException(status_code=400, detail="Not an observation.")
        source_ids = [str(sid) for sid in (observation.get("source_memory_ids") or [])]
        results = []
        for source_id in source_ids:
            try:
                source = memory.get(source_id)
            except Exception:
                source = None
            if source:
                results.append(source)
        return {
            "observation_id": observation_id,
            "total": len(results),
            "missing": len(source_ids) - len(results),
            "results": results,
        }
    except HTTPException:
        raise
    except Exception:
        raise upstream_error()
