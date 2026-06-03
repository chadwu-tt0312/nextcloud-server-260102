#!/bin/bash
#
# Nextcloud 批次設定 Files App 個人偏好 (Docker)
#
# 用途（對應需求 Q2）：
#   批次取消每個帳號「個人設定 → 其他設定」的勾選。
#
# 原理說明：
#   這些勾選是 Files app 的 per-user 偏好（存於 oc_preferences, appid=files）。
#   - OCS Preferences API 只能設定「登入者自己」，且 files app 未註冊驗證
#     listener，admin 無法替他人設定 → 因此只能用 occ。
#   - occ user:setting <uid> files <key> <value> 可由 admin 對任意帳號設定。
#
# 合法的 files key（見 apps/files/lib/Service/UserConfig.php ALLOWED_CONFIGS）：
#   crop_image_previews, default_view, folder_tree, grid_view,
#   show_dialog_deletion, show_dialog_file_extension, show_files_extensions,
#   show_hidden, show_mime_column, sort_favorites_first, sort_folders_first
#   值：1 = 勾選, 0 = 取消勾選
#
# 用法:
#   # 1) 先 inspect 任一帳號，確認「其他設定」兩個勾選的實際 key
#   ./set_files_settings_docker.sh --inspect <uid>
#
#   # 2) 預覽批次設定（請依 inspect 結果調整 --keys）
#   ./set_files_settings_docker.sh --keys "folder_tree show_mime_column" --value 0 --dry-run
#
#   # 3) 正式批次套用所有帳號
#   ./set_files_settings_docker.sh --keys "folder_tree show_mime_column" --value 0
#

set -euo pipefail

# 預設值
CONTAINER_NAME=""
KEYS="folder_tree show_mime_column"   # 預設值；請以 --inspect 結果為準調整
VALUE="0"
USERS=""
INSPECT_UID=""
DRY_RUN=false
AUTO_CONFIRM=false

# 顏色輸出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

usage() {
    echo "用法: $0 [OPTIONS]"
    echo ""
    echo "選項:"
    echo "  -c, --container NAME   指定容器名稱（預設：自動偵測含 nextcloud 的容器）"
    echo "      --inspect UID      列出該帳號目前所有 files 偏好（用來確認 key）後結束"
    echo "  -k, --keys \"K1 K2\"     要設定的 files key（空白分隔，預設: \"$KEYS\"）"
    echo "      --value V          設定值（1=勾選, 0=取消勾選；預設: $VALUE）"
    echo "  -u, --users \"u1 u2\"    只處理指定帳號（空白分隔）；未指定時處理所有帳號"
    echo "  -d, --dry-run          僅預覽，不實際執行"
    echo "  -y, --yes              自動確認，不詢問"
    echo "  -h, --help             顯示此說明"
    echo ""
    echo "範例:"
    echo "  $0 --inspect minio-DEPT_A"
    echo "  $0 --keys \"folder_tree show_mime_column\" --value 0 --dry-run"
    echo "  $0 --keys \"folder_tree show_mime_column\" --value 0 -y"
}

# 解析參數
while [[ $# -gt 0 ]]; do
    case $1 in
        --container|-c) CONTAINER_NAME="$2"; shift 2 ;;
        --inspect)      INSPECT_UID="$2"; shift 2 ;;
        --keys|-k)      KEYS="$2"; shift 2 ;;
        --value)        VALUE="$2"; shift 2 ;;
        --users|-u)     USERS="$2"; shift 2 ;;
        --dry-run|-d)   DRY_RUN=true; shift ;;
        --yes|-y)       AUTO_CONFIRM=true; shift ;;
        --help|-h)      usage; exit 0 ;;
        *)
            echo -e "${RED}❌ 錯誤：未知參數 $1${NC}" >&2
            usage
            exit 1
            ;;
    esac
done

# 自動偵測容器名稱
if [[ -z "$CONTAINER_NAME" ]]; then
    echo -e "${BLUE}🔍 自動偵測 Nextcloud 容器...${NC}"
    CONTAINER_NAME=$(docker ps --format "{{.Names}}" | grep -i nextcloud | head -n 1 || true)
    if [[ -z "$CONTAINER_NAME" ]]; then
        echo -e "${RED}❌ 找不到 Nextcloud 容器，請使用 --container 指定${NC}" >&2
        exit 1
    fi
    echo -e "${GREEN}✅ 找到容器: ${CONTAINER_NAME}${NC}"
fi

# 檢查容器是否運行中
if ! docker ps --format "{{.Names}}" | grep -q "^${CONTAINER_NAME}$"; then
    echo -e "${RED}❌ 容器 ${CONTAINER_NAME} 不存在或未運行${NC}" >&2
    exit 1
fi

occ() {
    docker exec -u www-data "${CONTAINER_NAME}" php occ "$@"
}

# Inspect 模式：列出該帳號目前所有 files 偏好後結束
if [[ -n "$INSPECT_UID" ]]; then
    echo -e "${BLUE}🔎 帳號 ${INSPECT_UID} 目前的 files 偏好：${NC}"
    occ user:setting "${INSPECT_UID}" files
    echo ""
    echo -e "${YELLOW}ℹ️  請從上面找出「其他設定」兩個勾選對應的 key，再用 --keys 指定。${NC}"
    exit 0
fi

# 取得目標使用者列表
if [[ -n "$USERS" ]]; then
    read -r -a USER_ARR <<< "$USERS"
else
    echo -e "${BLUE}📋 取得所有使用者...${NC}"
    # user:list --output=json 會回傳 {"uid":"displayname",...}
    USER_JSON=$(occ user:list --output=json)
    mapfile -t USER_ARR < <(echo "$USER_JSON" | jq -r 'keys[]')
fi

read -r -a KEY_ARR <<< "$KEYS"

echo -e "${BLUE}📋 設定資訊：${NC}"
echo "  容器名稱: ${CONTAINER_NAME}"
echo "  使用者數: ${#USER_ARR[@]}"
echo "  設定 key: ${KEYS}"
echo "  設定值  : ${VALUE}"
echo "  Dry-run : ${DRY_RUN}"

if [[ ${#USER_ARR[@]} -eq 0 ]]; then
    echo -e "${YELLOW}⚠️  沒有要處理的使用者${NC}"
    exit 0
fi

# 確認
if [[ "$DRY_RUN" != true && "$AUTO_CONFIRM" != true ]]; then
    echo ""
    read -p "是否繼續設定？(y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${YELLOW}❌ 已取消${NC}"
        exit 1
    fi
fi

# 逐一設定
SUCCESS=0
FAILED=0
START_TIME=$(date +%s)

for uid in "${USER_ARR[@]}"; do
    for key in "${KEY_ARR[@]}"; do
        if [[ "$DRY_RUN" == true ]]; then
            echo -e "${YELLOW}[DRY RUN]${NC} occ user:setting ${uid} files ${key} ${VALUE}"
            SUCCESS=$((SUCCESS + 1))
            continue
        fi
        if occ user:setting "${uid}" files "${key}" "${VALUE}" >/dev/null 2>&1; then
            echo -e "${GREEN}✓${NC} ${uid} files ${key} = ${VALUE}"
            SUCCESS=$((SUCCESS + 1))
        else
            echo -e "${RED}❌${NC} ${uid} files ${key} 設定失敗" >&2
            FAILED=$((FAILED + 1))
        fi
    done
done

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

echo ""
echo -e "${GREEN}✅ 處理完成（耗時: ${DURATION} 秒）${NC}"
echo "  成功: ${SUCCESS}"
echo "  失敗: ${FAILED}"

if [[ "$DRY_RUN" == true ]]; then
    echo -e "${YELLOW}ℹ️  Dry-run 模式，未實際變更任何設定${NC}"
fi

[[ "$FAILED" -eq 0 ]]
