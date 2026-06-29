#!/bin/bash
#
# Nextcloud External Storage Import Script (Docker)
#
# 在 Docker 環境中匯入 External Storage 掛載設定
#
# 用法:
#   ./import_external_storage_docker.sh mounts.json
#   ./import_external_storage_docker.sh mounts.json --container nextcloud
#   ./import_external_storage_docker.sh mounts.json --dry-run
#

set -euo pipefail

# 預設值
CONTAINER_NAME=""
MOUNTS_FILE=""
DRY_RUN=false
AUTO_CONFIRM=false
UPSERT=false

# 顏色輸出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 解析參數
while [[ $# -gt 0 ]]; do
    case $1 in
        --container|-c)
            CONTAINER_NAME="$2"
            shift 2
            ;;
        --dry-run|-d)
            DRY_RUN=true
            shift
            ;;
        --yes|-y)
            AUTO_CONFIRM=true
            shift
            ;;
        --upsert|-U)
            UPSERT=true
            shift
            ;;
        --help|-h)
            echo "用法: $0 [OPTIONS] <mounts.json>"
            echo ""
            echo "選項:"
            echo "  -c, --container NAME    指定容器名稱（預設：自動偵測）"
            echo "  -d, --dry-run           僅預覽，不實際匯入"
            echo "  -y, --yes               自動確認，不詢問"
            echo "  -U, --upsert            更新已存在掛載並新增缺少項目（建議用於重複執行）"
            echo "  -h, --help              顯示此說明"
            echo ""
            echo "範例:"
            echo "  $0 mounts.json"
            echo "  $0 mounts.json --container nextcloud-app-1"
            echo "  $0 mounts.json --dry-run"
            exit 0
            ;;
        --upsert|-U)
            UPSERT=true
            shift
            ;;
        *)
            if [[ "$1" == --* || "$1" == -* ]]; then
                echo -e "${RED}❌ 錯誤：未知參數 $1${NC}" >&2
                exit 1
            fi
            if [[ -z "$MOUNTS_FILE" ]]; then
                MOUNTS_FILE="$1"
            else
                echo -e "${RED}❌ 錯誤：只能指定一個 mounts.json 檔案${NC}" >&2
                exit 1
            fi
            shift
            ;;
    esac
done

# 檢查必要參數
if [[ -z "$MOUNTS_FILE" ]]; then
    echo -e "${RED}❌ 錯誤：請指定 mounts.json 檔案路徑${NC}" >&2
    echo "使用 --help 查看使用說明"
    exit 1
fi

if [[ ! -f "$MOUNTS_FILE" ]]; then
    echo -e "${RED}❌ 錯誤：檔案不存在: $MOUNTS_FILE${NC}" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC_SCRIPT="${SCRIPT_DIR}/sync_external_storage.py"

if [[ "$UPSERT" == true ]]; then
    if [[ ! -f "$SYNC_SCRIPT" ]]; then
        echo -e "${RED}❌ 找不到 ${SYNC_SCRIPT}${NC}" >&2
        exit 1
    fi
    SYNC_ARGS=(python3 "$SYNC_SCRIPT" "$MOUNTS_FILE" --runtime docker)
    if [[ -n "$CONTAINER_NAME" ]]; then
        SYNC_ARGS+=(--container "$CONTAINER_NAME")
    fi
    if [[ "$DRY_RUN" == true ]]; then
        SYNC_ARGS+=(--dry-run)
    fi
    echo -e "${BLUE}🔄 Upsert 模式（更新已存在 + 新增）${NC}"
    PYTHONPATH="${SCRIPT_DIR}" "${SYNC_ARGS[@]}"
    exit $?
fi

# 自動偵測容器名稱
if [[ -z "$CONTAINER_NAME" ]]; then
    echo -e "${BLUE}🔍 自動偵測 Nextcloud 容器...${NC}"
    CONTAINER_NAME=$(docker ps --format "{{.Names}}" | grep -i nextcloud | head -n 1)
    
    if [[ -z "$CONTAINER_NAME" ]]; then
        echo -e "${RED}❌ 找不到 Nextcloud 容器${NC}" >&2
        echo "請使用 --container 指定容器名稱"
        exit 1
    fi
    echo -e "${GREEN}✅ 找到容器: ${CONTAINER_NAME}${NC}"
fi

# 檢查容器是否存在且運行中
if ! docker ps --format "{{.Names}}" | grep -q "^${CONTAINER_NAME}$"; then
    echo -e "${RED}❌ 容器 ${CONTAINER_NAME} 不存在或未運行${NC}" >&2
    exit 1
fi

# 顯示檔案資訊
FILE_SIZE=$(du -h "$MOUNTS_FILE" | cut -f1)
MOUNT_COUNT=$(jq 'length' "$MOUNTS_FILE" 2>/dev/null || echo "未知")

echo -e "${BLUE}📋 匯入資訊：${NC}"
echo "  容器名稱: ${CONTAINER_NAME}"
echo "  檔案路徑: ${MOUNTS_FILE}"
echo "  檔案大小: ${FILE_SIZE}"
echo "  掛載數量: ${MOUNT_COUNT}"

# 複製檔案到容器
TEMP_PATH="/tmp/$(basename "$MOUNTS_FILE")"
echo -e "${BLUE}📤 複製檔案到容器...${NC}"
docker cp "$MOUNTS_FILE" "${CONTAINER_NAME}:${TEMP_PATH}"

# 預覽模式
echo -e "${BLUE}🔍 預覽模式（dry-run）...${NC}"
if docker exec -u www-data "${CONTAINER_NAME}" \
    php occ files_external:import --dry "${TEMP_PATH}" 2>&1; then
    echo -e "${GREEN}✅ 預覽成功${NC}"
else
    echo -e "${RED}❌ 預覽失敗${NC}" >&2
    docker exec "${CONTAINER_NAME}" rm -f "${TEMP_PATH}"
    exit 1
fi

# 如果是 dry-run 模式，只預覽不匯入
if [[ "$DRY_RUN" == true ]]; then
    echo -e "${YELLOW}ℹ️  Dry-run 模式，不執行實際匯入${NC}"
    docker exec "${CONTAINER_NAME}" rm -f "${TEMP_PATH}"
    exit 0
fi

# 確認是否繼續
if [[ "$AUTO_CONFIRM" != true ]]; then
    echo ""
    read -p "是否繼續匯入？(y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${YELLOW}❌ 已取消${NC}"
        docker exec "${CONTAINER_NAME}" rm -f "${TEMP_PATH}"
        exit 1
    fi
fi

# 正式匯入
echo -e "${BLUE}📥 開始匯入...${NC}"
START_TIME=$(date +%s)

if docker exec -u www-data "${CONTAINER_NAME}" \
    php occ files_external:import "${TEMP_PATH}" 2>&1; then
    END_TIME=$(date +%s)
    DURATION=$((END_TIME - START_TIME))
    
    echo -e "${GREEN}✅ 匯入完成（耗時: ${DURATION} 秒）${NC}"
else
    echo -e "${RED}❌ 匯入失敗${NC}" >&2
    docker exec "${CONTAINER_NAME}" rm -f "${TEMP_PATH}"
    exit 1
fi

# 清理臨時檔案
docker exec "${CONTAINER_NAME}" rm -f "${TEMP_PATH}"

echo -e "${GREEN}🎉 全部完成！${NC}"
