"""dashboard-mechanism-visibility 设计探针 F：图派发计数器的归属实例（只读）。

复跑：
    cd /Users/xuelingkang/Documents/Containers/mem0/server
    docker compose cp ../docs/design/dashboard-mechanism-visibility-evidence/probe_graph_counters.py mem0:/tmp/
    docker compose exec -T mem0 python /tmp/probe_graph_counters.py

用途：证明 `GET /graph/stats` 的计数是**进程内累计**（归当前 mem0 容器实例），因此页面上
必须标注计数起点，否则用户会把「重启后归零」误读为数据丢失。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

sys.path.insert(0, "/app")

from auth import create_access_token  # noqa: E402
from db import SessionLocal  # noqa: E402
from models import User  # noqa: E402
from sqlalchemy import select  # noqa: E402

BASE = "http://localhost:8000"


def token() -> str:
    session = SessionLocal()
    try:
        uid = str(session.scalar(select(User).where(User.role == "admin").order_by(User.created_at.asc())).id)
    finally:
        session.close()
    return create_access_token(uid, "admin")


def stats():
    req = urllib.request.Request(
        BASE + "/graph/stats", headers={"Authorization": f"Bearer {token()}"}
    )
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode())


def pid1_started_at() -> str:
    """从 /proc 推出容器 1 号进程的启动时刻（epoch 秒）。"""
    stat = open("/proc/1/stat", encoding="utf-8").read().split()
    start_ticks = int(stat[21])
    hz = 100
    btime = None
    for line in open("/proc/stat", encoding="utf-8"):
        if line.startswith("btime "):
            btime = int(line.split()[1])
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(btime + start_ticks / hz))


def main() -> None:
    first = stats()
    time.sleep(5)
    second = stats()
    out = {
        "container_pid1_started_at": pid1_started_at(),
        "reading_1": first,
        "reading_2_after_5s": second,
        "counters_move_within_one_instance": first != second,
        "note": (
            "计数归当前 mem0 容器实例：新建的 GraphSync 在首次派发时惰性创建，计数从 0 起。"
            "本文件对应的容器实例见 container_pid1_started_at；换实例后同一读法会得到更小的数。"
        ),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))


main()
