"""图库规模内存模型实测：按「3500 条事实 → 图节点/边」的换算规模直接压 FalkorDB。

不调用 LLM，纯 Cypher 建图，测的是图库自身在目标规模下的内存占用。
规模换算：3545 条事实 × (2.3 实体/条) ≈ 8150 实体节点；边 = 事实数（1 条事实 ↔ 1 条边）。
基线换算：实测 5 条 episode 产出 11 实体 + 8 边 + 5 episode 节点。
"""

import socket
import time


def cmd(*args, host="127.0.0.1", port=16379):
    s = socket.create_connection((host, port), timeout=120)
    payload = f"*{len(args)}\r\n" + "".join(
        f"${len(str(a).encode())}\r\n{a}\r\n" for a in args
    )
    s.sendall(payload.encode())
    buf = b""
    s.settimeout(120)
    while True:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        if buf.endswith(b"\r\n") and (buf.count(b"\r\n") > 1):
            break
    s.close()
    return buf[:400]


GRAPH = "gm-scale-probe"
N_ENT = 8150
N_EDGE = 3545


def info_mem():
    raw = cmd("INFO", "memory").decode("utf-8", "replace")
    out = {}
    for line in raw.splitlines():
        if ":" in line and ("used_memory" in line or "allocator" in line):
            k, v = line.split(":", 1)
            out[k] = v.strip()
    return out


print("=== baseline ===")
print(cmd("GRAPH.DELETE", GRAPH).decode("utf-8", "replace")[:80].strip())
time.sleep(1)
print(info_mem())

t0 = time.perf_counter()
cmd(
    "GRAPH.QUERY",
    GRAPH,
    f"""
    UNWIND range(1, {N_ENT}) AS i
    CREATE (n:Entity {{uuid: 'ent-' + toString(i), name: 'entity-' + toString(i),
            group_id: 'gm-scale-probe', summary: '这是一段用于规模压测的实体摘要文本，长度接近真实抽取结果。',
            created_at: '2026-09-17T07:00:00Z'}})
    """,
)
print(f"[nodes] {time.perf_counter() - t0:.1f}s")
print(info_mem())

t0 = time.perf_counter()
cmd(
    "GRAPH.QUERY",
    GRAPH,
    f"""
    MATCH (a:Entity), (b:Entity)
    WHERE a.uuid <> b.uuid
    WITH a, b LIMIT {N_EDGE}
    CREATE (a)-[e:RELATES_TO {{uuid: 'edge-' + a.uuid, group_id: 'gm-scale-probe',
            name: 'RELATES_TO', fact: '一段接近真实长度的事实陈述文本，用于规模压测。',
            created_at: '2026-09-17T07:00:00Z'}}]->(b)
    """,
)
print(f"[edges] {time.perf_counter() - t0:.1f}s")
print(info_mem())

print("=== graph stats ===")
print(cmd("GRAPH.QUERY", GRAPH, "MATCH (n) RETURN count(n)").decode("utf-8", "replace")[:200])
print(cmd("GRAPH.QUERY", GRAPH, "MATCH ()-[e]->() RETURN count(e)").decode("utf-8", "replace")[:200])
print("=== after (with graph resident) ===")
print(info_mem())
