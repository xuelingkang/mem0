"""dashboard-mechanism-visibility 设计探针 D：dry-run 零写入验证（读两次 + 一次 dry-run）。

复跑：
    cd /Users/xuelingkang/Documents/Containers/mem0/server
    docker compose cp ../docs/design/dashboard-mechanism-visibility-evidence/probe_dream_dryrun.py mem0:/tmp/
    docker compose exec -T mem0 python /tmp/probe_dream_dryrun.py

结论口径：dry-run 前后 `dream_runs` 行数不变、Qdrant 点数不变、观察数不变；唯一新增
产物是 `/app/history/dream-reports/<run_id>.json`（本探针在末尾删掉自己那份，避免污染
后续核验环境）。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

sys.path.insert(0, "/app")

from auth import create_access_token  # noqa: E402
from db import SessionLocal  # noqa: E402
from models import User  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

BASE = "http://localhost:8000"
COLL = "memories_2048"


def admin_user_id() -> str:
    """取本机第一个 admin 用户的 id（不把 id 写死进仓库）。"""
    session = SessionLocal()
    try:
        return str(session.scalar(select(User).where(User.role == "admin").order_by(User.created_at.asc())).id)
    finally:
        session.close()


def call(path: str, method: str = "GET", body=None, timeout: int = 600):
    data = json.dumps(body).encode() if body is not None else None
    token = create_access_token(admin_user_id(), "admin")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())


def point_counts():
    client = QdrantClient(url="http://qdrant:6333")
    total = client.count(collection_name=COLL, exact=True).count
    return total


def main() -> None:
    before = {
        "runs_total": call("/dream/runs?limit=1").get("total"),
        "points": point_counts(),
    }
    report = call("/dream/preview", "POST", {})
    after = {
        "runs_total": call("/dream/runs?limit=1").get("total"),
        "points": point_counts(),
    }

    report_path = report.get("report_path")
    report_body_matches = None
    if report_path and os.path.exists(report_path):
        on_disk = json.load(open(report_path, encoding="utf-8"))
        report_body_matches = on_disk == report

    out = {
        "before": before,
        "after": after,
        "runs_row_delta": (after["runs_total"] or 0) - (before["runs_total"] or 0),
        "points_delta": after["points"] - before["points"],
        "preview": {
            "mode": report.get("mode"),
            "status": report.get("status"),
            "duration_seconds": report.get("duration_seconds"),
            "observations_written": report.get("observations_written"),
            "observations_superseded": report.get("observations_superseded"),
            "would_write": len(report.get("would_write") or []),
            "candidates": len(report.get("candidates") or []),
            "errors": report.get("errors"),
            "totals": report.get("totals"),
        },
        "report_written_to": report_path,
        "report_file_equals_response_body": report_body_matches,
    }

    if report_path and os.path.exists(report_path):
        os.remove(report_path)
        out["probe_report_removed"] = not os.path.exists(report_path)

    print(json.dumps(out, ensure_ascii=False, indent=1))


main()
