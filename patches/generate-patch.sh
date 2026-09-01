#!/usr/bin/env bash
# =============================================================================
# mem0 self-hosted 本地补丁生成脚本
# 生成一份独立 patch 文件，用于 mem0 升级后重新应用所有本地改动。
#
# 适用：~/Documents/Containers/mem0（mem0ai/mem0 仓库，自托管部署）
# 产物：~/Documents/Containers/mem0/patches/mem0-local.patch
#
# 背景：本地对 mem0 做了 4 处改动，均未提交（保持上游状态、本地补丁最小化）。
#   - mem0/configs/prompts.py      : ADDITIVE_EXTRACTION_PROMPT 全中文翻译（治语言问题）
#   - server/main.py               : Qdrant scroll() 返回 tuple 兼容 + GET /memories 时间倒序
#   - server/routers/entities.py   : Qdrant scroll() 返回 tuple 兼容（Entities 页空 bug）
#   - server/docker-compose.yaml   : 新增 qdrant 服务（pgvector -> qdrant 迁移）
#
# 升级后重打 patch 流程：
#   1. git pull / 更新到新版 mem0
#   2. bash patches/apply-patch.sh   （自动尝试反打，失败则提示手动处理）
#   3. 检查 container：需要把改动的两个 py 文件复制进容器 site-packages：
#        docker cp mem0/configs/prompts.py mem0-dev-mem0-1:/usr/local/lib/python3.12/site-packages/mem0/configs/prompts.py
#        docker cp mem0/memory/main.py  mem0-dev-mem0-1:/usr/local/lib/python3.12/site-packages/mem0/memory/main.py
#   4. docker compose -f server/docker-compose.yaml restart mem0
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATCH_DIR="$REPO_DIR/patches"
PATCH_FILE="$PATCH_DIR/mem0-local.patch"
mkdir -p "$PATCH_DIR"

cd "$REPO_DIR"

# 只包含我们关心的 4 个文件（按路径排序，避免 docker-compose.yaml 的 qdrant 块被误排除）
git diff -- \
  mem0/configs/prompts.py \
  server/main.py \
  server/routers/entities.py \
  server/docker-compose.yaml \
  > "$PATCH_FILE"

if [ ! -s "$PATCH_FILE" ]; then
  echo "❌ 没有任何改动，patch 为空（可能已提交）"
  exit 1
fi

echo "✅ patch 已生成: $PATCH_FILE"
echo "   大小: $(wc -c < "$PATCH_FILE") bytes"
echo "   改动文件:"
git diff --name-only -- \
  mem0/configs/prompts.py \
  server/main.py \
  server/routers/entities.py \
  server/docker-compose.yaml
