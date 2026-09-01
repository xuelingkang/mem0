#!/usr/bin/env bash
# =============================================================================
# mem0 本地补丁应用/反打脚本
# 用于 mem0 升级后重新应用本地 patch（或反打还原）。
#
# 用法:
#   bash patches/apply-patch.sh          # 应用 patch（默认）
#   bash patches/apply-patch.sh --reverse # 反打（还原到上游）
#
# 注意：
#   - patch 文件须由 patches/generate-patch.sh 生成（mem0-local.patch）
#   - 应用后需把改动的 py 文件复制进容器 site-packages 并重启 mem0
#     （见本文件底部注释，或 generate-patch.sh 的说明）
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATCH_FILE="$REPO_DIR/patches/mem0-local.patch"

if [ ! -f "$PATCH_FILE" ]; then
  echo "❌ 找不到 $PATCH_FILE，请先运行 patches/generate-patch.sh"
  exit 1
fi

cd "$REPO_DIR"

MODE="apply"
if [ "${1:-}" = "--reverse" ]; then
  MODE="reverse"
fi

echo "==> 检测当前工作区是否已应用过 patch..."
if git apply --check "$PATCH_FILE" 2>/dev/null; then
  # patch 能干净应用 -> 尚未应用
  if [ "$MODE" = "apply" ]; then
    echo "    尚未应用，执行应用..."
    git apply "$PATCH_FILE"
    echo "✅ patch 已应用。"
  else
    echo "    patch 未应用过，无需反打。"
  fi
elif git apply --check --reverse "$PATCH_FILE" 2>/dev/null; then
  # patch 已应用 -> 可反打
  if [ "$MODE" = "apply" ]; then
    echo "    已应用过，无需重复应用。"
  else
    echo "    已应用，执行反打..."
    git apply --reverse "$PATCH_FILE"
    echo "✅ patch 已反打（还原上游）。"
  fi
else
  echo "❌ 无法干净应用/反打：工作区可能与 patch 基线不一致。"
  echo "   请手动处理（git checkout 后重打，或人工合并）。"
  echo "   涉及文件："
  git diff --name-only -- mem0/configs/prompts.py server/main.py server/routers/entities.py server/docker-compose.yaml
  exit 1
fi

echo ""
echo "======================================================================"
echo " 下一步（容器生效）："
echo "  1. 复制改动的 py 文件到容器 site-packages："
echo "     docker cp mem0/configs/prompts.py mem0-dev-mem0-1:/usr/local/lib/python3.12/site-packages/mem0/configs/prompts.py"
echo "     docker cp mem0/memory/main.py  mem0-dev-mem0-1:/usr/local/lib/python3.12/site-packages/mem0/memory/main.py"
echo "     （若升级重建了镜像，需要先重新确认 site-packages 路径）"
echo "  2. 重启："
echo "     cd server && docker compose restart mem0"
echo "======================================================================"
