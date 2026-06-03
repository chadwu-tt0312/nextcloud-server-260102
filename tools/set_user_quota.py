#!/usr/bin/env python3
"""
Nextcloud 批次設定使用者 Quota 工具

用途（對應需求 Q1）：
- 將使用者的 quota 設為 0，使其「無法在個人空間 (home) 新增檔案」，
  但「仍可在外部掛載空間 (minio / amazons3) 新增檔案」。
- 原理：Nextcloud 的 quota 僅作用於 home storage；外部儲存空間預設不計入
  quota（系統設定 quota_include_external_storage 預設 false）。

功能：
- 透過 OCS API 取得所有使用者（或由 --users 指定）
- 逐一設定 quota（預設 0），可用 --exclude 排除特定帳號（例如 admin）
- 支援 --dry-run 預覽

版本：1.0.0

用法:
    python set_user_quota.py --help
    python set_user_quota.py --dry-run
    python set_user_quota.py --quota 0 --exclude admin
    python set_user_quota.py --users minio-DEPT_A,minio-DEPT_B
"""

import argparse
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from tools_external_storage import get_nextcloud_config, setup_logger
from tools_user_admin import fetch_all_users, set_user_quota


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nextcloud 批次設定使用者 Quota 工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例:
  # 預覽：對所有使用者設定 quota=0（不實際執行）
  python set_user_quota.py --dry-run

  # 對所有使用者設定 quota=0，但排除 admin
  python set_user_quota.py --quota 0 --exclude admin

  # 只對指定使用者設定
  python set_user_quota.py --users minio-DEPT_A,minio-DEPT_B
        """,
    )

    parser.add_argument(
        "--quota",
        type=str,
        default="0",
        help='quota 值（預設 "0"；也可用 "none"、"1 GB"、"default"）',
    )
    parser.add_argument(
        "--users",
        type=str,
        default=None,
        help="只處理指定使用者（逗號分隔）；未指定時處理所有使用者",
    )
    parser.add_argument(
        "--exclude",
        type=str,
        default=None,
        help="排除的使用者（逗號分隔），例如 admin",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=20,
        help="併發數（同時處理幾位使用者；預設 20，1 表示循序）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="預覽模式，不實際設定，純粹列出項目並記錄",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="記錄檔案路徑（預設: logs/set-quota.log）",
    )

    args = parser.parse_args()

    log_path = Path(args.log_file) if args.log_file else Path("logs/set-quota.log")
    logger = setup_logger(log_path, logger_name="set_user_quota")

    config = get_nextcloud_config()
    if not (config["url"] and config["username"] and config["password"]):
        logger.error(
            "❌ 請設定環境變數 ENV_NEXTCLOUD_URL、ENV_NEXTCLOUD_USER、ENV_NEXTCLOUD_PASSWORD"
        )
        return 1

    logger.info(f"=== 執行記錄開始: {datetime.now().isoformat()} ===")
    logger.info(f"Nextcloud URL: {config['url']}")
    logger.info(f"管理員帳號: {config['username']}")
    logger.info(f"目標 quota: {args.quota}")
    logger.info(f"記錄檔案: {log_path}")
    logger.info("=" * 60)

    stats = {"total": 0, "success": 0, "failed": 0, "skipped": 0}
    start_time = datetime.now()

    try:
        # 1. 決定目標使用者列表
        if args.users:
            user_ids = [u.strip() for u in args.users.split(",") if u.strip()]
            logger.info(f"步驟 1: 使用指定的 {len(user_ids)} 位使用者")
        else:
            logger.info("步驟 1: 取得所有使用者...")
            success, message, user_ids = fetch_all_users(
                config["url"], config["username"], config["password"], logger=logger
            )
            if not success:
                logger.error(f"❌ 無法取得使用者列表: {message}")
                return 1

        # 2. 套用排除清單
        exclude = {u.strip() for u in args.exclude.split(",")} if args.exclude else set()
        if exclude:
            logger.info(f"排除使用者: {sorted(exclude)}")

        # 過濾排除清單後的待處理清單
        targets = [uid for uid in user_ids if uid not in exclude]
        stats["total"] = len(user_ids)
        stats["skipped"] = len(user_ids) - len(targets)
        logger.info(f"待處理 {len(targets)} 位（排除 {stats['skipped']} 位）")
        logger.info("=" * 60)

        # 3. 設定 quota（支援併發）
        logger.info(f"步驟 2: 設定 quota（併發 {max(1, args.concurrency)}）...")
        lock = threading.Lock()
        done = {"n": 0}
        total = len(targets)

        def _worker(uid: str) -> tuple[str, bool, str]:
            ok, msg = set_user_quota(
                config["url"],
                config["username"],
                config["password"],
                uid,
                quota=args.quota,
                dry_run=args.dry_run,
                logger=logger,
            )
            return uid, ok, msg

        def _record(uid: str, ok: bool, msg: str) -> None:
            with lock:
                done["n"] += 1
                idx = done["n"]
                if ok:
                    stats["success"] += 1
                    logger.info(f"✓ [{idx}/{total}] {uid} → {msg}")
                else:
                    stats["failed"] += 1
                    logger.error(f"❌ [{idx}/{total}] {uid} 設定失敗: {msg}")

        concurrency = max(1, args.concurrency)
        if concurrency == 1:
            for uid in targets:
                _record(*_worker(uid))
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [pool.submit(_worker, uid) for uid in targets]
                for fut in as_completed(futures):
                    _record(*fut.result())

        # 4. 輸出統計
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info("")
        logger.info("=" * 60)
        logger.info("✅ 處理完成！")
        logger.info(f"   總筆數: {stats['total']}")
        logger.info(f"   成功: {stats['success']} 筆")
        logger.info(f"   失敗: {stats['failed']} 筆")
        logger.info(f"   跳過: {stats['skipped']} 筆")
        logger.info(f"   耗時: {elapsed:.1f} 秒")

        if args.dry_run:
            logger.info("")
            logger.info("ℹ️  這是預覽模式，未實際變更任何使用者 quota")

        logger.info("")
        logger.info(f"=== 執行記錄結束: {datetime.now().isoformat()} ===")

        return 0 if stats["failed"] == 0 else 1

    except Exception as e:
        logger.error("")
        logger.error(f"❌ 發生錯誤: {e}")
        logger.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
