#!/usr/bin/env python3
"""
Nextcloud External Storage Upsert Tool

以 occ 匯出現有掛載，比對 mounts.json 後：
- 已存在：更新 configuration / options / applicable_users
- 不存在：files_external:import 新增
- 重複：保留最舊 mount_id，刪除其餘同 key 項目

版本：1.0.0
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

from tools_runtime import NextcloudRuntime, create_runtime

ADMIN_USER = "admin"
DEFAULT_SCAN_AFTER_SYNC = True


def _primary_users(users: list[str]) -> frozenset[str]:
    return frozenset(u for u in users if u != ADMIN_USER)


def mount_match_key(mount: dict[str, Any]) -> tuple[str, frozenset[str]] | None:
    users = _primary_users(mount.get("applicable_users", []))
    mount_point = mount.get("mount_point")
    if not mount_point or not users:
        return None
    return mount_point, users


def bucket_match_key(mount: dict[str, Any]) -> tuple[str, frozenset[str]] | None:
    bucket = (mount.get("configuration") or {}).get("bucket", "").lower()
    users = _primary_users(mount.get("applicable_users", []))
    if not bucket or not users:
        return None
    return bucket, users


def build_indexes(
    existing: list[dict[str, Any]],
) -> tuple[
    dict[tuple[str, frozenset[str]], list[dict[str, Any]]],
    dict[tuple[str, frozenset[str]], list[dict[str, Any]]],
    dict[int, dict[str, Any]],
]:
    by_mount_key: dict[tuple[str, frozenset[str]], list[dict[str, Any]]] = {}
    by_bucket_key: dict[tuple[str, frozenset[str]], list[dict[str, Any]]] = {}
    by_id: dict[int, dict[str, Any]] = {}

    for mount in existing:
        mount_id = int(mount["mount_id"])
        by_id[mount_id] = mount
        mk = mount_match_key(mount)
        if mk:
            by_mount_key.setdefault(mk, []).append(mount)
        bk = bucket_match_key(mount)
        if bk:
            by_bucket_key.setdefault(bk, []).append(mount)

    for mapping in (by_mount_key, by_bucket_key):
        for key in mapping:
            mapping[key].sort(key=lambda m: int(m["mount_id"]))

    return by_mount_key, by_bucket_key, by_id


def find_existing_mount(
    desired: dict[str, Any],
    by_mount_key: dict[tuple[str, frozenset[str]], list[dict[str, Any]]],
    by_bucket_key: dict[tuple[str, frozenset[str]], list[dict[str, Any]]],
    by_id: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    desired_id = desired.get("mount_id")
    if isinstance(desired_id, int) and desired_id in by_id:
        return by_id[desired_id]

    mk = mount_match_key(desired)
    if mk and mk in by_mount_key:
        return by_mount_key[mk][0]

    bk = bucket_match_key(desired)
    if bk and bk in by_bucket_key:
        return by_bucket_key[bk][0]

    return None


def find_duplicates(
    canonical: dict[str, Any],
    by_mount_key: dict[tuple[str, frozenset[str]], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    mk = mount_match_key(canonical)
    if not mk or mk not in by_mount_key:
        return []
    canonical_id = int(canonical["mount_id"])
    return [m for m in by_mount_key[mk] if int(m["mount_id"]) != canonical_id]


def occ_encode(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    if not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return value


def run_occ(
    runtime: NextcloudRuntime,
    args: list[str],
    logger: logging.Logger,
    *,
    dry_run: bool,
) -> bool:
    cmd = ["php", "occ", *args]
    display = " ".join(args)
    if dry_run:
        logger.info(f"  (dry-run) occ {display}")
        return True

    proc = runtime.exec_run_user(cmd)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "occ 失敗").strip()
        logger.error(f"  ❌ occ {display}\n{err}")
        return False
    return True


def update_mount(
    runtime: NextcloudRuntime,
    existing: dict[str, Any],
    desired: dict[str, Any],
    logger: logging.Logger,
    *,
    dry_run: bool,
) -> bool:
    mount_id = int(existing["mount_id"])
    mount_point = desired.get("mount_point", "")
    logger.info(f"  更新 mount_id={mount_id} ({mount_point})")

    existing_cfg = existing.get("configuration") or {}
    desired_cfg = desired.get("configuration") or {}
    for key, value in desired_cfg.items():
        if existing_cfg.get(key) != value:
            if not run_occ(
                runtime,
                ["files_external:config", str(mount_id), key, occ_encode(value)],
                logger,
                dry_run=dry_run,
            ):
                return False

    existing_opts = existing.get("options") or {}
    desired_opts = desired.get("options") or {}
    for key, value in desired_opts.items():
        if existing_opts.get(key) != value:
            if not run_occ(
                runtime,
                ["files_external:option", str(mount_id), key, occ_encode(value)],
                logger,
                dry_run=dry_run,
            ):
                return False

    desired_users = list(desired.get("applicable_users") or [])
    existing_users = list(existing.get("applicable_users") or [])
    to_add = [u for u in desired_users if u not in existing_users]
    to_remove = [u for u in existing_users if u not in desired_users]

    if to_add or to_remove:
        args = ["files_external:applicable", str(mount_id)]
        for user in to_add:
            args += ["--add-user", user]
        for user in to_remove:
            args += ["--remove-user", user]
        if not run_occ(runtime, args, logger, dry_run=dry_run):
            return False

    return True


def delete_mount(
    runtime: NextcloudRuntime,
    mount: dict[str, Any],
    logger: logging.Logger,
    *,
    dry_run: bool,
) -> bool:
    mount_id = int(mount["mount_id"])
    mount_point = mount.get("mount_point", "")
    logger.info(f"  刪除重複 mount_id={mount_id} ({mount_point})")
    return run_occ(
        runtime,
        ["files_external:delete", "--yes", str(mount_id)],
        logger,
        dry_run=dry_run,
    )


def import_mount(
    runtime: NextcloudRuntime,
    mount: dict[str, Any],
    logger: logging.Logger,
    *,
    dry_run: bool,
) -> bool:
    mount_point = mount.get("mount_point", "")
    logger.info(f"  新增掛載 ({mount_point})")

    if dry_run:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump([mount], tmp, ensure_ascii=False)
            remote_path = f"/tmp/{Path(tmp.name).name}"
        runtime.copy_file(tmp.name, remote_path)
        ok = run_occ(
            runtime,
            ["files_external:import", "--dry", remote_path],
            logger,
            dry_run=False,
        )
        runtime.exec_as_user("root", ["rm", "-f", remote_path])
        Path(tmp.name).unlink(missing_ok=True)
        return ok

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump([mount], tmp, ensure_ascii=False)
        local_path = tmp.name
        remote_path = f"/tmp/{Path(local_path).name}"

    try:
        runtime.copy_file(local_path, remote_path)
        if not run_occ(
            runtime,
            ["files_external:import", remote_path],
            logger,
            dry_run=False,
        ):
            return False
        runtime.exec_as_user("root", ["rm", "-f", remote_path])
        return True
    finally:
        Path(local_path).unlink(missing_ok=True)


def scan_mount(
    runtime: NextcloudRuntime,
    mount_id: int,
    logger: logging.Logger,
    *,
    dry_run: bool,
) -> bool:
    logger.info(f"  掃描 mount_id={mount_id}")
    return run_occ(
        runtime,
        ["files_external:scan", str(mount_id)],
        logger,
        dry_run=dry_run,
    )


def export_existing(
    runtime: NextcloudRuntime,
    logger: logging.Logger,
) -> tuple[bool, list[dict[str, Any]]]:
    proc = runtime.exec_run_user(["php", "occ", "files_external:export"])
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "occ export 失敗").strip()
        logger.error(err)
        return False, []
    try:
        data = json.loads(proc.stdout)
        if not isinstance(data, list):
            return False, []
        return True, data
    except json.JSONDecodeError as e:
        logger.error(f"export JSON 解析失敗: {e}")
        return False, []


def sync_mounts(
    runtime: NextcloudRuntime,
    desired_mounts: list[dict[str, Any]],
    logger: logging.Logger,
    *,
    dry_run: bool,
    scan_after: bool,
) -> dict[str, int]:
    stats = {"updated": 0, "created": 0, "deleted": 0, "scanned": 0, "failed": 0, "skipped": 0}

    ok, existing = export_existing(runtime, logger)
    if not ok:
        stats["failed"] = len(desired_mounts)
        return stats

    by_mount_key, by_bucket_key, by_id = build_indexes(existing)
    scanned_ids: set[int] = set()

    for i, desired in enumerate(desired_mounts, start=1):
        mount_point = desired.get("mount_point", "")
        logger.info(f"[{i}/{len(desired_mounts)}] 處理 {mount_point}")

        match = find_existing_mount(desired, by_mount_key, by_bucket_key, by_id)
        if match:
            if not update_mount(runtime, match, desired, logger, dry_run=dry_run):
                stats["failed"] += 1
                continue
            stats["updated"] += 1

            for dup in find_duplicates(match, by_mount_key):
                if delete_mount(runtime, dup, logger, dry_run=dry_run):
                    stats["deleted"] += 1
                else:
                    stats["failed"] += 1

            if scan_after:
                mount_id = int(match["mount_id"])
                if mount_id not in scanned_ids:
                    if scan_mount(runtime, mount_id, logger, dry_run=dry_run):
                        stats["scanned"] += 1
                        scanned_ids.add(mount_id)
                    else:
                        stats["failed"] += 1
        else:
            if import_mount(runtime, desired, logger, dry_run=dry_run):
                stats["created"] += 1
            else:
                stats["failed"] += 1

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upsert Nextcloud external storage mounts (update existing + create new)",
    )
    parser.add_argument("mounts_file", help="mounts.json 路徑")
    parser.add_argument("--runtime", choices=["docker", "k8s"], default="docker")
    parser.add_argument("-c", "--container", help="Docker 容器名稱")
    parser.add_argument("-n", "--namespace", default="default", help="K8s namespace")
    parser.add_argument("-p", "--pod", help="K8s Pod 名稱")
    parser.add_argument("--dry-run", action="store_true", help="僅預覽變更")
    parser.add_argument("--no-scan", action="store_true", help="同步後不執行 files_external:scan")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )
    logger = logging.getLogger("sync_external_storage")

    mounts_path = Path(args.mounts_file)
    if not mounts_path.is_file():
        logger.error(f"❌ 檔案不存在: {mounts_path}")
        return 1

    with open(mounts_path, encoding="utf-8") as f:
        desired = json.load(f)
    if not isinstance(desired, list):
        logger.error("❌ mounts.json 必須是陣列")
        return 1

    runtime = create_runtime(
        args.runtime,
        container=args.container,
        namespace=args.namespace,
        pod=args.pod,
    )
    target = runtime.resolve_target(logger)
    if not target:
        return 1

    logger.info(f"目標: {target.label}")
    logger.info(f"掛載數量: {len(desired)}")
    if args.dry_run:
        logger.info("模式: dry-run（不寫入）")
    logger.info("=" * 60)

    try:
        stats = sync_mounts(
            runtime,
            desired,
            logger,
            dry_run=args.dry_run,
            scan_after=not args.no_scan,
        )
    except Exception as e:
        logger.error(f"❌ 發生錯誤: {e}")
        logger.error(traceback.format_exc())
        return 1

    logger.info("")
    logger.info("=" * 60)
    logger.info("同步完成")
    logger.info(f"  更新: {stats['updated']}")
    logger.info(f"  新增: {stats['created']}")
    logger.info(f"  刪除重複: {stats['deleted']}")
    logger.info(f"  掃描: {stats['scanned']}")
    logger.info(f"  失敗: {stats['failed']}")

    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
