#!/usr/bin/env python3
"""迁移 memories_4096 -> memories_2048：用新 embedder (qwen3.7-text-embedding) 重 embedding 到 2048 维。

在 mem0 容器内执行（挂载 server/ 到 /app，读容器 env 的 EMBEDDER_* 配置）。
"""
import os
import time

from qdrant_client.http.models import PayloadSchemaType

from mem0.configs.embeddings.base import BaseEmbedderConfig
from mem0.embeddings.openai import OpenAIEmbedding
from mem0.vector_stores.qdrant import Qdrant

OLD_COL = "memories_4096"
NEW_COL = "memories_2048"
TARGET_DIMS = 2048
BATCH = 20  # 阿里云 MaaS 限制单次 <=20 条输入

emb = OpenAIEmbedding(
    BaseEmbedderConfig(
        model=os.environ.get("MEM0_DEFAULT_EMBEDDER_MODEL"),
        api_key=os.environ.get("EMBEDDER_API_KEY"),
        openai_base_url=os.environ.get("EMBEDDER_BASE_URL"),
        embedding_dims=TARGET_DIMS,
    )
)
print(f"embedder: model={emb.config.model} dims={TARGET_DIMS}", flush=True)

store = Qdrant(host="qdrant", port=6333, collection_name=NEW_COL, embedding_model_dims=TARGET_DIMS)
client = store.client
store.create_col(TARGET_DIMS, on_disk=False)
print(f"collection {NEW_COL} ensured", flush=True)

# created_at datetime 索引（游标翻页依赖）
try:
    client.create_payload_index(
        collection_name=NEW_COL,
        field_name="created_at",
        field_schema=PayloadSchemaType.DATETIME,
    )
    print("created_at index ensured", flush=True)
except Exception as e:
    print(f"created_at index (may already exist): {e}", flush=True)

# scroll 旧集合全量
points = []
offset = None
while True:
    res = client.scroll(collection_name=OLD_COL, limit=500, offset=offset, with_payload=True)
    pts, nxt = res[0], res[1]
    points.extend(pts)
    print(f"scrolled {len(points)}", flush=True)
    if nxt is None:
        break
    offset = nxt

print(f"total old points: {len(points)}", flush=True)

# 逐批重 embedding + upsert（payload 原样保留，bm25 由 store.insert 自动重算）
for i in range(0, len(points), BATCH):
    chunk = points[i : i + BATCH]
    texts = [p.payload.get("data") or p.payload.get("text_lemmatized") or "" for p in chunk]
    vecs = emb.embed_batch(texts)
    payloads = [p.payload for p in chunk]
    ids = [p.id for p in chunk]
    store.insert(vecs, payloads, ids)
    print(f"inserted {min(i + BATCH, len(points))}/{len(points)}", flush=True)

info = client.get_collection(NEW_COL)
print(
    f"done: {NEW_COL} points={info.points_count} size={info.config.params.vectors.size} "
    f"status={info.status}",
    flush=True,
)
