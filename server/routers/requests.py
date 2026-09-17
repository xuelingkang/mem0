import uuid
from datetime import datetime, timedelta, timezone

from auth import require_admin
from db import get_db
from fastapi import APIRouter, Depends, Query
from models import RequestLog
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

router = APIRouter(prefix="/requests", tags=["requests"])


class RequestLogItem(BaseModel):
    id: uuid.UUID
    method: str
    path: str
    status_code: int
    latency_ms: float
    auth_type: str
    created_at: datetime

    model_config = {"from_attributes": True}


API_KEY_AUTH_TYPES = ("api_key", "admin_api_key")


@router.get("", response_model=list[RequestLogItem])
def list_requests(
    _auth=Depends(require_admin),
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
):
    logs = (
        db.execute(
            select(RequestLog)
            .where(RequestLog.auth_type.in_(API_KEY_AUTH_TYPES))
            .order_by(RequestLog.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return logs


class StatusBucket(BaseModel):
    status_code: int
    count: int


class DayBucket(BaseModel):
    date: str
    total: int
    errors: int
    avg_latency_ms: float


class PathBucket(BaseModel):
    path: str
    count: int
    avg_latency_ms: float


class RequestStats(BaseModel):
    total: int
    success_rate: float
    avg_latency_ms: float
    p95_latency_ms: float
    window_days: int
    by_day: list[DayBucket]
    by_status: list[StatusBucket]
    top_paths: list[PathBucket]


@router.get("/stats", response_model=RequestStats)
def request_stats(
    _auth=Depends(require_admin),
    db: Session = Depends(get_db),
    days: int = Query(default=14, ge=1, le=90),
):
    """Aggregated request activity for the dashboard's Analytics view.

    Aggregation runs in the database (count/avg/percentile + group by), so the
    response stays small regardless of how many request rows the window holds.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    conds = (
        RequestLog.auth_type.in_(API_KEY_AUTH_TYPES),
        RequestLog.created_at >= since,
    )

    total = db.scalar(select(func.count()).select_from(RequestLog).where(*conds)) or 0
    avg_latency = db.scalar(select(func.avg(RequestLog.latency_ms)).where(*conds))
    p95_latency = db.scalar(
        select(
            func.percentile_cont(0.95).within_group(RequestLog.latency_ms)
        ).where(*conds)
    )
    ok_count = (
        db.scalar(
            select(func.count())
            .select_from(RequestLog)
            .where(*conds, RequestLog.status_code < 400)
        )
        or 0
    )

    by_day_rows = db.execute(
        select(
            func.date_trunc("day", RequestLog.created_at).label("day"),
            func.count().label("total"),
            func.count().filter(RequestLog.status_code >= 400).label("errors"),
            func.avg(RequestLog.latency_ms).label("avg_latency"),
        )
        .where(*conds)
        .group_by("day")
        .order_by("day")
    ).all()

    by_status_rows = db.execute(
        select(RequestLog.status_code, func.count().label("cnt"))
        .where(*conds)
        .group_by(RequestLog.status_code)
        .order_by(func.count().desc())
    ).all()

    top_path_rows = db.execute(
        select(
            RequestLog.path,
            func.count().label("cnt"),
            func.avg(RequestLog.latency_ms).label("avg_latency"),
        )
        .where(*conds)
        .group_by(RequestLog.path)
        .order_by(func.count().desc())
        .limit(10)
    ).all()

    return RequestStats(
        total=total,
        success_rate=round((ok_count / total) * 100, 1) if total else 0.0,
        avg_latency_ms=round(float(avg_latency or 0), 1),
        p95_latency_ms=round(float(p95_latency or 0), 1),
        window_days=days,
        by_day=[
            DayBucket(
                date=row.day.strftime("%Y-%m-%d"),
                total=row.total,
                errors=row.errors,
                avg_latency_ms=round(float(row.avg_latency or 0), 1),
            )
            for row in by_day_rows
        ],
        by_status=[
            StatusBucket(status_code=row[0], count=row[1]) for row in by_status_rows
        ],
        top_paths=[
            PathBucket(
                path=row[0],
                count=row[1],
                avg_latency_ms=round(float(row[2] or 0), 1),
            )
            for row in top_path_rows
        ],
    )
