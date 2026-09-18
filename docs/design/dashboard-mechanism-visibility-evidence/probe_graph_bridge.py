"""dashboard-mechanism-visibility 设计探针 C：图桥侧只读面与「读即建键」副作用（读多写一）。

复跑：
    cd /Users/xuelingkang/Documents/Containers/mem0/server
    docker compose exec -T graph-bridge python - <<'PY'   # 或 cp 进容器执行本文件
    ...

本探针刻意包含一次**删除**：它先证明「对未知 group_id 读 /stats 会新建空图键」，再把该
探针键删掉（键名带 probe 前缀，非业务键）。business 键 `mem0_xue` 只读不删。
"""

from __future__ import annotations

import json
import urllib.request

BRIDGE = "http://localhost:8000"
PROBE_KEY = "mem0_dashboard_probe_key"


def get(path: str):
    return json.loads(urllib.request.urlopen(BRIDGE + path, timeout=60).read().decode())


def delete(path: str):
    req = urllib.request.Request(BRIDGE + path, method="DELETE")
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode())


def main() -> None:
    out = {}
    out["health"] = get("/health")
    before = get("/graphs")["graphs"]
    out["graphs_before"] = before

    out["stats_mem0_xue"] = get("/stats?group_id=mem0_xue")

    # 副作用验证：对未知 group_id 读 /stats
    probe_stats = get(f"/stats?group_id={PROBE_KEY}")
    after_read = get("/graphs")["graphs"]
    out["probe_key_stats_reading"] = probe_stats
    out["graphs_after_reading_unknown_key"] = after_read
    out["read_created_a_new_key"] = sorted(set(after_read) - set(before))

    out["mem0_xue_after_probe"] = get("/stats?group_id=mem0_xue")

    # 清掉探针键，恢复原状
    out["probe_key_deleted"] = delete(f"/graph/{PROBE_KEY}")
    out["graphs_after_cleanup"] = get("/graphs")["graphs"]
    out["cleanup_restored_original_key_set"] = sorted(out["graphs_after_cleanup"]) == sorted(before)

    print(json.dumps(out, ensure_ascii=False, indent=1))


main()
