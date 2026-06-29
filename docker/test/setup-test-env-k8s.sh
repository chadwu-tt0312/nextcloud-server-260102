#!/usr/bin/env bash
#
# 一鍵設定本機測試環境：MinIO bucket + Nextcloud 外部儲存掛載
#
# 目標：讓 chad / usr01 / 00059094 在「所有檔案」看到「個人雲端硬碟」
#
# 前置：
#   1. docker network create n8n-network   （若尚未建立）
#   2. minio-proxy/docker/minio-docker-compose-v2.yaml 已啟動
#   3. docker/nextcloud-docker-compose.yaml 已啟動
#   4. Nextcloud 已有使用者 chad、usr01、00059094
#
# 用法（在 repo 根目錄或 docker/test 目錄）:
#   bash docker/test/setup-test-env.sh
#   bash docker/test/setup-test-env.sh --dry-run
#   bash docker/test/setup-test-env.sh --users chad
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

MINIO_CONTAINER=""
NC_CONTAINER=""
DRY_RUN=false
AUTO_YES=false
USERS_ARG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --minio-container|-m) MINIO_CONTAINER="$2"; shift 2 ;;
        --nextcloud-container|-c) NC_CONTAINER="$2"; shift 2 ;;
        --users|-u) USERS_ARG="$2"; shift 2 ;;
        --dry-run|-d) DRY_RUN=true; shift ;;
        --yes|-y) AUTO_YES=true; shift ;;
        --help|-h)
            echo "用法: $0 [--dry-run] [--users chad] [-c NC_CONTAINER] [-m MINIO_CONTAINER]"
            exit 0
            ;;
        *) echo "未知參數: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$NC_CONTAINER" ]]; then
#    NC_CONTAINER="$(docker ps --format '{{.Names}}' | grep -i nextcloud | grep -v db | head -n 1 || true)"
    NC_CONTAINER=$(kubectl get pod -n nextcloud -l app=nextcloud -o jsonpath='{.items[0].metadata.name}')
fi

if [[ -z "$NC_CONTAINER" ]]; then
    echo "❌ 找不到 Nextcloud 容器，請用 --nextcloud-container 指定" >&2
    exit 1
fi

MOUNTS_FILE="${SCRIPT_DIR}/mounts-local-test.json"
if [[ -n "$USERS_ARG" && "$USERS_ARG" == "chad" ]]; then
#    MOUNTS_FILE="${SCRIPT_DIR}/mounts-local-test-chad.json"
#    MOUNTS_FILE="${SCRIPT_DIR}/mounts-local-test-smg_arc304-dep.json"
    MOUNTS_FILE="${SCRIPT_DIR}/mounts-local-test-smg_arc304.json"
fi
SYNC_SCRIPT="${REPO_ROOT}/tools/sync_external_storage.py"

echo "========================================"
echo " Nextcloud + MinIO 測試環境設定"
echo "========================================"
echo "Nextcloud 容器: ${NC_CONTAINER}"
echo "掛載設定檔:     ${MOUNTS_FILE}"
echo ""

# ---- 1. MinIO buckets ----
BUCKET_ARGS=()
if [[ -n "$USERS_ARG" ]]; then
    BUCKET_ARGS+=(--users "$USERS_ARG")
fi
if [[ -n "$MINIO_CONTAINER" ]]; then
    BUCKET_ARGS+=(--minio-container "$MINIO_CONTAINER")
fi

# 260625: 廠內不要建立 bucket
# echo "[1/4] 建立 MinIO bucket..."
# bash "${SCRIPT_DIR}/setup-minio-buckets.sh" "${BUCKET_ARGS[@]}"

# 260625: 廠內不能用 docker，不額外做啟用行為 
# ---- 2. 啟用 files_external ----
# echo ""
# echo "[2/4] 啟用 files_external app..."
# if [[ "$DRY_RUN" == true ]]; then
# #    echo "  (dry-run) docker exec -u www-data ${NC_CONTAINER} php occ app:enable files_external"
#     echo "  (dry-run) kubectl exec -n nextcloud -it ${NC_CONTAINER} -- php /var/www/html/occ app:list"
# else
# #    docker exec -u www-data "${NC_CONTAINER}" php occ app:enable files_external --force || true
#     kubectl exec -n nextcloud -it "${NC_CONTAINER}" -- php /var/www/html/occ app:list
# fi

# ---- 3. 匯入外部儲存 ----
echo ""
echo "[3/4] 同步外部儲存掛載（upsert）..."

if [[ ! -f "$SYNC_SCRIPT" ]]; then
    echo "❌ 找不到 ${SYNC_SCRIPT}" >&2
    exit 1
fi

SYNC_ARGS=(python3 "$SYNC_SCRIPT" "$MOUNTS_FILE" --runtime k8s --namespace nextcloud)
if [[ -n "$NC_CONTAINER" ]]; then
    SYNC_ARGS+=(--pod "$NC_CONTAINER")
fi
if [[ "$DRY_RUN" == true ]]; then
    SYNC_ARGS+=(--dry-run)
fi

echo "PYTHONPATH=${REPO_ROOT}/tools ${SYNC_ARGS[*]}"
PYTHONPATH="${REPO_ROOT}/tools" "${SYNC_ARGS[@]}"

# 260625-01: 廠內不能用 docker 
# 260625-02: 廠內外部儲存數量太多
# ---- 4. 驗證掛載狀態 ----
echo ""
echo "[4/4] 列出外部儲存..."
# docker exec -u www-data "${NC_CONTAINER}" php occ files_external:list || true
# kubectl exec -n nextcloud -it "${NC_CONTAINER}" -- php /var/www/html/occ files_external:list || true

echo ""
echo "========================================"
echo " 完成"
echo "========================================"
echo ""
echo "驗證步驟："
echo "  1. 開啟 http://localhost:8085"
echo "  2. 以 chad 登入"
echo "  3. 檔案 → 所有檔案，應看到「個人雲端硬碟」"
echo "  4. 進入後應有 welcome.txt"
echo ""
echo "若掛載狀態為 invalid，在容器內檢查連線："
echo "  docker exec -u www-data ${NC_CONTAINER} php occ files_external:verify 1"
