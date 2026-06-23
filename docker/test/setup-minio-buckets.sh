#!/usr/bin/env bash
#
# 在本機測試環境建立 MinIO bucket 並放入驗證用檔案
#
# 前置：minio-docker-compose-v2.yaml 已啟動，且 minio1 容器在 n8n-network
#
# 用法:
#   ./setup-minio-buckets.sh
#   ./setup-minio-buckets.sh --minio-container minio-proxy-minio1-1
#   ./setup-minio-buckets.sh --users chad
#

set -euo pipefail

MINIO_CONTAINER=""
MINIO_USER="${MINIO_ROOT_USER:-minioadmin}"
MINIO_PASS="${MINIO_ROOT_PASSWORD:-minioadminpw}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
USER_BUCKETS=(chad usr01 00059094)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --minio-container|-m)
            MINIO_CONTAINER="$2"
            shift 2
            ;;
        --users|-u)
            IFS=',' read -r -a USER_BUCKETS <<< "$2"
            shift 2
            ;;
        --help|-h)
            echo "用法: $0 [--minio-container NAME] [--users chad,usr01]"
            exit 0
            ;;
        *)
            echo "未知參數: $1" >&2
            exit 1
            ;;
    esac
done

if [[ -z "$MINIO_CONTAINER" ]]; then
    MINIO_CONTAINER="$(docker ps --format '{{.Names}}' | grep -E 'minio1' | head -n 1 || true)"
fi

if [[ -z "$MINIO_CONTAINER" ]]; then
    echo "❌ 找不到 minio1 容器，請用 --minio-container 指定" >&2
    exit 1
fi

echo "MinIO 容器: ${MINIO_CONTAINER}"
echo "建立 bucket 並上傳驗證檔..."

docker exec "${MINIO_CONTAINER}" sh -c "
set -e
mc alias set local '${MINIO_ENDPOINT}' '${MINIO_USER}' '${MINIO_PASS}' >/dev/null
"

for user in "${USER_BUCKETS[@]}"; do
    bucket="${user}-filespace"
    echo "  → ${bucket}"

    docker exec "${MINIO_CONTAINER}" sh -c "
set -e
mc mb --ignore-existing local/${bucket}
echo 'Hello from MinIO - ${user}' | mc pipe local/${bucket}/welcome.txt
mc ls local/${bucket}/
"
done

echo ""
echo "✅ MinIO bucket 建立完成"
echo "   Console: http://localhost:9001  (${MINIO_USER} / ${MINIO_PASS})"
