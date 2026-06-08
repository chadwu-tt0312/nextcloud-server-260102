#!/usr/bin/env python3
"""
Nextcloud 批次帳號處理「驅動器」(Docker / Kubernetes)

協調流程（對應需求 Q1 + Q2 + Q3）：
  1. 取得目標帳號（OCS 分頁取全部，或 --users / --users-file 指定）
  2. 產生 config.json，複製 config 與 batch_provision.php 進 Pod/容器
  3. 遠端執行 batch_provision.php（單次 bootstrap、內部迴圈）
       - 清除 skeleton 範本檔（或清空 home）
       - 設定 quota（預設 0 B）
       - 取消「檔案設定 → 其他設定」兩個勾選（預設）
  4. （可選）執行 occ trashbin:cleanup 真正釋放被刪檔案

執行環境：
  --runtime docker   使用 docker cp / docker exec（預設）
  --runtime k8s      使用 kubectl cp / kubectl exec

安全預設：
  - 預設為「乾跑」(dry-run)，必須加 --apply 才會實際變更。
  - 清檔預設只刪 skeleton 白名單檔名，保留使用者自建內容。

版本：1.1.0

用法:
    uv run python run_batch_provision.py --help
    # Docker
    uv run python run_batch_provision.py --users 00059094 -c chad-nextcloud-1 --apply
    # Kubernetes
    uv run python run_batch_provision.py --runtime k8s -n nextcloud -p nextcloud-0 --users 00059094 --apply
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
from tools_runtime import NextcloudRuntime, create_runtime, fetch_all_users_from_occ
from tools_user_admin import fetch_all_users

# fallback 白名單；實際執行時 batch_provision.php 會再掃描 core/skeleton 併入
DEFAULT_SKELETON_NAMES = [
    "Documents",
    "Photos",
    "Templates",
    "Templates credits.md",
    "Nextcloud.png",
    "Nextcloud intro.mp4",
    "Nextcloud Manual.pdf",
    "Reasons to use Nextcloud.pdf",
    "Readme.md",
    "welcome.txt",
]

REMOTE_SCRIPT_PATH = "/tmp/batch_provision.php"
REMOTE_CONFIG_PATH = "/tmp/batch_config.json"

# Q2：檔案設定 → 其他設定（兩個勾選，由外部 app 註冊，非 files UserConfig）
DEFAULT_ADDITIONAL_SETTINGS = {
    "recommendations": {"enabled": "0"},
    "text": {"workspace_enabled": "0"},
}


def _ocs_configured(config: dict[str, str]) -> bool:
    return bool(config["url"] and config["username"] and config["password"])


def resolve_targets(
    args,
    config: dict[str, str],
    runtime: NextcloudRuntime | None,
    logger,
) -> list[str] | None:
    """依參數決定目標帳號清單。"""
    if args.users:
        return [u.strip() for u in args.users.split(",") if u.strip()]
    if args.users_file:
        path = Path(args.users_file)
        if not path.exists():
            logger.error(f"❌ users-file 不存在: {path}")
            return None
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # --all：優先 OCS（大量分頁較穩），無 ENV 時改 occ user:list
    if args.user_source == "ocs":
        if not _ocs_configured(config):
            logger.error("❌ --user-source ocs 需要 ENV_NEXTCLOUD_URL / USER / PASSWORD")
            return None
        success, message, users = fetch_all_users(
            config["url"], config["username"], config["password"], logger=logger
        )
    elif args.user_source == "occ":
        if runtime is None:
            logger.error("❌ --user-source occ 需要先解析 Docker/K8s 目標")
            return None
        success, message, users = fetch_all_users_from_occ(runtime, logger)
    else:  # auto
        if _ocs_configured(config):
            logger.info("使用者來源: OCS API（已設定 ENV_NEXTCLOUD_*）")
            success, message, users = fetch_all_users(
                config["url"], config["username"], config["password"], logger=logger
            )
        elif runtime is not None:
            logger.info("使用者來源: occ user:list（未設定 ENV_NEXTCLOUD_*，改用容器內 occ）")
            success, message, users = fetch_all_users_from_occ(runtime, logger)
        else:
            logger.error(
                "❌ --all 需要 ENV_NEXTCLOUD_URL/USER/PASSWORD，"
                "或可連線的 Docker/K8s 目標以使用 occ user:list"
            )
            return None

    if not success:
        logger.error(f"❌ 無法取得使用者列表: {message}")
        return None
    return users


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nextcloud 批次帳號處理驅動器 (Docker / Kubernetes)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # 執行環境
    parser.add_argument(
        "--runtime",
        choices=["docker", "k8s"],
        default="docker",
        help="執行環境：docker（預設）或 k8s",
    )
    parser.add_argument("--container", "-c", type=str, default=None, help="[docker] 容器名稱")
    parser.add_argument("--namespace", "-n", type=str, default="default", help="[k8s] 命名空間")
    parser.add_argument("--pod", "-p", type=str, default=None, help="[k8s] Pod 名稱")
    parser.add_argument(
        "--label-selector",
        type=str,
        default="app=nextcloud",
        help="[k8s] 自動偵測 Pod 用的 label（預設 app=nextcloud）",
    )

    # 目標帳號（三選一）
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="處理所有帳號（OCS 或 occ 取得）")
    parser.add_argument(
        "--user-source",
        choices=["auto", "ocs", "occ"],
        default="auto",
        help="--all 時的使用者來源：auto=有 ENV 用 OCS 否則 occ（預設）",
    )
    group.add_argument("--users", type=str, help="指定帳號（逗號分隔）")
    group.add_argument("--users-file", type=str, help="從檔案讀取帳號（每行一個）")

    # 動作
    parser.add_argument(
        "--clear-files",
        choices=["skeleton", "all", "none"],
        default="skeleton",
        help="清檔模式：skeleton=只刪範本白名單(預設) / all=清空整個 home / none=不刪",
    )
    parser.add_argument("--quota", type=str, default="0 B", help='設定 quota（預設 "0 B"）')
    parser.add_argument("--no-quota", action="store_true", help="不變更 quota")
    parser.add_argument(
        "--skip-additional-settings",
        action="store_true",
        help="跳過 Q2（不取消「其他設定」兩個勾選）",
    )
    parser.add_argument(
        "--exclude",
        type=str,
        default="admin",
        help="--all 時排除的帳號（逗號分隔，預設 admin）",
    )
    parser.add_argument(
        "--no-exclude",
        action="store_true",
        help="--all 時不排除任何帳號（含 admin）",
    )

    # 執行控制
    parser.add_argument("--apply", action="store_true", help="實際執行（未指定時為乾跑 dry-run）")
    parser.add_argument("--cleanup-trash", action="store_true", help="完成後執行 occ trashbin:cleanup")
    parser.add_argument("--base", type=str, default=None, help="遠端 base.php 路徑（預設 /var/www/html/lib/base.php）")
    parser.add_argument("--log-file", type=str, default=None, help="記錄檔案路徑（預設 logs/batch-provision.log）")

    args = parser.parse_args()

    log_path = Path(args.log_file) if args.log_file else Path("logs/batch-provision.log")
    logger = setup_logger(log_path, logger_name="run_batch_provision")
    dry_run = not args.apply

    config = get_nextcloud_config()

    logger.info(f"=== 執行記錄開始: {datetime.now().isoformat()} ===")
    logger.info(f"模式: {'DRY-RUN（乾跑）' if dry_run else 'APPLY（實際執行）'}")
    logger.info(f"runtime: {args.runtime}")

    runtime = create_runtime(
        args.runtime,
        container=args.container,
        namespace=args.namespace,
        pod=args.pod,
        label_selector=args.label_selector,
    )
    target = runtime.resolve_target(logger)
    if target is None:
        if args.runtime == "docker":
            logger.error("❌ 找不到 Nextcloud 容器，請用 --container 指定")
        return 1
    logger.info(f"目標: {target.label}")

    targets = resolve_targets(args, config, runtime, logger)
    if targets is None:
        return 1

    # --all 預設排除 admin（可用 --no-exclude 或 --exclude 自訂）
    if args.all and not args.no_exclude:
        exclude = {u.strip() for u in args.exclude.split(",") if u.strip()}
        if exclude:
            before = len(targets)
            targets = [uid for uid in targets if uid not in exclude]
            skipped = before - len(targets)
            if skipped:
                logger.info(f"排除 {skipped} 位: {sorted(exclude)}")

    if not targets:
        logger.warning("⚠️  沒有要處理的帳號")
        return 0
    logger.info(f"目標帳號數: {len(targets)}")

    user_settings = {} if args.skip_additional_settings else DEFAULT_ADDITIONAL_SETTINGS
    batch_cfg = {
        "users": targets,
        "clear_files": args.clear_files,
        "skeleton_names": DEFAULT_SKELETON_NAMES,
        "set_quota": None if args.no_quota else args.quota,
        "user_settings": user_settings,
        "dry_run": dry_run,
    }
    logger.info(f"清檔模式: {args.clear_files}")
    logger.info(f"quota: {'(不變更)' if args.no_quota else args.quota}")
    logger.info(f"其他設定: {user_settings if user_settings else '(跳過)'}")
    logger.info("=" * 60)

    php_script = Path(__file__).parent / "batch_provision.php"
    if not php_script.exists():
        logger.error(f"❌ 找不到 batch_provision.php: {php_script}")
        return 1

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

        logger.info("執行 batch_provision.php...")
        exec_env = {"NC_BASE": args.base} if args.base else None
        proc = runtime.exec_run_user(
            ["php", REMOTE_SCRIPT_PATH, REMOTE_CONFIG_PATH],
            env=exec_env,
        )

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
                remaining = row.get("remaining")
                extra = f" | 殘留 {remaining}" if remaining else ""
                logger.info(
                    f"✓ {row.get('uid')} | 刪除 {len(row.get('deleted', []))} 項"
                    f" | quota={row.get('quota')} | user_settings={row.get('user_settings')}{extra}"
                )

        if proc.stderr.strip():
            logger.error(f"stderr: {proc.stderr.strip()}")

        logger.info("=" * 60)
        if summary:
            logger.info(f"✅ 完成：{summary}")
        else:
            logger.warning("⚠️  未取得 summary，請檢查上方輸出")

        if args.cleanup_trash and not dry_run:
            logger.info("執行 occ trashbin:cleanup...")
            cp = runtime.exec_run_user(["php", "occ", "trashbin:cleanup", *targets])
            logger.info(cp.stdout.strip() or "trashbin:cleanup 完成")
            if cp.stderr.strip():
                logger.error(cp.stderr.strip())

        try:
            Path(local_cfg).unlink(missing_ok=True)
        except OSError:
            pass

        if dry_run:
            logger.info("ℹ️  這是乾跑模式，未實際變更。加上 --apply 才會執行。")

        logger.info(f"=== 執行記錄結束: {datetime.now().isoformat()} ===")
        return proc.returncode

    except subprocess.CalledProcessError as e:
        logger.error(f"❌ 遠端指令執行失敗: {e}")
        return 1
    except Exception as e:
        logger.error(f"❌ 發生錯誤: {e}")
        logger.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
