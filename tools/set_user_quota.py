#!/usr/bin/env python3
"""
Nextcloud 批次設定使用者 Quota 工具

用途（對應需求 Q1）：
- 將使用者的 quota 設為 0，使其「無法在個人空間 (home) 新增檔案」，
  但「仍可在外部掛載空間 (minio / amazons3) 新增檔案」。

功能：
- 取得所有使用者：OCS API（有 ENV）或 occ user:list（Docker/K8s，無 ENV）
- 設定 quota：OCS API（有 ENV，支援併發）或 occ user:setting（無 ENV）
- 預設排除 admin；未指定 --users 時處理全部（排除後）

版本：1.1.0

用法:
    uv run python set_user_quota.py --dry-run -c chad-nextcloud-1
    uv run python set_user_quota.py --users minio-A,minio-B
    uv run python set_user_quota.py --apply -c chad-nextcloud-1   # 需加 --apply 的話用無 --dry-run
"""

import argparse
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from tools_external_storage import get_nextcloud_config, setup_logger
from tools_runtime import NextcloudRuntime, create_runtime, fetch_all_users_from_occ
from tools_user_admin import (
    fetch_all_users,
    ocs_configured,
    set_user_quota,
    set_user_quota_occ,
)


def resolve_user_list(
    args,
    config: dict[str, str],
    runtime: NextcloudRuntime | None,
    logger,
) -> list[str] | None:
    if args.users:
        return [u.strip() for u in args.users.split(",") if u.strip()]

    if args.user_source == "ocs":
        if not ocs_configured(config):
            logger.error("❌ --user-source ocs 需要 ENV_NEXTCLOUD_URL / USER / PASSWORD")
            return None
        success, message, users = fetch_all_users(
            config["url"], config["username"], config["password"], logger=logger
        )
    elif args.user_source == "occ":
        if runtime is None:
            logger.error("❌ --user-source occ 需要 Docker/K8s 目標（-c 或 -p）")
            return None
        success, message, users = fetch_all_users_from_occ(runtime, logger)
    else:  # auto
        if ocs_configured(config):
            logger.info("使用者來源: OCS API")
            success, message, users = fetch_all_users(
                config["url"], config["username"], config["password"], logger=logger
            )
        elif runtime is not None:
            logger.info("使用者來源: occ user:list（未設定 ENV_NEXTCLOUD_*）")
            success, message, users = fetch_all_users_from_occ(runtime, logger)
        else:
            logger.error(
                "❌ 需要 ENV_NEXTCLOUD_* 或 Docker/K8s 目標（-c / -p）以取得使用者列表"
            )
            return None

    if not success:
        logger.error(f"❌ 無法取得使用者列表: {message}")
        return None
    return users


def resolve_quota_backend(args, config: dict[str, str], logger) -> str:
    if args.quota_backend != "auto":
        return args.quota_backend
    if ocs_configured(config):
        logger.info("quota 設定方式: OCS API（併發）")
        return "ocs"
    logger.info("quota 設定方式: occ user:setting（未設定 ENV_NEXTCLOUD_*）")
    return "occ"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nextcloud 批次設定使用者 Quota 工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例:
  # 乾跑（無 ENV，Docker + occ）
  uv run python set_user_quota.py --dry-run -c chad-nextcloud-1

  # 正式設定全部（預設排除 admin）
  uv run python set_user_quota.py -c chad-nextcloud-1

  # 指定帳號（不受 exclude 影響）
  uv run python set_user_quota.py --users minio-A,minio-B
        """,
    )

    parser.add_argument(
        "--runtime",
        choices=["docker", "k8s"],
        default="docker",
        help="執行環境（occ 模式需要；預設 docker）",
    )
    parser.add_argument("--container", "-c", type=str, default=None, help="[docker] 容器名稱")
    parser.add_argument("--namespace", "-n", type=str, default="default", help="[k8s] 命名空間")
    parser.add_argument("--pod", "-p", type=str, default=None, help="[k8s] Pod 名稱")
    parser.add_argument(
        "--label-selector",
        type=str,
        default="app=nextcloud",
        help="[k8s] 自動偵測 Pod 的 label",
    )
    parser.add_argument(
        "--user-source",
        choices=["auto", "ocs", "occ"],
        default="auto",
        help="使用者列表來源（預設 auto）",
    )
    parser.add_argument(
        "--quota-backend",
        choices=["auto", "ocs", "occ"],
        default="auto",
        help="quota 設定方式（預設 auto：有 ENV 用 OCS，否則 occ）",
    )

    parser.add_argument("--quota", type=str, default="0", help='quota 值（預設 "0"）')
    parser.add_argument("--users", type=str, default=None, help="只處理指定使用者（逗號分隔）")
    parser.add_argument(
        "--exclude",
        type=str,
        default="admin",
        help="排除的使用者（逗號分隔，預設 admin；僅在未指定 --users 時生效）",
    )
    parser.add_argument(
        "--no-exclude",
        action="store_true",
        help="不排除任何帳號",
    )
    parser.add_argument("--concurrency", type=int, default=20, help="OCS 併發數（預設 20）")
    parser.add_argument("--dry-run", action="store_true", help="預覽模式，不實際設定")
    parser.add_argument("--log-file", type=str, default=None, help="記錄檔案路徑")

    args = parser.parse_args()

    log_path = Path(args.log_file) if args.log_file else Path("logs/set-quota.log")
    logger = setup_logger(log_path, logger_name="set_user_quota")
    config = get_nextcloud_config()

    logger.info(f"=== 執行記錄開始: {datetime.now().isoformat()} ===")
    logger.info(f"目標 quota: {args.quota}")
    logger.info(f"記錄檔案: {log_path}")

    runtime: NextcloudRuntime | None = None
    quota_backend = resolve_quota_backend(args, config, logger)

    need_runtime = (
        args.user_source == "occ"
        or (args.user_source == "auto" and not ocs_configured(config))
        or quota_backend == "occ"
    )
    if need_runtime:
        runtime = create_runtime(
            args.runtime,
            container=args.container,
            namespace=args.namespace,
            pod=args.pod,
            label_selector=args.label_selector,
        )
        target = runtime.resolve_target(logger)
        if target is None:
            logger.error("❌ 找不到 Docker/K8s 目標，請用 -c 或 -p 指定")
            return 1
        logger.info(f"目標: {target.label}")
    elif ocs_configured(config):
        logger.info(f"Nextcloud URL: {config['url']}")

    logger.info("=" * 60)
    stats = {"total": 0, "success": 0, "failed": 0, "skipped": 0}
    start_time = datetime.now()

    try:
        user_ids = resolve_user_list(args, config, runtime, logger)
        if user_ids is None:
            return 1

        if not args.users and not args.no_exclude:
            exclude = {u.strip() for u in args.exclude.split(",") if u.strip()}
            if exclude:
                logger.info(f"排除使用者: {sorted(exclude)}")
        else:
            exclude = set()

        targets = [uid for uid in user_ids if uid not in exclude]
        stats["total"] = len(user_ids)
        stats["skipped"] = len(user_ids) - len(targets)
        logger.info(f"待處理 {len(targets)} 位（排除 {stats['skipped']} 位）")
        logger.info("=" * 60)

        if not targets:
            logger.warning("⚠️  沒有要處理的使用者")
            return 0

        lock = threading.Lock()
        done = {"n": 0}
        total = len(targets)

        def _worker(uid: str) -> tuple[str, bool, str]:
            if quota_backend == "ocs":
                return uid, *set_user_quota(
                    config["url"],
                    config["username"],
                    config["password"],
                    uid,
                    quota=args.quota,
                    dry_run=args.dry_run,
                    logger=logger,
                )
            assert runtime is not None
            return uid, *set_user_quota_occ(
                runtime, uid, quota=args.quota, dry_run=args.dry_run, logger=logger
            )

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

        logger.info(f"設定 quota（backend={quota_backend}）...")
        concurrency = max(1, args.concurrency)

        # occ 每次 exec 會 bootstrap，大量帳號建議設 --concurrency 1 或改用 OCS
        if quota_backend == "occ":
            concurrency = 1
            logger.info("occ 模式使用循序執行（避免大量並行 bootstrap）")

        if concurrency == 1:
            for uid in targets:
                _record(*_worker(uid))
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [pool.submit(_worker, uid) for uid in targets]
                for fut in as_completed(futures):
                    _record(*fut.result())

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
            logger.info("ℹ️  這是預覽模式，未實際變更任何使用者 quota")
        logger.info(f"=== 執行記錄結束: {datetime.now().isoformat()} ===")
        return 0 if stats["failed"] == 0 else 1

    except Exception as e:
        logger.error(f"❌ 發生錯誤: {e}")
        logger.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
