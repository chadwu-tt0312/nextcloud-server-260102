#!/usr/bin/env bash
#
# K8s：先列出所有外部儲存，再逐筆更新 mount_point
#
# 用法（repo 根目錄）:
#   bash docker/test/rename-mount-points-k8s.sh
#   bash docker/test/rename-mount-points-k8s.sh --limit 10 --apply
#   bash docker/test/rename-mount-points-k8s.sh -n nextcloud -p nextcloud-xxx --apply
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_rename-mount-points-common.sh
source "${SCRIPT_DIR}/_rename-mount-points-common.sh"

NAMESPACE="nextcloud"
POD=""
LABEL_SELECTOR="app=nextcloud"
EXPORT_FILE_ARG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--namespace) NAMESPACE="$2"; shift 2 ;;
        -p|--pod) POD="$2"; shift 2 ;;
        -l|--label-selector) LABEL_SELECTOR="$2"; shift 2 ;;
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

if ! command -v kubectl &>/dev/null; then
    echo "❌ 找不到 kubectl" >&2
    exit 1
fi

if [[ -z "$POD" ]]; then
    POD="$(kubectl get pods -n "$NAMESPACE" -l "$LABEL_SELECTOR" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    if [[ -z "$POD" ]]; then
        POD="$(kubectl get pods -n "$NAMESPACE" \
            -o jsonpath='{.items[?(@.metadata.name=~"nextcloud.*")].metadata.name}' \
            2>/dev/null | awk '{print $1}' || true)"
    fi
fi
if [[ -z "$POD" ]]; then
    echo "❌ 在 namespace ${NAMESPACE} 找不到 Nextcloud Pod，請用 -p 指定" >&2
    exit 1
fi

PHASE="$(kubectl get pod "$POD" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
if [[ "$PHASE" != "Running" ]]; then
    echo "❌ Pod ${POD} 未運行（狀態: ${PHASE:-未知}）" >&2
    exit 1
fi

NC_TARGET_LABEL="k8s:${NAMESPACE}/${POD}"

# kubectl exec 無 -u；以 su 執行 occ（與 k8s_helm/nextcloud README 一致）
nc_occ() {
    local inner
    inner=$(printf '%q ' php /var/www/html/occ "$@")
    kubectl exec -n "$NAMESPACE" "$POD" -- su -s /bin/sh www-data -c "$inner"
}

RENAME_EXPORT_FILE="$EXPORT_FILE_ARG"

rename_mount_points_run
