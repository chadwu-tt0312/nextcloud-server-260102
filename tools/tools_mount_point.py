#!/usr/bin/env python3
"""
External storage mount_point 命名（無第三方依賴）

命名規則（display 模式）：
  - 個人：/個人-{帳號}（每位使用者最多 1 個）
  - 部門：/部門-{標籤}（每位使用者可 0～n 個，標籤取自 minio-DEPT_ 後綴，- 轉 _）

例：
  /minio-00059094           → /個人-00059094
  /minio-DEPT_SDD-ARC5      → /部門-SDD_ARC5
  /minio-DEPT_SDD-ARC5-ENG1 → /部門-SDD_ARC5_ENG1
"""

from __future__ import annotations

from typing import Any

# 舊版固定名稱（相容用）
MOUNT_POINT_PERSONAL = "/個人雲端硬碟"
MOUNT_POINT_DEPARTMENT = "/部門雲端硬碟"

MOUNT_POINT_PREFIX_PERSONAL = "個人-"
MOUNT_POINT_PREFIX_DEPARTMENT = "部門-"

MOUNT_POINT_STYLE_DISPLAY = "display"  # /個人-{帳號}、/部門-{標籤}
MOUNT_POINT_STYLE_ACCOUNT = "account"  # 資料夾名稱優先取自 bucket

DEFAULT_BUCKET_SUFFIX = "-filespace"
ADMIN_USER = "admin"


def account_label_from_bucket(
    bucket: str,
    bucket_suffix: str = DEFAULT_BUCKET_SUFFIX,
) -> str | None:
    """00059094-filespace → 00059094"""
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
    if applicable_users:
        for user in applicable_users:
            if user != ADMIN_USER:
                return user
    normalized = user_id.lstrip("/")
    if normalized.startswith("minio-"):
        return normalized.removeprefix("minio-")
    return normalized


def _normalize_mount_point(mount_point: str) -> str:
    mp = (mount_point or "").strip()
    return mp if mp.startswith("/") else f"/{mp}"


def is_labeled_personal_mount(mount_point: str) -> bool:
    return _normalize_mount_point(mount_point).startswith(f"/{MOUNT_POINT_PREFIX_PERSONAL}")


def is_labeled_department_mount(mount_point: str) -> bool:
    return _normalize_mount_point(mount_point).startswith(f"/{MOUNT_POINT_PREFIX_DEPARTMENT}")


def is_labeled_mount(mount_point: str) -> bool:
    return is_labeled_personal_mount(mount_point) or is_labeled_department_mount(mount_point)


def personal_label_from_mount(
    mount_point: str,
    *,
    bucket: str | None = None,
    applicable_users: list[str] | None = None,
    bucket_suffix: str = DEFAULT_BUCKET_SUFFIX,
) -> str | None:
    """從 bucket 或 minio-{帳號} 掛載點擷取個人標籤。"""
    label = account_label_from_bucket(bucket or "", bucket_suffix)
    if label:
        return label
    normalized = mount_point.lstrip("/")
    if normalized.startswith("minio-") and not normalized.startswith("minio-DEPT_"):
        return normalized.removeprefix("minio-")
    return None


def department_label_from_bucket(
    bucket: str,
    bucket_suffix: str = DEFAULT_BUCKET_SUFFIX,
) -> str | None:
    """
    從部門 bucket 擷取掛載標籤。

    例：dep-sdd-arc5-eng1-filespace → SDD_ARC5_ENG1
    """
    name = account_label_from_bucket(bucket or "", bucket_suffix)
    if not name:
        return None
    lower = name.lower()
    for prefix in ("dept-", "dept_", "dep-", "dep_"):
        if lower.startswith(prefix):
            name = name[len(prefix):]
            break
    label = name.replace("-", "_").replace(".", "_").strip("_")
    return label.upper() if label else None


def is_generic_personal_mount(mount_point: str) -> bool:
    return _normalize_mount_point(mount_point) == MOUNT_POINT_PERSONAL


def is_generic_department_mount(mount_point: str) -> bool:
    return _normalize_mount_point(mount_point) == MOUNT_POINT_DEPARTMENT


def department_label_from_mount(
    mount_point: str,
    applicable_groups: list[str] | None = None,
) -> str | None:
    """minio-DEPT_SDD-ARC5-ENG1 → SDD_ARC5_ENG1"""
    normalized = mount_point.lstrip("/")
    if normalized.startswith("minio-DEPT_"):
        return normalized.removeprefix("minio-DEPT_").replace("-", "_")
    if applicable_groups:
        for group in applicable_groups:
            gl = group.lower()
            if gl.startswith("dept_"):
                return group[5:].replace("-", "_")
    return None


def format_personal_mount_point(label: str) -> str:
    return f"/{MOUNT_POINT_PREFIX_PERSONAL}{label}"


def format_department_mount_point(label: str) -> str:
    return f"/{MOUNT_POINT_PREFIX_DEPARTMENT}{label.upper()}"


def resolve_mount_point(
    user_id: str,
    *,
    style: str = MOUNT_POINT_STYLE_DISPLAY,
    applicable_users: list[str] | None = None,
    applicable_groups: list[str] | None = None,
    bucket: str | None = None,
    bucket_suffix: str = DEFAULT_BUCKET_SUFFIX,
) -> str:
    """
    依 user_id 與命名模式決定 mount_point。

    display：
    - minio-DEPT_* → /部門-{標籤}
    - 其他 minio-* → /個人-{帳號}
    - 其餘 → /{帳號}

    account：優先 bucket 去掉 suffix
    """
    normalized = user_id.lstrip("/")
    account = _primary_applicable_account(user_id, applicable_users)

    if style == MOUNT_POINT_STYLE_ACCOUNT:
        label = account_label_from_bucket(bucket or "", bucket_suffix)
        if label:
            return f"/{label}"
        if normalized.startswith("minio-DEPT_"):
            dept = department_label_from_mount(f"/{normalized}", applicable_groups)
            if dept:
                return format_department_mount_point(dept)
            return f"/{normalized.removeprefix('minio-')}"
        return f"/{account}"

    if normalized.startswith("minio-DEPT_"):
        dept = department_label_from_mount(f"/{normalized}", applicable_groups)
        if dept:
            return format_department_mount_point(dept)
        return format_department_mount_point(normalized.removeprefix("minio-DEPT_").replace("-", "_"))

    if normalized.startswith("minio-"):
        label = personal_label_from_mount(
            f"/{normalized}",
            bucket=bucket,
            applicable_users=applicable_users,
            bucket_suffix=bucket_suffix,
        )
        if label:
            return format_personal_mount_point(label)
        return format_personal_mount_point(account)

    return f"/{account}"


def resolve_mount_point_display(
    user_id: str,
    applicable_users: list[str] | None = None,
) -> str:
    return resolve_mount_point(
        user_id, style=MOUNT_POINT_STYLE_DISPLAY, applicable_users=applicable_users
    )


def apply_mount_point_style_to_mount(
    mount: dict[str, Any],
    style: str,
    bucket_suffix: str = DEFAULT_BUCKET_SUFFIX,
) -> dict[str, Any]:
    if style != MOUNT_POINT_STYLE_ACCOUNT:
        bucket = (mount.get("configuration") or {}).get("bucket", "")
        users = list(mount.get("applicable_users") or [])
        groups = list(mount.get("applicable_groups") or [])
        old_mp = mount.get("mount_point", "")
        user_id = old_mp.lstrip("/") if old_mp else _primary_applicable_account("", users)
        if not user_id.startswith("minio-"):
            user_id = f"minio-{_primary_applicable_account(user_id, users)}"
        updated = dict(mount)
        updated["mount_point"] = resolve_mount_point(
            user_id,
            style=MOUNT_POINT_STYLE_DISPLAY,
            applicable_users=users,
            applicable_groups=groups,
            bucket=bucket,
            bucket_suffix=bucket_suffix,
        )
        return updated
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


def mount_applicable_identities(mount: dict[str, Any]) -> list[str]:
    users = [u for u in (mount.get("applicable_users") or []) if u != ADMIN_USER]
    if users:
        return users
    groups = list(mount.get("applicable_groups") or [])
    if groups:
        return groups
    return ["__global__"]


def classify_mount_point_from_mount(mount: dict[str, Any]) -> str | None:
    """
    將單筆掛載對應到新 mount_point；已是目標 /個人-* 或 /部門-* 時回傳 None。

    亦支援從舊版固定名稱遷移：
      /個人雲端硬碟 → /個人-{帳號}（依 bucket）
      /部門雲端硬碟 → /部門-{標籤}（依 bucket 或 group）
    """
    old = _normalize_mount_point(mount.get("mount_point", ""))
    if is_labeled_mount(old):
        return None

    bucket = (mount.get("configuration") or {}).get("bucket", "")
    groups = list(mount.get("applicable_groups") or [])
    users = list(mount.get("applicable_users") or [])

    if is_generic_personal_mount(old):
        label = personal_label_from_mount(old, bucket=bucket, applicable_users=users)
        return format_personal_mount_point(label) if label else None

    if is_generic_department_mount(old):
        label = department_label_from_bucket(bucket) or department_label_from_mount(old, groups)
        return format_department_mount_point(label) if label else None

    if old.startswith("/minio-DEPT_"):
        label = department_label_from_mount(old, groups)
        return format_department_mount_point(label) if label else None

    if old.startswith("/minio-"):
        label = personal_label_from_mount(old, bucket=bucket, applicable_users=users)
        return format_personal_mount_point(label) if label else None

    return None


def classify_mount_point_rename(
    current_mount_point: str,
    *,
    personal_name: str = MOUNT_POINT_PERSONAL,
    department_name: str = MOUNT_POINT_DEPARTMENT,
    bucket: str | None = None,
    applicable_groups: list[str] | None = None,
    applicable_users: list[str] | None = None,
) -> str | None:
    """相容舊 API；建議改用 classify_mount_point_from_mount。"""
    return classify_mount_point_from_mount(
        {
            "mount_point": current_mount_point,
            "configuration": {"bucket": bucket or ""},
            "applicable_groups": applicable_groups or [],
            "applicable_users": applicable_users or [],
        }
    )


def build_mount_rename_plan(
    mounts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    產生更名計畫。

    衝突規則：
    - 個人：每位使用者最多 1 個（含已是 /個人-* 與待更名個人掛載）
    - 部門：可有多個，僅「同一使用者 + 相同目標名稱」重複時衝突

    Returns:
        list of {status, mount_id, old, new, users, error?}
        status: skip | update | conflict
    """
    existing_personal: dict[str, int] = {}
    personal_candidates: dict[str, list[int]] = {}
    target_counts: dict[tuple[str, str], int] = {}
    candidates: list[dict[str, Any]] = []

    for m in mounts:
        mid = int(m["mount_id"])
        old = _normalize_mount_point(m.get("mount_point", ""))
        identities = mount_applicable_identities(m)

        if is_labeled_personal_mount(old):
            for uid in identities:
                existing_personal[uid] = existing_personal.get(uid, 0) + 1
            continue

        new = classify_mount_point_from_mount(m)
        if new is None:
            continue

        is_personal = new.startswith(f"/{MOUNT_POINT_PREFIX_PERSONAL}")
        candidates.append(
            {
                "mount_id": mid,
                "old": old,
                "new": new,
                "users": identities,
                "is_personal": is_personal,
            }
        )

        for uid in identities:
            if is_personal:
                personal_candidates.setdefault(uid, []).append(mid)
            key = (uid, new)
            target_counts[key] = target_counts.get(key, 0) + 1

    duplicate_target = {k for k, c in target_counts.items() if c > 1}
    personal_conflict_users = {
        uid
        for uid, ids in personal_candidates.items()
        if len(ids) + existing_personal.get(uid, 0) > 1
    }

    plan: list[dict[str, Any]] = []

    for m in mounts:
        mid = int(m["mount_id"])
        old = _normalize_mount_point(m.get("mount_point", ""))

        if is_labeled_mount(old):
            plan.append(
                {"status": "skip", "mount_id": mid, "old": old, "new": "", "users": mount_applicable_identities(m)}
            )
            continue

        new = classify_mount_point_from_mount(m)
        if new is None:
            plan.append(
                {"status": "skip", "mount_id": mid, "old": old, "new": "", "users": mount_applicable_identities(m)}
            )
            continue

        identities = mount_applicable_identities(m)
        is_personal = new.startswith(f"/{MOUNT_POINT_PREFIX_PERSONAL}")
        conflict = False
        error = ""

        if is_personal and any(uid in personal_conflict_users for uid in identities):
            conflict = True
            error = "conflict: 同一使用者只能有一個個人外部磁碟"
        elif any((uid, new) in duplicate_target for uid in identities):
            conflict = True
            error = f"conflict: 同一使用者將有多個「{new}」"

        plan.append(
            {
                "status": "conflict" if conflict else "update",
                "mount_id": mid,
                "old": old,
                "new": new,
                "users": identities,
                **({"error": error} if conflict else {}),
            }
        )

    return plan
