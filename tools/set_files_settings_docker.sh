#!/bin/bash
#
# Nextcloud 批次取消「檔案設定 → 其他設定」兩個勾選 (Docker)
#
# 用途（對應需求 Q2）：
#   批次取消每個帳號「檔案設定 → 其他設定」的兩個勾選：
#     - Show recommendations      → recommendations / enabled
#     - Show folder description   → text / workspace_enabled
#
# 原理說明：
#   這兩個勾選由外部 app 透過 OCA.Files.Settings 注入，存於 oc_preferences，
#   不屬於 files app 的 UserConfig.php。
#   - OCS Preferences API 只能設定「登入者自己」，admin 無法代設他人偏好
#   - occ user:setting <uid> <app> <key> <value> 可由 admin 對任意帳號設定
#
# 用法:
#   # 1) 先 inspect 任一帳號，確認目前狀態
#   ./set_files_settings_docker.sh --inspect <uid>
#
#   # 2) 預覽批次取消兩個勾選
#   ./set_files_settings_docker.sh --dry-run
#
#   # 3) 正式批次套用所有帳號
#   ./set_files_settings_docker.sh -y
#

set -euo pipefail

# 預設值
CONTAINER_NAME=""
VALUE="0"
USERS=""
INSPECT_UID=""
DRY_RUN=false
AUTO_CONFIRM=false

# Q2：其他設定兩個勾選（app:key）
SETTINGS=(
    "recommendations:enabled"
    "text:workspace_enabled"
)

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
    echo "      --inspect UID      列出該帳號目前 recommendations / text 偏好後結束"
    echo "      --value V          設定值（1=勾選, 0=取消勾選；預設: $VALUE）"
    echo "  -u, --users \"u1 u2\"    只處理指定帳號（空白分隔）；未指定時處理所有帳號"
    echo "  -d, --dry-run          僅預覽，不實際執行"
    echo "  -y, --yes              自動確認，不詢問"
    echo "  -h, --help             顯示此說明"
    echo ""
    echo "固定取消的兩個勾選："
    echo "  recommendations enabled         (Show recommendations)"
    echo "  text workspace_enabled          (Show folder description)"
    echo ""
    echo "範例:"
    echo "  $0 --inspect minio-DEPT_A"
    echo "  $0 --dry-run"
    echo "  $0 -y"
}

# 解析參數
while [[ $# -gt 0 ]]; do
    case $1 in
        --container|-c) CONTAINER_NAME="$2"; shift 2 ;;
        --inspect)      INSPECT_UID="$2"; shift 2 ;;
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

# Inspect 模式
if [[ -n "$INSPECT_UID" ]]; then
    echo -e "${BLUE}🔎 帳號 ${INSPECT_UID} 目前的「其他設定」偏好：${NC}"
    echo ""
    echo -e "${YELLOW}recommendations (Show recommendations):${NC}"
    occ user:setting "${INSPECT_UID}" recommendations || true
    echo ""
    echo -e "${YELLOW}text (Show folder description):${NC}"
    occ user:setting "${INSPECT_UID}" text || true
    exit 0
fi

# 取得目標使用者列表
if [[ -n "$USERS" ]]; then
    read -r -a USER_ARR <<< "$USERS"
else
    echo -e "${BLUE}📋 取得所有使用者...${NC}"
    USER_JSON=$(occ user:list --output=json)
    mapfile -t USER_ARR < <(echo "$USER_JSON" | jq -r 'keys[]')
fi

echo -e "${BLUE}📋 設定資訊：${NC}"
echo "  容器名稱: ${CONTAINER_NAME}"
echo "  使用者數: ${#USER_ARR[@]}"
echo "  設定項目: recommendations/enabled, text/workspace_enabled"
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
    for setting in "${SETTINGS[@]}"; do
        app="${setting%%:*}"
        key="${setting#*:}"
        if [[ "$DRY_RUN" == true ]]; then
            echo -e "${YELLOW}[DRY RUN]${NC} occ user:setting ${uid} ${app} ${key} ${VALUE}"
            SUCCESS=$((SUCCESS + 1))
            continue
        fi
        if occ user:setting "${uid}" "${app}" "${key}" "${VALUE}" >/dev/null 2>&1; then
            echo -e "${GREEN}✓${NC} ${uid} ${app} ${key} = ${VALUE}"
            SUCCESS=$((SUCCESS + 1))
        else
            echo -e "${RED}❌${NC} ${uid} ${app} ${key} 設定失敗" >&2
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
