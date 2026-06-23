#!/usr/bin/env python3
"""
Nextcloud 批次重新命名外部儲存掛載點（Docker / Kubernetes 驅動器）

將 minio-* 掛載點改為：
  - /minio-{Emp_no}    → /個人雲端硬碟
  - /minio-DEPT_{Dept} → /部門雲端硬碟

建議流程（約 1.7 萬筆）：
  1. 乾跑預覽統計
  2. --limit 10 --apply 小量驗證
  3. 全量 --apply

版本：1.0.0

用法:
    cd tools
    uv run python run_rename_mount_points.py -c <容器>              # 乾跑
    uv run python run_rename_mount_points.py -c <容器> --limit 10 --apply
    uv run python run_rename_mount_points.py -c <容器> --apply
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

from tools_external_storage import (
    MOUNT_POINT_DEPARTMENT,
    MOUNT_POINT_PERSONAL,
    setup_logger,
)
from tools_runtime import create_runtime

REMOTE_SCRIPT_PATH = "/tmp/batch_rename_mount_points.php"
REMOTE_CONFIG_PATH = "/tmp/rename_mount_config.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="批次重新命名 Nextcloud 外部儲存掛載點 (Docker / Kubernetes)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例:
  # 1. 乾跑：統計將變更的掛載數
  uv run python run_rename_mount_points.py -c chad-nextcloud-1

  # 2. 小量實測
  uv run python run_rename_mount_points.py -c chad-nextcloud-1 --limit 10 --apply

  # 3. 全量執行（建議離峰時段，約數分鐘）
  uv run python run_rename_mount_points.py -c chad-nextcloud-1 --apply
        """,
    )

    parser.add_argument(
        "--runtime",
        choices=["docker", "k8s"],
        default="docker",
        help="執行環境（預設 docker）",
    )
    parser.add_argument("--container", "-c", type=str, default=None, help="[docker] 容器名稱")
    parser.add_argument("--namespace", "-n", type=str, default="default", help="[k8s] 命名空間")
    parser.add_argument("--pod", "-p", type=str, default=None, help="[k8s] Pod 名稱")
    parser.add_argument(
        "--label-selector",
        type=str,
        default="app=nextcloud",
        help="[k8s] Pod label（預設 app=nextcloud）",
    )
    parser.add_argument("--apply", action="store_true", help="實際執行（預設為乾跑）")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="最多處理幾筆（0=不限制，用於小量驗證）",
    )
    parser.add_argument(
        "--personal-name",
        type=str,
        default=MOUNT_POINT_PERSONAL,
        help=f"個人掛載顯示名稱（預設 {MOUNT_POINT_PERSONAL}）",
    )
    parser.add_argument(
        "--department-name",
        type=str,
        default=MOUNT_POINT_DEPARTMENT,
        help=f"部門掛載顯示名稱（預設 {MOUNT_POINT_DEPARTMENT}）",
    )
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=500,
        help="每 N 筆輸出進度（預設 500）",
    )
    parser.add_argument("--base", type=str, default=None, help="遠端 base.php 路徑")
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="記錄檔路徑（預設 logs/rename-mount-points.log）",
    )

    args = parser.parse_args()
    dry_run = not args.apply

    log_path = (
        Path(args.log_file) if args.log_file else Path("logs/rename-mount-points.log")
    )
    logger = setup_logger(log_path, logger_name="run_rename_mount_points")

    logger.info(f"=== 執行記錄開始: {datetime.now().isoformat()} ===")
    logger.info(f"模式: {'DRY-RUN（乾跑）' if dry_run else 'APPLY（實際執行）'}")
    logger.info(f"個人: {args.personal_name} | 部門: {args.department_name}")
    if args.limit > 0:
        logger.info(f"限制筆數: {args.limit}")

    runtime = create_runtime(
        args.runtime,
        container=args.container,
        namespace=args.namespace,
        pod=args.pod,
        label_selector=args.label_selector,
    )
    target = runtime.resolve_target(logger)
    if target is None:
        logger.error("❌ 找不到 Nextcloud 容器/Pod，請用 --container 或 --pod 指定")
        return 1
    logger.info(f"目標: {target.label}")

    php_script = Path(__file__).parent / "batch_rename_mount_points.php"
    if not php_script.exists():
        logger.error(f"❌ 找不到 batch_rename_mount_points.php: {php_script}")
        return 1

    batch_cfg = {
        "personal_name": args.personal_name,
        "department_name": args.department_name,
        "backend": "amazons3",
        "dry_run": dry_run,
        "limit": args.limit,
        "progress_interval": args.progress_interval,
    }

    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as tf:
            json.dump(batch_cfg, tf, ensure_ascii=False)
            local_cfg = tf.name

        logger.info("複製腳本與設定到遠端...")
        runtime.copy_file(str(php_script), REMOTE_SCRIPT_PATH)
        runtime.copy_file(local_cfg, REMOTE_CONFIG_PATH)
        runtime.fix_file_permissions([REMOTE_SCRIPT_PATH, REMOTE_CONFIG_PATH], logger)

        logger.info("執行 batch_rename_mount_points.php...")
        exec_env = {"NC_BASE": args.base} if args.base else None
        proc = runtime.exec_run_user(
            ["php", REMOTE_SCRIPT_PATH, REMOTE_CONFIG_PATH],
            env=exec_env,
        )

        summary = None
        error_samples: list[str] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.info(line)
                continue

            if "summary" in row:
                summary = row["summary"]
                continue
            if "progress" in row:
                logger.info(
                    f"進度: {row['progress']} / {row.get('eligible', '?')} 筆"
                )
                continue

            if not row.get("ok", True):
                msg = (
                    f"id={row.get('id')} {row.get('old')} → {row.get('new')}: "
                    f"{row.get('error', 'unknown')}"
                )
                if len(error_samples) < 20:
                    error_samples.append(msg)
                continue

            if row.get("dry_run") and args.limit <= 20:
                logger.info(
                    f"[dry-run] id={row.get('id')} {row.get('old')} → {row.get('new')}"
                )

        if proc.stderr.strip():
            logger.error(f"stderr: {proc.stderr.strip()}")

        if proc.returncode != 0:
            logger.error(f"❌ 遠端腳本結束碼: {proc.returncode}")
            return proc.returncode

        if summary:
            logger.info("=" * 60)
            logger.info("摘要:")
            logger.info(f"  外部儲存總數:     {summary.get('total', 0)}")
            logger.info(f"  符合更名條件:     {summary.get('eligible', 0)}")
            logger.info(f"  已更名/將更名:    {summary.get('updated', 0)}")
            logger.info(f"  已是目標名稱:     {summary.get('already', 0)}")
            logger.info(f"  略過（非 minio）: {summary.get('skipped', 0)}")
            logger.info(f"  衝突:             {summary.get('conflicts', 0)}")
            logger.info(f"  錯誤:             {summary.get('errors', 0)}")
            if error_samples:
                logger.info("錯誤範例（最多 20 筆）:")
                for sample in error_samples:
                    logger.info(f"  - {sample}")

        if dry_run:
            logger.info("")
            logger.info("目前為乾跑。確認無誤後加上 --apply 執行。")
        else:
            logger.info("")
            logger.info("建議登入 1～2 個測試帳號，確認「個人雲端硬碟」「部門雲端硬碟」顯示正確。")

        return 0

    except Exception:
        logger.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
