#!/usr/bin/env python3
"""
External storage mount_point 命名（無第三方依賴）

供 gen_mounts_local_test.py、sync_external_storage.py 等輕量腳本使用，
避免載入 tools_external_storage（需要 httpx）。
"""

from __future__ import annotations

from typing import Any

# 外部儲存掛載點顯示名稱（UI 資料夾名稱，與 bucket 無關）
MOUNT_POINT_PERSONAL = "/個人雲端硬碟"
MOUNT_POINT_DEPARTMENT = "/部門雲端硬碟"

# mount_point 命名模式（可透過 ENV_MOUNT_POINT_STYLE 或 CLI --mount-point-style 覆寫）
MOUNT_POINT_STYLE_DISPLAY = "display"  # 個人→/個人雲端硬碟、部門→/部門雲端硬碟（預設，原行為）
MOUNT_POINT_STYLE_ACCOUNT = "account"  # 資料夾名稱優先取自 bucket（去掉 -filespace），例如 /00073839

DEFAULT_BUCKET_SUFFIX = "-filespace"
ADMIN_USER = "admin"


def account_label_from_bucket(
    bucket: str,
    bucket_suffix: str = DEFAULT_BUCKET_SUFFIX,
) -> str | None:
    """
    從 MinIO bucket 名稱擷取掛載資料夾標籤。

    例：00073839-filespace → 00073839
    K8s + LDAP 時 applicable_users 常為 UUID，應以此為準而非帳號 UID。
    """
    name = (bucket or "").strip()
    if not name:
        return None
    if bucket_suffix and name.endswith(bucket_suffix):
        label = name[: -len(bucket_suffix)].strip()
        return label or None
    return None


def _primary_applicable_account(
    user_id: str,
    applicable_users: list[str] | None = None,
) -> str:
    """從 applicable_users 或 user_id 推得主要帳號名稱（排除 admin）。"""
    if applicable_users:
        for user in applicable_users:
            if user != ADMIN_USER:
                return user
    normalized = user_id.lstrip("/")
    if normalized.startswith("minio-"):
        return normalized.removeprefix("minio-")
    return normalized


def resolve_mount_point(
    user_id: str,
    *,
    style: str = MOUNT_POINT_STYLE_DISPLAY,
    applicable_users: list[str] | None = None,
    bucket: str | None = None,
    bucket_suffix: str = DEFAULT_BUCKET_SUFFIX,
) -> str:
    """
    依 user_id 與命名模式決定 mount_point（UI 資料夾路徑）。

    display（預設，原行為）：
    - minio-DEPT_* → /部門雲端硬碟
    - 其他 minio-* → /個人雲端硬碟
    - 其餘 → /{帳號}

    account（每人一資料夾）：
    - 優先：configuration.bucket 去掉 bucket_suffix（例 00073839-filespace → /00073839）
    - 否則 minio-DEPT_* → /DEPT_{部門名}
    - 否則 → /{applicable_users 帳號}（本機測試如 /chad）
    """
    normalized = user_id.lstrip("/")
    account = _primary_applicable_account(user_id, applicable_users)

    if style == MOUNT_POINT_STYLE_ACCOUNT:
        label = account_label_from_bucket(bucket or "", bucket_suffix)
        if label:
            return f"/{label}"
        if normalized.startswith("minio-DEPT_"):
            return f"/{normalized.removeprefix('minio-')}"
        return f"/{account}"

    if normalized.startswith("minio-DEPT_"):
        return MOUNT_POINT_DEPARTMENT
    if normalized.startswith("minio-"):
        return MOUNT_POINT_PERSONAL
    return f"/{account}"


def resolve_mount_point_display(
    user_id: str,
    applicable_users: list[str] | None = None,
) -> str:
    """相容舊 API：等同 resolve_mount_point(style=display)。"""
    return resolve_mount_point(
        user_id, style=MOUNT_POINT_STYLE_DISPLAY, applicable_users=applicable_users
    )


def apply_mount_point_style_to_mount(
    mount: dict[str, Any],
    style: str,
    bucket_suffix: str = DEFAULT_BUCKET_SUFFIX,
) -> dict[str, Any]:
    """依 style 重算單筆掛載的 mount_point（用於靜態 JSON + account 模式）。"""
    if style != MOUNT_POINT_STYLE_ACCOUNT:
        return mount
    bucket = (mount.get("configuration") or {}).get("bucket", "")
    users = list(mount.get("applicable_users") or [])
    primary = _primary_applicable_account("", users)
    updated = dict(mount)
    updated["mount_point"] = resolve_mount_point(
        primary,
        style=style,
        applicable_users=users,
        bucket=bucket,
        bucket_suffix=bucket_suffix,
    )
    return updated


def apply_mount_point_style_to_mounts(
    mounts: list[dict[str, Any]],
    style: str,
    bucket_suffix: str = DEFAULT_BUCKET_SUFFIX,
) -> list[dict[str, Any]]:
    return [
        apply_mount_point_style_to_mount(m, style, bucket_suffix=bucket_suffix)
        for m in mounts
    ]


def classify_mount_point_rename(
    current_mount_point: str,
    *,
    personal_name: str = MOUNT_POINT_PERSONAL,
    department_name: str = MOUNT_POINT_DEPARTMENT,
) -> str | None:
    """
    將現有 mount_point 對應到新的顯示名稱；已是目標名稱或非 minio-* 時回傳 None（略過）。

    Returns:
        新 mount_point，或 None 表示不需變更
    """
    mount = (
        current_mount_point
        if current_mount_point.startswith("/")
        else f"/{current_mount_point}"
    )
    if mount in (personal_name, department_name):
        return None
    if mount.startswith("/minio-DEPT_"):
        return department_name
    if mount.startswith("/minio-"):
        return personal_name
    return None
