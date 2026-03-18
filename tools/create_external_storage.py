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
    python create_external_storage.py --mounts-file import/mounts.json
    python create_external_storage.py --mounts-file import/mounts.json --dry-run
"""

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

from tools_external_storage import (
    convert_mount_to_api_format,
    extract_amazons3_buckets,
    fetch_all_external_storages,
    get_nextcloud_config,
    setup_logger,
)
from tools_external_storage import (
    create_external_storage as create_storage_api,
)

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
  python create_external_storage.py --mounts-file import/mounts.json

  # 預覽模式（不實際建立）
  python create_external_storage.py --mounts-file import/mounts.json --dry-run
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
        default="import/mounts.json",
        help="mounts.json 檔案路徑（預設: import/mounts.json）",
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
    logger = setup_logger(log_path, logger_name="create_external_storage")

    # 讀取 Nextcloud 設定
    config = get_nextcloud_config()
    if not (config["url"] and config["username"] and config["password"]):
        logger.error(
            "❌ 請設定環境變數 ENV_NEXTCLOUD_URL、ENV_NEXTCLOUD_USER、ENV_NEXTCLOUD_PASSWORD"
        )
        return 1

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
            config["url"], config["username"], config["password"], logger=logger
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
                success, message = create_storage_api(
                    config["url"],
                    config["username"],
                    config["password"],
                    api_data,
                    dry_run=args.dry_run,
                    logger=logger,
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
        logger.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
