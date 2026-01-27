#!/usr/bin/env python3
"""
Nextcloud External Storage Sync Tool

功能：
- 從 Nextcloud API 取得所有 amazons3 backend 的外部儲存空間
- 比對 mounts.json 中的 bucket，自動建立不存在的項目
- 使用 httpx 進行 HTTP 請求

版本：1.0.0

用法:
    python create_external_storage.py --help
    python create_external_storage.py --mounts-file tools/mounts.json
    python create_external_storage.py --mounts-file tools/mounts.json --dry-run
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

# 載入 .env 環境變數
load_dotenv()

# ============================================================================
# 設定 Logger
# ============================================================================


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
    logger = logging.getLogger("create_external_storage")
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


# ============================================================================
# 工具函數
# ============================================================================


def get_nextcloud_config() -> dict[str, str]:
    """
    從環境變數讀取 Nextcloud 連線設定

    Returns:
        包含 url, username, password 的字典
    """
    return {
        "url": os.getenv("ENV_NEXTCLOUD_URL", "http://localhost:8085"),
        "username": os.getenv("ENV_NEXTCLOUD_USER", "admin"),
        "password": os.getenv("ENV_NEXTCLOUD_PASSWORD", "password123"),
    }


def fetch_all_external_storages(
    nextcloud_url: str, username: str, password: str
) -> tuple[bool, str, dict[str, Any]]:
    """
    從 Nextcloud API 取得所有外部儲存空間

    Args:
        nextcloud_url: Nextcloud 伺服器 URL
        username: 管理員帳號
        password: 管理員密碼

    Returns:
        (success, message, data) tuple
        - success: 是否成功
        - message: 訊息
        - data: JSON 資料字典（key 為 ID，value 為儲存空間資訊）
    """
    logger = logging.getLogger("create_external_storage")
    url = f"{nextcloud_url.rstrip('/')}/apps/files_external/globalstorages"

    try:
        logger.info(f"呼叫 Nextcloud API: {url}")
        with httpx.Client(timeout=60.0) as client:
            response = client.get(
                url,
                auth=(username, password),
                headers={
                    "OCS-APIRequest": "true",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            data = response.json()

            # 確保回傳的是字典格式
            if not isinstance(data, dict):
                return False, f"API 回傳格式不正確: 預期字典，收到 {type(data)}", {}

            logger.info(f"成功取得 {len(data)} 個外部儲存空間")
            return True, "成功取得外部儲存空間列表", data

    except httpx.TimeoutException:
        error_msg = f"API 請求逾時: {url}"
        logger.error(error_msg)
        return False, error_msg, {}
    except httpx.HTTPStatusError as e:
        error_msg = f"API 請求失敗 (HTTP {e.response.status_code}): {url}"
        logger.error(error_msg)
        return False, error_msg, {}
    except Exception as e:
        error_msg = f"呼叫 API 時發生錯誤: {e}"
        logger.error(error_msg)
        return False, error_msg, {}


def extract_amazons3_buckets(storages: dict[str, Any]) -> list[str]:
    """
    從外部儲存空間列表中提取所有 amazons3 backend 的 bucket 列表

    Args:
        storages: 外部儲存空間字典（key 為 ID，value 為儲存空間資訊）

    Returns:
        bucket 名稱列表（已轉為小寫）
    """
    buckets = []
    for storage_id, storage_info in storages.items():
        if storage_info.get("backend") == "amazons3":
            backend_options = storage_info.get("backendOptions", {})
            bucket = backend_options.get("bucket", "")
            if bucket:
                buckets.append(bucket.lower())
    return buckets


def convert_mount_to_api_format(mount: dict[str, Any]) -> dict[str, Any]:
    """
    將 mounts.json 格式轉換為 Nextcloud API 格式

    Args:
        mount: mounts.json 中的單筆資料

    Returns:
        API 格式的字典
    """
    configuration = mount.get("configuration", {})
    backend_options = {
        "bucket": configuration.get("bucket", ""),
        "hostname": configuration.get("hostname", ""),
        "port": configuration.get("port", "9000"),
        "region": configuration.get("region", "us-east-1"),
        "use_ssl": configuration.get("use_ssl", False),
        "use_path_style": configuration.get("use_path_style", True),
        # 預設值（mounts.json 中可能沒有）
        "legacy_auth": configuration.get("legacy_auth", False),
        "useMultipartCopy": configuration.get("useMultipartCopy", True),
        "key": configuration.get("key", ""),
        "secret": configuration.get("secret", ""),
    }

    return {
        "mountPoint": mount.get("mount_point", ""),
        "backend": "amazons3",
        "authMechanism": mount.get("authentication_type", "amazons3::accesskey"),
        "backendOptions": backend_options,
        "mountOptions": mount.get("options", {}),
        "applicableUsers": mount.get("applicable_users", []),
        "applicableGroups": mount.get("applicable_groups", []),
    }


def create_external_storage(
    nextcloud_url: str,
    username: str,
    password: str,
    api_data: dict[str, Any],
    dry_run: bool = False,
) -> tuple[bool, str]:
    """
    建立新的外部儲存空間

    Args:
        nextcloud_url: Nextcloud 伺服器 URL
        username: 管理員帳號
        password: 管理員密碼
        api_data: API 格式的資料
        dry_run: 是否為預覽模式

    Returns:
        (success, message) tuple
    """
    logger = logging.getLogger("create_external_storage")
    if dry_run:
        logger.info(f"[DRY RUN] 將建立外部儲存空間: {api_data.get('mountPoint')}")
        return True, "DRY RUN: 模擬建立成功"

    url = f"{nextcloud_url.rstrip('/')}/apps/files_external/globalstorages"

    try:
        log_message = f"[{datetime.now().isoformat()}] 建立: {api_data.get('mountPoint')}"
        logger.info(f"   ➕ {log_message}")
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                url,
                auth=(username, password),
                headers={
                    "OCS-APIRequest": "true",
                    "Content-Type": "application/json",
                },
                json=api_data,
            )
            response.raise_for_status()

            # 確認狀態碼為 201
            if response.status_code == 201:
                return True, f"成功建立 (HTTP {response.status_code})"
            else:
                warning_msg = f"⚠️  建立外部儲存空間回傳非預期狀態碼: {response.status_code}"
                logger.warning(warning_msg)
                return False, warning_msg

    except httpx.TimeoutException:
        error_msg = f"API 請求逾時: {url}"
        logger.error(error_msg)
        return False, error_msg
    except httpx.HTTPStatusError as e:
        error_msg = f"API 請求失敗 (HTTP {e.response.status_code}): {url}"
        logger.error(error_msg)
        return False, error_msg
    except Exception as e:
        error_msg = f"建立外部儲存空間時發生錯誤: {e}"
        logger.error(error_msg)
        return False, error_msg


# ============================================================================
# 主要處理函數
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nextcloud External Storage Sync Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例:
  # 使用預設路徑
  python create_external_storage.py

  # 指定 mounts.json 路徑
  python create_external_storage.py --mounts-file tools/mounts.json

  # 預覽模式（不實際建立）
  python create_external_storage.py --mounts-file tools/mounts.json --dry-run
        """,
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="預覽模式，不實際建立，純粹列出項目並記錄",
    )
    parser.add_argument(
        "--mounts-file",
        type=str,
        default="tools/mounts.json",
        help="mounts.json 檔案路徑（預設: tools/mounts.json）",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="記錄檔案路徑（預設: logs/create-mount.log）",
    )

    args = parser.parse_args()

    # 設定 log 檔案路徑
    mounts_file = Path(args.mounts_file)
    log_path = Path(args.log_file) if args.log_file else Path("logs/create-mount.log")

    # 驗證 mounts.json 檔案是否存在
    if not mounts_file.exists():
        print(f"❌ mounts.json 檔案不存在: {mounts_file}", file=sys.stderr)
        return 1

    # 初始化 logger（總是初始化，即使沒有 log_file 也會輸出到 console）
    logger = setup_logger(log_path)

    # 讀取 Nextcloud 設定
    config = get_nextcloud_config()
    logger.info(f"=== 執行記錄開始: {datetime.now().isoformat()} ===")
    logger.info(f"mounts.json 檔案: {mounts_file}")
    logger.info(f"Nextcloud URL: {config['url']}")
    logger.info(f"管理員帳號: {config['username']}")
    logger.info(f"記錄檔案: {log_path}")
    logger.info("=" * 60)
    logger.info("")

    # 統計資訊
    stats = {
        "total": 0,
        "exists": 0,
        "created": 0,
        "failed": 0,
    }

    # 記錄開始時間
    start_time = datetime.now()

    try:
        # 1. 取得所有外部儲存空間
        logger.info("步驟 1: 取得所有外部儲存空間...")
        success, message, storages = fetch_all_external_storages(
            config["url"], config["username"], config["password"]
        )
        if not success:
            logger.error(f"❌ 無法取得外部儲存空間: {message}")
            return 1

        # 2. 提取 amazons3 backend 的 bucket 列表
        logger.info("步驟 2: 提取 amazons3 backend 的 bucket 列表...")
        bucket_lst = extract_amazons3_buckets(storages)
        logger.info(f"找到 {len(bucket_lst)} 個現有的 amazons3 bucket: {bucket_lst}")

        # 3. 讀取 mounts.json
        logger.info("步驟 3: 讀取 mounts.json...")
        with open(mounts_file, "r", encoding="utf-8") as f:
            mounts = json.load(f)

        if not isinstance(mounts, list):
            logger.error(f"❌ mounts.json 格式錯誤: 預期陣列，收到 {type(mounts)}")
            return 1

        stats["total"] = len(mounts)
        logger.info(f"讀取到 {stats['total']} 筆資料")
        logger.info("=" * 60)

        # 4. 迴圈處理每筆資料
        logger.info("步驟 4: 比對並建立外部儲存空間...")
        for i, mount in enumerate(mounts, start=1):
            try:
                configuration = mount.get("configuration", {})
                bucket = configuration.get("bucket", "").lower()
                mount_point = mount.get("mount_point", "")

                if not bucket:
                    logger.warning(f"⚠️  第 {i} 筆：bucket 為空，跳過")
                    stats["failed"] += 1
                    continue

                # 檢查 bucket 是否存在
                if bucket in bucket_lst:
                    logger.info(f"✓ 第 {i} 筆：{mount_point} (bucket: {bucket}) 已存在，跳過")
                    stats["exists"] += 1
                    continue

                # 轉換格式並建立
                api_data = convert_mount_to_api_format(mount)
                success, message = create_external_storage(
                    config["url"],
                    config["username"],
                    config["password"],
                    api_data,
                    dry_run=args.dry_run,
                )

                if success:
                    stats["created"] += 1
                    # 將新建立的 bucket 加入列表（避免重複建立）
                    bucket_lst.append(bucket)
                else:
                    stats["failed"] += 1
                    logger.error(
                        f"❌ 第 {i} 筆：{mount_point} (bucket: {bucket}) 建立失敗: {message}"
                    )

            except KeyError as e:
                logger.error(f"❌ 第 {i} 筆：缺少必要欄位 {e}")
                stats["failed"] += 1
            except Exception as e:
                logger.error(f"❌ 第 {i} 筆處理失敗: {e}")
                stats["failed"] += 1

        # 5. 輸出統計資訊
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info("")
        logger.info("=" * 60)
        logger.info("✅ 處理完成！")
        logger.info(f"   總筆數: {stats['total']}")
        logger.info(f"   已存在: {stats['exists']} 筆")
        logger.info(f"   新增成功: {stats['created']} 筆")
        logger.info(f"   新增失敗: {stats['failed']} 筆")
        logger.info(f"   耗時: {elapsed:.1f} 秒")
        if stats["total"] > 0:
            logger.info(f"   平均速度: {stats['total'] / elapsed:.1f} 筆/秒")

        if args.dry_run:
            logger.info("")
            logger.info("ℹ️  這是預覽模式，未實際建立任何外部儲存空間")

        logger.info("")
        logger.info(f"=== 執行記錄結束: {datetime.now().isoformat()} ===")
        if log_path:
            logger.info(f"📝 記錄檔案: {log_path}")

        return 0 if stats["failed"] == 0 else 1

    except FileNotFoundError as e:
        logger.error(f"❌ 檔案不存在: {e}")
        return 1
    except json.JSONDecodeError as e:
        logger.error(f"❌ JSON 解析錯誤: {e}")
        return 1
    except Exception as e:
        logger.error("")
        logger.error(f"❌ 發生錯誤: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
