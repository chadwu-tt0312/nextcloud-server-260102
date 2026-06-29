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
#   bash docker/test/setup-test-env.sh --mount-point-style account   # 每人帳號資料夾 /chad
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TOOLS_RUN="${REPO_ROOT}/tools/run_python.sh"

MINIO_CONTAINER=""
NC_CONTAINER=""
DRY_RUN=false
AUTO_YES=false
USERS_ARG=""
MOUNT_POINT_STYLE="display"
GENERATED_MOUNTS_FILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --minio-container|-m) MINIO_CONTAINER="$2"; shift 2 ;;
        --nextcloud-container|-c) NC_CONTAINER="$2"; shift 2 ;;
        --users|-u) USERS_ARG="$2"; shift 2 ;;
        --mount-point-style) MOUNT_POINT_STYLE="$2"; shift 2 ;;
        --dry-run|-d) DRY_RUN=true; shift ;;
        --yes|-y) AUTO_YES=true; shift ;;
        --help|-h)
            echo "用法: $0 [--dry-run] [--users chad] [--mount-point-style display|account] [-c NC_CONTAINER] [-m MINIO_CONTAINER]"
            echo ""
            echo "  --mount-point-style display  掛載點為 /個人雲端硬碟（預設，原行為）"
            echo "  --mount-point-style account  掛載點為每人帳號，例如 /chad"
            exit 0
            ;;
        *) echo "未知參數: $1" >&2; exit 1 ;;
    esac
done

if [[ "$MOUNT_POINT_STYLE" != "display" && "$MOUNT_POINT_STYLE" != "account" ]]; then
    echo "❌ --mount-point-style 僅支援 display 或 account" >&2
    exit 1
fi

if [[ -z "$NC_CONTAINER" ]]; then
    NC_CONTAINER="$(docker ps --format '{{.Names}}' | grep -i nextcloud | grep -v db | head -n 1 || true)"
fi

if [[ -z "$NC_CONTAINER" ]]; then
    echo "❌ 找不到 Nextcloud 容器，請用 --nextcloud-container 指定" >&2
    exit 1
fi

MOUNTS_FILE="${SCRIPT_DIR}/mounts-local-test.json"
GEN_MOUNTS_SCRIPT="${REPO_ROOT}/tools/gen_mounts_local_test.py"
GENERATED_MOUNTS_FILE=""

if [[ "$MOUNT_POINT_STYLE" == "account" ]]; then
    GEN_USERS="${USERS_ARG:-chad,usr01,00059094}"
    GENERATED_MOUNTS_FILE="$(mktemp "${TMPDIR:-/tmp}/mounts-local-test-XXXX.json")"
    bash "${TOOLS_RUN}" "${GEN_MOUNTS_SCRIPT}" \
        --users "${GEN_USERS}" \
        --style account \
        -o "${GENERATED_MOUNTS_FILE}"
    MOUNTS_FILE="${GENERATED_MOUNTS_FILE}"
elif [[ -n "$USERS_ARG" && "$USERS_ARG" == "chad" ]]; then
    MOUNTS_FILE="${SCRIPT_DIR}/mounts-local-test-chad.json"
elif [[ -n "$USERS_ARG" ]]; then
    GENERATED_MOUNTS_FILE="$(mktemp "${TMPDIR:-/tmp}/mounts-local-test-XXXX.json")"
    bash "${TOOLS_RUN}" "${GEN_MOUNTS_SCRIPT}" \
        --users "${USERS_ARG}" \
        --style display \
        -o "${GENERATED_MOUNTS_FILE}"
    MOUNTS_FILE="${GENERATED_MOUNTS_FILE}"
fi

cleanup() {
    if [[ -n "${GENERATED_MOUNTS_FILE}" && -f "${GENERATED_MOUNTS_FILE}" ]]; then
        rm -f "${GENERATED_MOUNTS_FILE}"
    fi
}
trap cleanup EXIT

SYNC_SCRIPT="${REPO_ROOT}/tools/sync_external_storage.py"

echo "========================================"
echo " Nextcloud + MinIO 測試環境設定"
echo "========================================"
echo "Nextcloud 容器: ${NC_CONTAINER}"
echo "掛載設定檔:     ${MOUNTS_FILE}"
echo "mount_point:    ${MOUNT_POINT_STYLE} ($([[ "$MOUNT_POINT_STYLE" == "account" ]] && echo 'bucket 去掉 -filespace' || echo '/個人雲端硬碟'))"
echo ""

# ---- 1. MinIO buckets ----
BUCKET_ARGS=()
if [[ -n "$USERS_ARG" ]]; then
    BUCKET_ARGS+=(--users "$USERS_ARG")
fi
if [[ -n "$MINIO_CONTAINER" ]]; then
    BUCKET_ARGS+=(--minio-container "$MINIO_CONTAINER")
fi

echo "[1/4] 建立 MinIO bucket..."
bash "${SCRIPT_DIR}/setup-minio-buckets.sh" "${BUCKET_ARGS[@]}"

# ---- 2. 啟用 files_external ----
echo ""
echo "[2/4] 啟用 files_external app..."
if [[ "$DRY_RUN" == true ]]; then
    echo "  (dry-run) docker exec -u www-data ${NC_CONTAINER} php occ app:enable files_external"
else
    docker exec -u www-data "${NC_CONTAINER}" php occ app:enable files_external --force || true
fi

# ---- 3. 匯入外部儲存 ----
echo ""
echo "[3/4] 同步外部儲存掛載（upsert）..."

if [[ ! -f "$SYNC_SCRIPT" ]]; then
    echo "❌ 找不到 ${SYNC_SCRIPT}" >&2
    exit 1
fi

SYNC_ARGS=("$SYNC_SCRIPT" "$MOUNTS_FILE" --runtime docker --container "$NC_CONTAINER")
if [[ "$MOUNT_POINT_STYLE" == "account" ]]; then
    SYNC_ARGS+=(--mount-point-style account)
fi
if [[ "$DRY_RUN" == true ]]; then
    SYNC_ARGS+=(--dry-run)
fi

bash "${TOOLS_RUN}" "${SYNC_ARGS[@]}"

# ---- 4. 驗證掛載狀態 ----
echo ""
echo "[4/4] 列出外部儲存..."
docker exec -u www-data "${NC_CONTAINER}" php occ files_external:list || true

echo ""
echo "========================================"
echo " 完成"
echo "========================================"
echo ""
echo "驗證步驟："
echo "  1. 開啟 http://localhost:8085"
echo "  2. 以 chad 登入"
echo "  3. 檔案 → 所有檔案（display: 個人雲端硬碟；account: bucket 名稱如 /chad 或 /00073839）"
echo "  4. 進入後應有 welcome.txt"
echo ""
echo "若掛載狀態為 invalid，在容器內檢查連線："
echo "  docker exec -u www-data ${NC_CONTAINER} php occ files_external:verify 1"
