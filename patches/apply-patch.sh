#!/usr/bin/env bash
# =============================================================================
# mem0 本地补丁应用/反打脚本
# 用于 mem0 升级后重新应用本地 patch（或反打还原）。
#
# 用法:
#   bash patches/apply-patch.sh          # 应用 patch（默认）
#   bash patches/apply-patch.sh --reverse # 反打（还原到上游）
#
# 注意:
#   - patch 文件须由 patches/generate-patch.sh 生成（mem0-local.patch）
#   - patch 由「upstream/main..HEAD」提交区间导出，覆盖运行时代码 + dashboard 前端
#   - 应用后生效路径见本文件底部（三种改动语义不同，别混）
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
  echo "   两条出路："
  echo "     a) 先对齐基线：git fetch upstream && git rebase upstream/main，再重跑本脚本"
  echo "     b) 重新导出：bash patches/generate-patch.sh（会以当前 HEAD 为准覆盖 patch）"
  echo "   涉及文件:"
  grep '^diff --git' "$PATCH_FILE" | sed 's|diff --git a/||;s| b/.*||' | sed 's/^/     /'
  exit 1
fi

echo ""
echo "======================================================================"
echo " 下一步（容器生效）——三种改动语义不同，按需执行："
echo "   1) 改 server/*.py 或 mem0/**.py:"
echo "        cd server && docker compose restart mem0"
echo "      （源码经 volume 挂载，容器启动时 cp 进 site-packages；"
echo "        无需 docker cp、无需 rebuild）"
echo "   2) 改 server/dashboard/**:"
echo "        cd server && docker compose build mem0-dashboard \\"
echo "          && docker compose up -d --no-deps --force-recreate mem0-dashboard"
echo "      （dashboard 是 build 型镜像，改源码必须重建）"
echo "   3) 改 server/.env:"
echo "        cd server && docker compose up -d --force-recreate mem0"
echo "      （env 在容器创建时固化，restart 不够）"
echo "======================================================================"
