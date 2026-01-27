#!/usr/bin/env python3
"""
Nextcloud External Storage Configuration Generator

功能：
- 從 CSV 檔案讀取使用者清單
- 產生 Nextcloud files_external:import 所需的 JSON 設定檔
- 支援斷點續傳功能（中斷後可繼續處理）
- 自動記錄執行過程（檔案與 console 顯示）
- 支援資料變更偵測（使用 SHA256 雜湊值）

版本：1.0.0

用法:
    python gen_external_storage.py --help
    python gen_external_storage.py  # 使用預設值：import/import-accounts.csv 和 mounts.json
    python gen_external_storage.py --csv import-accounts.csv --output mounts.json
    python gen_external_storage.py --csv import/import-accounts.csv --output mounts.json

CSV 格式範例 (import-accounts.csv):
    Dept_name,Emp_no,Capacity
    SMG_ARC1,,10GiB
    SMG_ARC5,00059094,2GiB

注意事項：
- CSV 檔案路徑相同即視為相同檔案（用於斷點續傳驗證）
- 支援最多 50,000 筆資料（目前已知使用場景）
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

# 載入 .env 環境變數
load_dotenv()

# ============================================================================
# 常數定義
# ============================================================================

# 進度顯示與狀態儲存間隔
PROGRESS_REPORT_INTERVAL = 100  # 每 100 筆顯示進度
STATE_SAVE_INTERVAL = 500  # 每 500 筆儲存狀態
STATE_SAVE_TIME_INTERVAL = 30  # 每 30 秒儲存狀態

# 超大檔案定義（目前已知使用場景在 5 萬筆內，不需要特殊處理）
LARGE_FILE_THRESHOLD = 50000  # 超過此筆數可考慮優化，但目前不處理


# ============================================================================
# 工具函數
# ============================================================================


def get_env_config() -> dict[str, str]:
    """
    從環境變數讀取 MinIO 連線設定

    Returns:
        包含 hostname, port, region, access_key, secret_key 的字典
    """
    from urllib.parse import urlparse

    minio_url = os.getenv("ENV_MINIO_URL", "http://localhost:9000")
    parsed = urlparse(minio_url)

    return {
        "hostname": parsed.hostname or "localhost",
        "port": str(parsed.port or 9000),
        "region": os.getenv("ENV_REGION", "us-east-1"),
        "access_key": os.getenv("ENV_MINIO_ACCESS_KEY", ""),
        "secret_key": os.getenv("ENV_MINIO_SECRET_KEY", ""),
    }


def sanitize_bucket_name(name: str) -> str:
    """
    將名稱轉換為符合 S3/MinIO bucket 命名規則的格式

    規則：
    - 只能用 a-z、0-9、點 (.)、連字號 (-)
    - 長度 3-63 字元
    - 開頭結尾不能是 . 或 -
    - 不能有連續的點

    Args:
        name: 原始名稱

    Returns:
        符合規則的 bucket 名稱
    """
    # 1. 轉小寫
    result = name.lower()

    # 2. 將底線、斜線和反斜線替換為連字號
    result = result.replace("_", "-").replace("/", "-").replace("\\", "-")

    # 3. 移除不允許的字元（只保留 a-z, 0-9, ., -）
    result = re.sub(r"[^a-z0-9.-]", "", result)

    # 4. 將連續的點替換為單一點
    result = re.sub(r"\.{2,}", ".", result)

    # 5. 將連續的連字號替換為單一連字號
    result = re.sub(r"-{2,}", "-", result)

    # 6. 移除開頭的 . 或 -
    result = result.lstrip(".-")

    # 7. 移除結尾的 . 或 -
    result = result.rstrip(".-")

    # 8. 確保長度在 3-63 之間
    if len(result) < 3:
        result = result.ljust(3, "0")  # 補 0 到最小長度
    elif len(result) > 63:
        result = result[:63].rstrip(".-")  # 截斷並移除結尾的 . 或 -

    return result


def generate_bucket_name_from_user_id(user_id: str, bucket_suffix: str = "-filespace") -> str:
    """
    從 user_id 產生符合 MinIO 命名規則的 bucket 名稱

    轉換邏輯：
    1. 移除 "minio-" 前綴（如果存在）
    2. 使用 sanitize_bucket_name() 處理
    3. 加上 bucket_suffix

    範例：
    - "minio-DEPT_SMG_ARC1" -> "dept-smg-arc1-filespace"
    - "minio-00059094" -> "00059094-filespace"

    Args:
        user_id: 使用者 ID（例如：minio-DEPT_SMG_ARC1）
        bucket_suffix: Bucket 名稱後綴（預設：-filespace）

    Returns:
        符合 MinIO 命名規則的 bucket 名稱
    """
    # 移除 "minio-" 前綴（如果存在）
    if user_id.startswith("minio-"):
        base_name = user_id[6:]  # 移除 "minio-" (6 個字元)
    else:
        base_name = user_id

    # 使用 sanitize_bucket_name() 處理
    sanitized = sanitize_bucket_name(base_name)

    # 加上後綴
    bucket_name = f"{sanitized}{bucket_suffix}"

    return bucket_name


def calculate_row_hash(row: dict[str, str]) -> str:
    """
    計算 CSV 資料列的雜湊值（用於比較資料是否變更）

    Args:
        row: CSV 資料列字典

    Returns:
        SHA256 雜湊值（hex 字串）
    """
    # 將所有欄位值排序後組合
    sorted_items = sorted(row.items())
    row_str = "|".join(f"{k}:{v}" for k, v in sorted_items)
    return hashlib.sha256(row_str.encode("utf-8")).hexdigest()


def setup_logger(log_file: Path | None = None) -> logging.Logger:
    """
    設定 logger（使用 Python logging 模組）
    - 總是輸出到 console（使用 StreamHandler）
    - 如果提供 log_file，同時輸出到檔案

    Args:
        log_file: 記錄檔案路徑（None 時只輸出到 console）

    Returns:
        Logger 物件
    """
    logger = logging.getLogger("gen_external_storage.py")
    logger.setLevel(logging.INFO)

    # 避免重複添加 handler
    if logger.handlers:
        return logger

    # 設定格式（不包含時間戳記，因為訊息中已包含）
    formatter = logging.Formatter("%(message)s")

    # 建立 console handler（總是輸出到 console）
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 如果提供 log_file，建立檔案 handler
    if log_file:
        try:
            # 確保目錄存在
            log_file.parent.mkdir(parents=True, exist_ok=True)

            # 建立檔案 handler（追加模式）
            file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

        except IOError as e:
            logger.warning(f"⚠️  警告：無法設定日誌檔案 {log_file}: {e}")

    return logger


def load_state_file(state_path: Path, csv_path: str) -> dict[str, dict[str, Any]]:
    """
    載入狀態檔案並驗證 CSV 檔案路徑

    Args:
        state_path: 狀態檔案路徑
        csv_path: 當前 CSV 檔案路徑（用於驗證）

    Returns:
        已處理的資料字典（key: user_id, value: 資料字典），如果檔案不存在或路徑不匹配則返回空字典
    """
    if not state_path.exists():
        return {}

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

        # 驗證 CSV 檔案路徑是否相同（路徑相同即視為相同檔案）
        state_csv_path = state.get("csv_path", "")
        if state_csv_path != csv_path:
            print(
                f"⚠️  警告：狀態檔案對應的 CSV 檔案不同（狀態: {state_csv_path}, 當前: {csv_path}），將重新開始",
                file=sys.stderr,
            )
            return {}

        processed_data = state.get("processed_data", {})
        return processed_data

    except (json.JSONDecodeError, IOError) as e:
        # 注意：這裡無法使用 logger，因為 logger 可能尚未初始化
        # 使用 print 作為後備方案
        print(f"⚠️  警告：無法讀取狀態檔案 {state_path}: {e}", file=sys.stderr)
        return {}


def save_state_file(
    state_path: Path,
    csv_path: str,
    processed_data: dict[str, dict[str, Any]],
    total_rows: int,
    processed_count: int,
) -> None:
    """
    儲存狀態檔案

    Args:
        state_path: 狀態檔案路徑
        csv_path: CSV 檔案路徑
        processed_data: 已處理的資料（key: user_id, value: 資料字典）
        total_rows: 總資料筆數
        processed_count: 已處理筆數
    """
    state = {
        "csv_path": csv_path,
        "last_updated": datetime.now().isoformat(),
        "total_rows": total_rows,
        "processed_count": processed_count,
        "processed_data": processed_data,
    }

    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except IOError as e:
        # 注意：這裡無法使用 logger，因為 logger 可能尚未初始化
        # 使用 print 作為後備方案
        print(f"⚠️  警告：無法儲存狀態檔案 {state_path}: {e}", file=sys.stderr)


def generate_mount_config(
    mount_id: int,
    user_id: str,
    bucket_name: str,
    access_key: str,
    secret_key: str,
    hostname: str = "localhost",
    port: str = "9000",
    region: str = "us-east-1",
    use_ssl: bool = False,
    use_path_style: bool = True,
    applicable_users: list[str] | None = None,
    applicable_groups: list[str] | None = None,
) -> dict[str, Any]:
    """
    產生單筆 Nextcloud External Storage 掛載設定

    Args:
        mount_id: 掛載 ID（用於 import 時識別）
        user_id: 使用者 ID（也作為掛載點名稱）
        bucket_name: S3 Bucket 名稱
        access_key: S3 Access Key
        secret_key: S3 Secret Key
        hostname: MinIO/S3 主機位址
        port: 連接埠
        region: AWS Region
        use_ssl: 是否啟用 SSL
        use_path_style: 是否啟用 Path Style
        applicable_users: 適用使用者清單（空陣列表示所有使用者）
        applicable_groups: 適用群組清單

    Returns:
        掛載設定字典
    """
    return {
        "mount_id": mount_id,
        "mount_point": f"/{user_id}",
        "storage": "\\OCA\\Files_External\\Lib\\Storage\\AmazonS3",
        "authentication_type": "amazons3::accesskey",
        "configuration": {
            "bucket": bucket_name,
            "hostname": hostname,
            "port": port,
            "region": region,
            "use_ssl": use_ssl,
            "use_path_style": use_path_style,
            "key": access_key,
            "secret": secret_key,
        },
        "options": {},
        "applicable_users": applicable_users or [],
        "applicable_groups": applicable_groups or [],
    }


# ============================================================================
# 主要處理函數
# ============================================================================


def process_csv_rows(
    rows: list[dict[str, str]],
    user_id_extractor: Callable[[dict[str, str]], str],
    mount_generator: Callable[[dict[str, str], str, dict[str, str], int], dict[str, Any]],
    csv_path: str,
    state_path: Path | None,
    resume: bool,
    logger: logging.Logger,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    共用的 CSV 處理邏輯

    Args:
        rows: CSV 資料列清單
        user_id_extractor: 從 CSV 列提取 user_id 的函數
        mount_generator: 產生掛載設定的函數（參數: row, user_id, env_config, mount_id）
        csv_path: CSV 檔案路徑
        state_path: 狀態檔案路徑
        resume: 是否啟用斷點續傳
        logger: Logger 物件

    Returns:
        (掛載設定清單, 統計資訊字典)
    """
    mounts = []
    processed_data: dict[str, dict[str, Any]] = {}
    stats = {
        "total": 0,
        "skipped": 0,
        "updated": 0,
        "new": 0,
        "errors": 0,
    }

    # 載入狀態檔案
    if resume and state_path:
        processed_data = load_state_file(state_path, csv_path)
        if processed_data:
            logger.info(f"📋 載入狀態檔案：已處理 {len(processed_data)} 筆資料")

    stats["total"] = len(rows)
    logger.info(f"📋 開始處理 {stats['total']} 筆資料...")

    # 記錄開始時間
    start_time = datetime.now()
    last_save_time = start_time
    env_config = get_env_config()

    for i, row in enumerate(rows, start=1):
        try:
            # 提取 user_id
            user_id = user_id_extractor(row)
            if not user_id:
                logger.warning(f"⚠️  第 {i} 筆：無法提取 user_id，跳過")
                stats["errors"] += 1
                continue

            # 計算資料雜湊值
            row_hash = calculate_row_hash(row)

            # 檢查是否已處理過
            if resume and user_id in processed_data:
                existing = processed_data[user_id]
                if existing.get("hash") == row_hash:
                    # 資料完全相同，跳過（但需要更新 mount_id）
                    stats["skipped"] += 1
                    existing_mount = existing["mount"].copy()
                    existing_mount["mount_id"] = i
                    mounts.append(existing_mount)
                    if i % PROGRESS_REPORT_INTERVAL == 0:
                        logger.info(
                            f"   📊 進度: {i}/{stats['total']} "
                            f"(跳過: {stats['skipped']}, 新增: {stats['new']}, 更新: {stats['updated']})"
                        )
                    continue
                else:
                    # 資料有變更，需要更新
                    stats["updated"] += 1
                    log_message = f"[{datetime.now().isoformat()}] 更新: {user_id} (第 {i} 筆)"
                    logger.info(f"   🔄 {log_message}")
            else:
                # 新資料
                stats["new"] += 1
                if stats["new"] <= 10 or stats["new"] % 100 == 0:
                    log_message = f"[{datetime.now().isoformat()}] 新增: {user_id} (第 {i} 筆)"
                    logger.info(f"   ➕ {log_message}")

            # 產生掛載設定
            mount = mount_generator(row, user_id, env_config, i)

            mounts.append(mount)

            # 更新已處理資料
            processed_data[user_id] = {
                "hash": row_hash,
                "mount": mount,
                "processed_at": datetime.now().isoformat(),
            }

            # 定期顯示進度並儲存狀態
            if i % PROGRESS_REPORT_INTERVAL == 0:
                elapsed = (datetime.now() - start_time).total_seconds()
                rate = i / elapsed if elapsed > 0 else 0
                remaining = (stats["total"] - i) / rate if rate > 0 else 0
                logger.info(
                    f"   📊 進度: {i}/{stats['total']} "
                    f"(跳過: {stats['skipped']}, 新增: {stats['new']}, 更新: {stats['updated']}) "
                    f"速度: {rate:.1f} 筆/秒, 預估剩餘: {remaining:.0f} 秒"
                )

            # 定期儲存狀態
            current_time = datetime.now()
            if state_path and (
                i % STATE_SAVE_INTERVAL == 0
                or (current_time - last_save_time).total_seconds() >= STATE_SAVE_TIME_INTERVAL
            ):
                save_state_file(state_path, csv_path, processed_data, stats["total"], i)
                last_save_time = current_time

        except KeyError as e:
            logger.error(f"❌ 第 {i} 筆：缺少必要欄位 {e}")
            stats["errors"] += 1
            logger.error(f"[{datetime.now().isoformat()}] 錯誤: 第 {i} 筆 - 缺少欄位 {e}")
        except ValueError as e:
            logger.error(f"❌ 第 {i} 筆：資料格式錯誤 {e}")
            stats["errors"] += 1
            logger.error(f"[{datetime.now().isoformat()}] 錯誤: 第 {i} 筆 - 格式錯誤 {e}")
        except Exception as e:
            logger.error(f"❌ 第 {i} 筆處理失敗: {e}")
            stats["errors"] += 1
            logger.error(f"[{datetime.now().isoformat()}] 錯誤: 第 {i} 筆 - {e}")

    # 最終儲存狀態
    if state_path:
        save_state_file(state_path, csv_path, processed_data, stats["total"], stats["total"])

    # 顯示完成統計
    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info("")
    logger.info("✅ 處理完成！")
    logger.info(f"   總筆數: {stats['total']}")
    logger.info(f"   跳過: {stats['skipped']} 筆（資料未變更）")
    logger.info(f"   新增: {stats['new']} 筆")
    logger.info(f"   更新: {stats['updated']} 筆")
    logger.info(f"   錯誤: {stats['errors']} 筆")
    logger.info(f"   耗時: {elapsed:.1f} 秒")
    if stats["total"] > 0:
        logger.info(f"   平均速度: {stats['total'] / elapsed:.1f} 筆/秒")

    return mounts, stats


def generate_from_import_csv(
    csv_path: str,
    logger: logging.Logger,
    bucket_suffix: str = "-filespace",
    hostname: str | None = None,
    port: str | None = None,
    region: str | None = None,
    use_ssl: bool = False,
    use_path_style: bool = True,
    state_path: Path | None = None,
    resume: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    從 import-accounts.csv 格式讀取使用者清單並產生掛載設定

    CSV 欄位:
        - Dept_name: 部門名稱
        - Emp_no: 員工編號
        - Capacity: 容量（目前不使用）

    user_id 組裝邏輯:
        - Emp_no 有值時: f"minio-{Emp_no}"
        - Emp_no 空白時: f"minio-DEPT_{Dept_name}"

    Args:
        csv_path: CSV 檔案路徑
        bucket_suffix: Bucket 名稱後綴
        hostname: MinIO/S3 主機位址（None 時從環境變數讀取）
        port: 連接埠（None 時從環境變數讀取）
        region: AWS Region（None 時從環境變數讀取）
        use_ssl: 是否啟用 SSL
        use_path_style: 是否啟用 Path Style
        state_path: 狀態檔案路徑（用於斷點續傳）
        resume: 是否啟用斷點續傳
        log_file: 記錄檔案路徑

    Returns:
        (掛載設定清單, 統計資訊字典)
    """

    def extract_user_id(row: dict[str, str]) -> str:
        """從 CSV 列提取 user_id"""
        emp_no = row.get("Emp_no", "").strip()
        dept_name = row.get("Dept_name", "").strip()

        if emp_no:
            return f"minio-{emp_no}"
        elif dept_name:
            return f"minio-DEPT_{dept_name}"
        else:
            return ""  # 無法提取，將在 process_csv_rows 中處理為錯誤

    def generate_mount(
        row: dict[str, str], user_id: str, config: dict[str, str], mount_id: int
    ) -> dict[str, Any]:
        """產生掛載設定"""
        bucket_name = generate_bucket_name_from_user_id(user_id, bucket_suffix)

        return generate_mount_config(
            mount_id=mount_id,
            user_id=user_id,
            bucket_name=bucket_name,
            access_key=config["access_key"],
            secret_key=config["secret_key"],
            hostname=hostname or config["hostname"],
            port=port or config["port"],
            region=region or config["region"],
            use_ssl=use_ssl,
            use_path_style=use_path_style,
        )

    # 讀取 CSV 檔案
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # 使用共用處理邏輯
    mounts, stats = process_csv_rows(
        rows=rows,
        user_id_extractor=extract_user_id,
        mount_generator=generate_mount,
        csv_path=csv_path,
        state_path=state_path,
        resume=resume,
        logger=logger,
    )

    return mounts, stats


def save_mounts_json(mounts: list[dict[str, Any]], output_path: str) -> None:
    """
    儲存掛載設定為 JSON 檔案

    Args:
        mounts: 掛載設定清單
        output_path: 輸出檔案路徑
    """
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(mounts, f, indent=2, ensure_ascii=False)

    file_size = output_file.stat().st_size
    file_size_mb = file_size / (1024 * 1024)

    # 使用 logger 輸出（如果 logger 已初始化）
    logger = logging.getLogger("gen_external_storage.py")
    if logger.handlers:
        logger.info(f"✅ 已產生 {len(mounts)} 個掛載設定至 {output_path}")
        logger.info(f"   檔案大小: {file_size_mb:.2f} MB")
    else:
        # 後備方案：如果 logger 尚未初始化，使用 print
        print(f"✅ 已產生 {len(mounts)} 個掛載設定至 {output_path}")
        print(f"   檔案大小: {file_size_mb:.2f} MB")


# ============================================================================
# 主程式
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nextcloud External Storage Configuration Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例:
  # 使用預設值（import/import-accounts.csv 和 mounts.json）
  python gen_external_storage.py

  # 從 import 目錄下的 import-accounts.csv 讀取（欄位: Dept_name,Emp_no,Capacity）
  python gen_external_storage.py --csv import-accounts.csv --output mounts.json

  # 指定完整路徑（仍會自動加上 import/ 前綴）
  python gen_external_storage.py --csv import/import-accounts.csv --output mounts.json

  # 自訂 MinIO 連線設定（覆蓋環境變數）
  python gen_external_storage.py --csv import-accounts.csv \\
      --hostname minio.example.com --port 443 --use-ssl --output mounts.json

  # 停用斷點續傳
  python gen_external_storage.py --csv import-accounts.csv --output mounts.json --no-resume

匯入指令:
  php occ files_external:import --dry mounts.json  # 預覽
  php occ files_external:import mounts.json        # 正式匯入
        """,
    )

    # 資料來源（可選，預設為 import/import-accounts.csv）
    parser.add_argument(
        "--csv",
        type=str,
        default="import/import-accounts.csv",
        dest="import_csv",
        help="import-accounts.csv 格式檔案路徑（欄位: Dept_name, Emp_no, Capacity），檔案會從 import 目錄讀取（預設: import/import-accounts.csv）",
    )

    # 輸出設定
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="mounts.json",
        help="輸出 JSON 檔案路徑（預設: mounts.json）",
    )

    # MinIO/S3 連線設定（可選，預設從環境變數讀取）
    parser.add_argument(
        "--hostname",
        type=str,
        default=None,
        help="MinIO/S3 主機位址（預設從 ENV_MINIO_URL 讀取）",
    )
    parser.add_argument(
        "--port",
        type=str,
        default=None,
        help="連接埠（預設從 ENV_MINIO_URL 讀取）",
    )
    parser.add_argument(
        "--region",
        type=str,
        default=None,
        help="AWS Region（預設從 ENV_REGION 讀取）",
    )
    parser.add_argument(
        "--use-ssl",
        action="store_true",
        help="啟用 SSL（預設: 關閉）",
    )
    parser.add_argument(
        "--no-path-style",
        action="store_true",
        help="停用 Path Style（預設: 啟用）",
    )
    parser.add_argument(
        "--access-key",
        type=str,
        default=None,
        help="Access Key（預設從 ENV_MINIO_ACCESS_KEY 讀取）",
    )
    parser.add_argument(
        "--secret-key",
        type=str,
        default=None,
        help="Secret Key（預設從 ENV_MINIO_SECRET_KEY 讀取）",
    )

    # Bucket 命名設定
    parser.add_argument(
        "--bucket-suffix",
        type=str,
        default="-filespace",
        help="Bucket 名稱後綴（預設: -filespace）",
    )

    # 斷點續傳設定
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="停用斷點續傳功能（預設: 啟用）",
    )
    parser.add_argument(
        "--state-file",
        type=str,
        default=None,
        help="狀態檔案路徑（預設: {output}.state.json）",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="記錄檔案路徑（預設: logs/gen-mount.log）",
    )

    args = parser.parse_args()

    # 設定狀態檔案和記錄檔案路徑
    output_path = Path(args.output)
    state_path = (
        Path(args.state_file) if args.state_file else output_path.with_suffix(".state.json")
    )
    log_path = Path(args.log_file) if args.log_file else Path("logs/gen-mount.log")

    # 初始化 logger（總是初始化，即使沒有 log_file 也會輸出到 console）
    logger = setup_logger(log_path)
    logger.info(f"=== 執行記錄開始: {datetime.now().isoformat()} ===")

    # 統一檔案來源為 import 目錄
    csv_path_str = args.import_csv
    csv_path = Path(csv_path_str)

    # 如果路徑不是絕對路徑且不包含 import 目錄，自動加上 import/ 前綴
    if not csv_path.is_absolute():
        # 檢查路徑的第一個部分是否為 "import"
        parts = csv_path.parts
        if len(parts) == 0 or parts[0] != "import":
            csv_path = Path("import") / csv_path_str
            csv_path_str = str(csv_path)

    if not csv_path.exists():
        logger.error(f"❌ CSV 檔案不存在: {csv_path}")
        return 1

    logger.info(f"CSV 檔案: {csv_path}")
    logger.info(f"輸出檔案: {args.output}")
    logger.info(f"狀態檔案: {state_path}")
    logger.info("=" * 60)
    logger.info("")

    # 產生掛載設定
    try:
        resume = not args.no_resume

        mounts, stats = generate_from_import_csv(
            csv_path=csv_path_str,
            logger=logger,
            bucket_suffix=args.bucket_suffix,
            hostname=args.hostname,
            port=args.port,
            region=args.region,
            use_ssl=args.use_ssl,
            use_path_style=not args.no_path_style,
            state_path=state_path if resume else None,
            resume=resume,
        )

        if not mounts:
            logger.error("❌ 未產生任何掛載設定")
            return 1

        save_mounts_json(mounts, args.output)

        # 寫入最終統計到記錄檔案
        logger.info("")
        logger.info(f"=== 執行記錄結束: {datetime.now().isoformat()} ===")
        logger.info(f"總筆數: {stats['total']}")
        logger.info(f"跳過: {stats['skipped']} 筆")
        logger.info(f"新增: {stats['new']} 筆")
        logger.info(f"更新: {stats['updated']} 筆")
        logger.info(f"錯誤: {stats['errors']} 筆")

        logger.info("")
        if log_path:
            logger.info(f"📝 記錄檔案: {log_path}")
        if resume:
            logger.info(f"💾 狀態檔案: {state_path}")

        return 0

    except Exception as e:
        # 如果 logger 尚未初始化，使用 print 作為後備方案
        logger = logging.getLogger("gen_external_storage.py")
        if logger.handlers:
            logger.error("")
            logger.error(f"❌ 發生錯誤: {e}")
            logger.error(traceback.format_exc())
        else:
            print(f"❌ 發生錯誤: {e}", file=sys.stderr)
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
