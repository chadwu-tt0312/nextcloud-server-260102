#!/usr/bin/env bash
#
# Docker：先列出所有外部儲存，再逐筆更新 mount_point
#
# 用法（repo 根目錄）:
#   bash docker/test/rename-mount-points-docker.sh
#   bash docker/test/rename-mount-points-docker.sh -c chad-nextcloud-1 --limit 10 --apply
#   bash docker/test/rename-mount-points-docker.sh -c chad-nextcloud-1 --apply
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_rename-mount-points-common.sh
source "${SCRIPT_DIR}/_rename-mount-points-common.sh"

CONTAINER=""
EXPORT_FILE_ARG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -c|--container) CONTAINER="$2"; shift 2 ;;
        --apply) APPLY=true; shift ;;
        --limit) LIMIT="$2"; shift 2 ;;
        --list-only) LIST_ONLY=true; shift ;;
        --skip-list) SKIP_LIST=true; shift ;;
        --export-file) EXPORT_FILE_ARG="$2"; SKIP_LIST=true; shift 2 ;;
        --log-dir) LOG_DIR="$2"; shift 2 ;;
        -h|--help) rename_mount_points_usage; exit 0 ;;
        *) echo "未知參數: $1" >&2; rename_mount_points_usage >&2; exit 1 ;;
    esac
done

[[ -z "$LOG_DIR" ]] && LOG_DIR="${SCRIPT_DIR}/logs"

if [[ -z "$CONTAINER" ]]; then
    CONTAINER="$(docker ps --format '{{.Names}}' | grep -i nextcloud | grep -vi db | head -n 1 || true)"
fi
if [[ -z "$CONTAINER" ]]; then
    echo "❌ 找不到 Nextcloud 容器，請用 -c 指定" >&2
    exit 1
fi

if ! docker inspect "$CONTAINER" &>/dev/null; then
    echo "❌ 容器不存在: ${CONTAINER}" >&2
    exit 1
fi

NC_TARGET_LABEL="docker:${CONTAINER}"
NC_OCC_CMD=(docker exec -u www-data "$CONTAINER" php occ)
RENAME_EXPORT_FILE="$EXPORT_FILE_ARG"

rename_mount_points_run
