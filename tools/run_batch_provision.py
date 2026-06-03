#!/usr/bin/env python3
"""
Nextcloud 批次帳號處理「驅動器」(Docker)

協調流程（對應需求 Q1 + Q2 + Q3）：
  1. 取得目標帳號（OCS 分頁取全部，或 --users / --users-file 指定）
  2. 產生 config.json，docker cp config 與 batch_provision.php 進容器
  3. docker exec 執行 batch_provision.php（單次 bootstrap、內部迴圈）
       - 清除 skeleton 範本檔（或清空 home）
       - 設定 quota（預設 0 B）
       - 設定 files 偏好（取消「其他設定」勾選）
  4. （可選）執行 occ trashbin:cleanup 真正釋放被刪檔案

安全預設：
  - 預設為「乾跑」(dry-run)，必須加 --apply 才會實際變更。
  - 清檔預設只刪 skeleton 白名單檔名，保留使用者自建內容。

版本：1.0.0

用法:
    python run_batch_provision.py --help
    # 乾跑：對所有帳號清 skeleton + quota=0 + 取消兩個勾選
    python run_batch_provision.py --all --keys folder_tree,show_mime_column
    # 正式執行
    python run_batch_provision.py --all --keys folder_tree,show_mime_column --apply
    # 只處理指定帳號
    python run_batch_provision.py --users minio-DEPT_A,minio-DEPT_B --apply
"""

import argparse
import json
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

from tools_external_storage import get_nextcloud_config, setup_logger
from tools_user_admin import fetch_all_users

# 預設 skeleton 頂層項目（core/skeleton 內的示範檔/資料夾名稱）
DEFAULT_SKELETON_NAMES = [
    "Documents",
    "Photos",
    "Templates",
    "Nextcloud.png",
    "Nextcloud intro.mp4",
    "Nextcloud Manual.pdf",
    "Reasons to use Nextcloud.pdf",
    "Readme.md",
]

REMOTE_SCRIPT_PATH = "/tmp/batch_provision.php"
REMOTE_CONFIG_PATH = "/tmp/batch_config.json"


def detect_container(logger) -> str | None:
    """自動偵測含 nextcloud 的容器名稱。"""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error(f"❌ 無法執行 docker ps: {e}")
        return None

    for name in result.stdout.splitlines():
        if "nextcloud" in name.lower():
            return name.strip()
    return None


def resolve_targets(args, config, logger) -> list[str] | None:
    """依參數決定目標帳號清單。"""
    if args.users:
        return [u.strip() for u in args.users.split(",") if u.strip()]
    if args.users_file:
        path = Path(args.users_file)
        if not path.exists():
            logger.error(f"❌ users-file 不存在: {path}")
            return None
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    # --all
    success, message, users = fetch_all_users(
        config["url"], config["username"], config["password"], logger=logger
    )
    if not success:
        logger.error(f"❌ 無法取得使用者列表: {message}")
        return None
    return users


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nextcloud 批次帳號處理驅動器 (Docker)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # 目標帳號（三選一）
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="處理所有帳號（OCS 分頁取得）")
    group.add_argument("--users", type=str, help="指定帳號（逗號分隔）")
    group.add_argument("--users-file", type=str, help="從檔案讀取帳號（每行一個）")

    # 動作
    parser.add_argument(
        "--clear-files",
        choices=["skeleton", "all", "none"],
        default="skeleton",
        help="清檔模式：skeleton=只刪範本白名單(預設) / all=清空整個 home / none=不刪",
    )
    parser.add_argument(
        "--quota",
        type=str,
        default="0 B",
        help='設定 quota（預設 "0 B"）',
    )
    parser.add_argument("--no-quota", action="store_true", help="不變更 quota")
    parser.add_argument(
        "--keys",
        type=str,
        default="",
        help="要設定的 files 偏好 key（逗號分隔），例如 folder_tree,show_mime_column",
    )
    parser.add_argument("--value", type=str, default="0", help="偏好設定值（預設 0=取消勾選）")

    # 執行控制
    parser.add_argument("--apply", action="store_true", help="實際執行（未指定時為乾跑 dry-run）")
    parser.add_argument("--cleanup-trash", action="store_true", help="完成後執行 occ trashbin:cleanup")
    parser.add_argument("--container", "-c", type=str, default=None, help="容器名稱（預設自動偵測）")
    parser.add_argument("--base", type=str, default=None, help="容器內 base.php 路徑（預設 /var/www/html/lib/base.php）")
    parser.add_argument("--log-file", type=str, default=None, help="記錄檔案路徑（預設 logs/batch-provision.log）")

    args = parser.parse_args()

    log_path = Path(args.log_file) if args.log_file else Path("logs/batch-provision.log")
    logger = setup_logger(log_path, logger_name="run_batch_provision")
    dry_run = not args.apply

    # 取得帳號（--all 才需要 OCS 連線設定）
    config = get_nextcloud_config()
    if args.all and not (config["url"] and config["username"] and config["password"]):
        logger.error("❌ --all 需要環境變數 ENV_NEXTCLOUD_URL / USER / PASSWORD")
        return 1

    logger.info(f"=== 執行記錄開始: {datetime.now().isoformat()} ===")
    logger.info(f"模式: {'DRY-RUN（乾跑）' if dry_run else 'APPLY（實際執行）'}")

    # 1. 容器
    container = args.container or detect_container(logger)
    if not container:
        logger.error("❌ 找不到 Nextcloud 容器，請用 --container 指定")
        return 1
    logger.info(f"容器: {container}")

    # 2. 目標帳號
    targets = resolve_targets(args, config, logger)
    if targets is None:
        return 1
    if not targets:
        logger.warning("⚠️  沒有要處理的帳號")
        return 0
    logger.info(f"目標帳號數: {len(targets)}")

    # 3. 組裝 config
    keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    prefs = {k: args.value for k in keys}
    batch_cfg = {
        "users": targets,
        "clear_files": args.clear_files,
        "skeleton_names": DEFAULT_SKELETON_NAMES,
        "set_quota": None if args.no_quota else args.quota,
        "prefs": prefs,
        "dry_run": dry_run,
    }
    logger.info(f"清檔模式: {args.clear_files}")
    logger.info(f"quota: {'(不變更)' if args.no_quota else args.quota}")
    logger.info(f"偏好設定: {prefs if prefs else '(無)'}")
    logger.info("=" * 60)

    php_script = Path(__file__).parent / "batch_provision.php"
    if not php_script.exists():
        logger.error(f"❌ 找不到 batch_provision.php: {php_script}")
        return 1

    try:
        # 4. 寫出 config 到本地暫存檔
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as tf:
            json.dump(batch_cfg, tf, ensure_ascii=False)
            local_cfg = tf.name

        # 5. docker cp 腳本與 config 進容器
        logger.info("複製腳本與設定進容器...")
        subprocess.run(["docker", "cp", str(php_script), f"{container}:{REMOTE_SCRIPT_PATH}"], check=True)
        subprocess.run(["docker", "cp", local_cfg, f"{container}:{REMOTE_CONFIG_PATH}"], check=True)

        # 6. 執行 batch_provision.php
        logger.info("執行 batch_provision.php...")
        exec_cmd = ["docker", "exec", "-u", "www-data"]
        if args.base:
            exec_cmd += ["-e", f"NC_BASE={args.base}"]
        exec_cmd += [container, "php", REMOTE_SCRIPT_PATH, REMOTE_CONFIG_PATH]

        proc = subprocess.run(exec_cmd, capture_output=True, text=True)

        summary = None
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
            if not row.get("ok", True):
                logger.error(f"❌ {row.get('uid')}: {row.get('errors')}")
            else:
                logger.info(
                    f"✓ {row.get('uid')} | 刪除 {len(row.get('deleted', []))} 項"
                    f" | quota={row.get('quota')} | prefs={row.get('prefs')}"
                )

        if proc.stderr.strip():
            logger.error(f"stderr: {proc.stderr.strip()}")

        logger.info("=" * 60)
        if summary:
            logger.info(f"✅ 完成：{summary}")
        else:
            logger.warning("⚠️  未取得 summary，請檢查上方輸出")

        # 7. 清垃圾桶
        if args.cleanup_trash and not dry_run:
            logger.info("執行 occ trashbin:cleanup...")
            # 一次帶入多個帳號 = 單次 bootstrap
            cleanup_cmd = ["docker", "exec", "-u", "www-data", container, "php", "occ", "trashbin:cleanup", *targets]
            cp = subprocess.run(cleanup_cmd, capture_output=True, text=True)
            logger.info(cp.stdout.strip() or "trashbin:cleanup 完成")
            if cp.stderr.strip():
                logger.error(cp.stderr.strip())

        # 清理本地暫存
        try:
            Path(local_cfg).unlink(missing_ok=True)
        except OSError:
            pass

        if dry_run:
            logger.info("ℹ️  這是乾跑模式，未實際變更。加上 --apply 才會執行。")

        logger.info(f"=== 執行記錄結束: {datetime.now().isoformat()} ===")
        return proc.returncode

    except subprocess.CalledProcessError as e:
        logger.error(f"❌ 指令執行失敗: {e}\n{e.stderr if hasattr(e, 'stderr') else ''}")
        return 1
    except Exception as e:
        logger.error(f"❌ 發生錯誤: {e}")
        logger.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
