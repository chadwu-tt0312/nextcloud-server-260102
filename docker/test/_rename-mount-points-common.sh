#!/usr/bin/env bash
#
# 共用邏輯：先列出外部儲存，再逐筆更新 mount_point
#
# 更名規則：
#   /minio-{帳號}             → /個人-{帳號}（每位使用者最多 1 個）
#   /minio-DEPT_{部門-名稱}     → /部門-{部門_名稱}（每位使用者可 0～n 個）
#
# 例：/minio-00059094 → /個人-00059094
#     /minio-DEPT_SDD-ARC5-ENG1 → /部門-SDD_ARC5_ENG1
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
export PYTHONPATH="${REPO_ROOT}/tools${PYTHONPATH:+:${PYTHONPATH}}"

# 由 docker / k8s 腳本設定後呼叫 rename_mount_points_run
NC_OCC_CMD=()          # e.g. docker exec ... php occ
NC_OCC_PHP_PREFIX=()   # e.g. php /var/www/html/occ 的完整前綴（不含 occ 子命令）
NC_TARGET_LABEL=""
LOG_DIR=""
APPLY=false
LIMIT=0
LIST_ONLY=false
SKIP_LIST=false

rename_mount_points_usage() {
    cat <<'EOF'
用法: rename-mount-points-{docker|k8s}.sh [選項]

流程:
  [1] occ files_external:list  → 儲存可讀清單
  [2] occ files_external:export → 儲存 JSON
  [3] 依規則逐筆 occ files_external:config <id> mount_point <新名稱>

選項:
  --apply           實際更新（預設乾跑）
  --limit N         最多更新 N 筆（0=不限制）
  --list-only       只執行步驟 1～2，不更新
  --skip-list       略過列出，使用 --export-file 既有 JSON
  --export-file F   使用指定 export JSON（搭配 --skip-list）
  --log-dir DIR     記錄目錄（預設 docker/test/logs）
  -h, --help        說明

同一使用者可同時有多個「部門-*」掛載，但「個人-*」最多 1 個。
EOF
}

# 在遠端執行 occ 子命令（www-data）
nc_occ() {
    "${NC_OCC_CMD[@]}" "$@"
}

rename_mount_points_step_list() {
    local ts list_file export_file
    ts="$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$LOG_DIR"
    list_file="${LOG_DIR}/files_external_list-${ts}.log"
    export_file="${LOG_DIR}/files_external_export-${ts}.json"

    echo "[1/3] 列出外部儲存 (files_external:list)..."
    nc_occ files_external:list | tee "$list_file"
    echo "  → 已儲存: ${list_file}"

    echo ""
    echo "[2/3] 匯出 JSON (files_external:export)..."
    nc_occ files_external:export > "$export_file"
    local count
    count="$(python3 -c "import json; print(len(json.load(open('${export_file}'))))" 2>/dev/null || echo "?")"
    echo "  → 已儲存: ${export_file}（${count} 筆）"
    echo ""

    RENAME_EXPORT_FILE="$export_file"
    RENAME_LIST_FILE="$list_file"
}

# 產生更新計畫 TSV：status mount_id old new users_json
# status: update | skip | conflict
rename_mount_points_build_plan() {
    local export_file="$1"
    local plan_file="$2"
    python3 - "$export_file" "$plan_file" <<'PY'
import json
import sys

from tools_mount_point import build_mount_rename_plan

export_path, plan_path = sys.argv[1], sys.argv[2]
with open(export_path, encoding="utf-8") as f:
    mounts = json.load(f)

plan = build_mount_rename_plan(mounts)
with open(plan_path, "w", encoding="utf-8") as out:
    for row in plan:
        out.write(
            f"{row['status']}\t{row['mount_id']}\t{row['old']}\t{row['new']}\t"
            f"{json.dumps(row['users'], ensure_ascii=False)}\n"
        )
PY
}

rename_mount_points_step_apply() {
    local export_file="$1"
    local plan_file
    plan_file="$(mktemp)"

    rename_mount_points_build_plan "$export_file" "$plan_file"

    local total update skip conflict updated errors
    total=0
    update=0
    skip=0
    conflict=0
    updated=0
    errors=0

    echo "[3/3] 逐筆更新 mount_point..."
    echo "  （個人：每使用者最多 1 個；部門：可有多個不同名稱）"
    echo ""

  while IFS=$'\t' read -r status mount_id old new users_json; do
        total=$((total + 1))
        case "$status" in
            skip)
                skip=$((skip + 1))
                continue
                ;;
            conflict)
                conflict=$((conflict + 1))
                echo "  [衝突] id=${mount_id} ${old} → ${new}  users=${users_json}"
                echo "         同一使用者將有多個「${new}」，請先整併後再執行"
                continue
                ;;
            update)
                update=$((update + 1))
                if [[ "$LIMIT" -gt 0 && "$updated" -ge "$LIMIT" ]]; then
                    echo "  （已達 --limit ${LIMIT}，停止）"
                    break
                fi
                if $APPLY; then
                    if nc_occ files_external:config "$mount_id" mount_point "$new"; then
                        updated=$((updated + 1))
                        echo "  [OK] id=${mount_id}  ${old} → ${new}"
                    else
                        errors=$((errors + 1))
                        echo "  [ERR] id=${mount_id}  ${old} → ${new}" >&2
                    fi
                else
                    updated=$((updated + 1))
                    echo "  [dry-run] id=${mount_id}  ${old} → ${new}"
                fi
                ;;
        esac
    done < "$plan_file"

    echo ""
    echo "========================================"
    echo " 摘要 (${NC_TARGET_LABEL})"
    echo "========================================"
    echo "  匯出總筆數:       ${total}"
    echo "  將更新/已模擬:    ${updated}（符合條件 ${update} 筆）"
    echo "  略過:             ${skip}"
    echo "  衝突:             ${conflict}"
    echo "  錯誤:             ${errors}"
    echo ""
    if ! $APPLY && [[ "$update" -gt 0 ]]; then
        echo "目前為乾跑。確認無誤後加上 --apply 執行。"
    fi

    rm -f "$plan_file"
}

rename_mount_points_run() {
    local export_file="${RENAME_EXPORT_FILE:-}"

    echo "========================================"
    echo " 外部儲存 mount_point 批次更名"
    echo " 目標: ${NC_TARGET_LABEL}"
    echo " 模式: $(if $APPLY; then echo 'APPLY'; else echo 'DRY-RUN'; fi)"
    echo "========================================"
    echo ""
    echo "規則:"
    echo "  /minio-{帳號}         → /個人-{帳號}"
    echo "  /minio-DEPT_{部門名}  → /部門-{部門_名}"
    echo "  個人：每使用者最多 1 個；部門：0～n 個"
    echo ""

    if ! $SKIP_LIST; then
        rename_mount_points_step_list
        export_file="$RENAME_EXPORT_FILE"
    elif [[ -z "$export_file" ]]; then
        echo "❌ --skip-list 需搭配 --export-file" >&2
        exit 1
    fi

    if [[ ! -f "$export_file" ]]; then
        echo "❌ 找不到 export 檔: ${export_file}" >&2
        exit 1
    fi

    if $LIST_ONLY; then
        echo "（--list-only）已完成列出，未執行更新。"
        exit 0
    fi

    rename_mount_points_step_apply "$export_file"
}
