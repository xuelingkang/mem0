#!/usr/bin/env bash
# =============================================================================
# mem0 self-hosted 本地补丁生成脚本
# 生成一份独立 patch 文件，用于 mem0 升级后重新应用所有本地改动。
#
# 适用：~/Documents/Containers/mem0（mem0ai/mem0 仓库，自托管部署）
# 产物：~/Documents/Containers/mem0/patches/mem0-local.patch
#
# 【基线语义】本地改动以 git submit 形式保存在 main 分支上（未提交的工作区 diff
# 属于旧工作流，已废弃）。patch 由「upstream/main..HEAD」这一提交区间导出，
# 因此每次同步上游（git rebase upstream/main）后，本脚本导出的就是当前真实
# 的本地改动全集，而非某一时点的快照。
#
# 【patch 用途】兜底恢复。正常路径是 git 历史本身承载改动；只有当你把工作区
# reset 到纯净上游、需要找回本地改动时，才用 apply-patch.sh 重打。
#
# 【本地改动涵盖】运行时代码 + dashboard 前端 + 补丁脚本：
#   - mem0/configs/prompts.py      : ADDITIVE_EXTRACTION_PROMPT 中文化
#   - mem0/memory/main.py          : 认知覆盖（UPDATE/DELETE 提炼）+ UPDATE_MIN_SCORE
#   - mem0/vector_stores/qdrant.py : list() 游标分页
#   - server/main.py               : scroll() tuple 兼容 + GET /memories 时间倒序游标
#   - server/routers/entities.py   : scroll() tuple 兼容（Entities 页空 bug）
#   - server/docker-compose.yaml   : qdrant 服务、LLLL/embedder 独立 provider、去 --reload
#   - server/migrate_to_2048.py    : 2048 维迁移脚本（模板）
#   - server/dashboard/src/**      : 游标翻页 + 语义搜索 + 移除 1000 硬顶/广告条
#
# 【注意】patches/ 目录自身被排除，避免 patch 自引用（每次生成内容都不同）。
#
# 【升级后重打流程】
#   1. git fetch upstream && git rebase upstream/main
#   2. bash patches/apply-patch.sh
#   3. 生效（三者语义不同，别混）：
#      - 改 server/*.py 或 mem0/**.py  → cd server && docker compose restart mem0
#        （源码经 volume 挂载，容器启动时 cp 进 site-packages，无需 docker cp、无需 rebuild）
#      - 改 server/dashboard/**        → docker compose build mem0-dashboard
#                                        && docker compose up -d --no-deps --force-recreate mem0-dashboard
#      - 改 server/.env                → docker compose up -d --force-recreate mem0
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATCH_FILE="$REPO_DIR/patches/mem0-local.patch"

# 基线：上游远程跟踪分支（可用 BASE=... 覆盖）
BASE="${BASE:-upstream/main}"

cd "$REPO_DIR"

if ! git rev-parse --verify --quiet "$BASE" >/dev/null; then
  echo "❌ 找不到基线 '$BASE'。先执行：git fetch upstream"
  exit 1
fi

# 排除 patches/ 自身，避免 patch 自引用
EXCLUDE=':(exclude)patches/'

if [ -z "$(git diff --name-only "$BASE..HEAD" -- . "$EXCLUDE")" ]; then
  echo "❌ 相对 $BASE 没有任何本地改动（HEAD 可能就在基线上）"
  exit 1
fi

git diff "$BASE..HEAD" -- . "$EXCLUDE" > "$PATCH_FILE"

echo "✅ patch 已生成: $PATCH_FILE"
echo "   基线: $BASE ($(git rev-parse --short "$BASE"))"
echo "   HEAD: $(git rev-parse --short HEAD)"
echo "   大小: $(wc -c < "$PATCH_FILE") bytes"
echo "   改动文件:"
git diff --name-only "$BASE..HEAD" -- . "$EXCLUDE" | sed 's/^/     /'
