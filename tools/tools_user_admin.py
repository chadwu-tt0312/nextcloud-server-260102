#!/usr/bin/env python3
"""
Nextcloud 使用者管理工具共用模組

供 set_user_quota.py 等腳本共用：
- 透過 OCS Provisioning API 列出所有使用者
- 透過 OCS Provisioning API 設定單一使用者的 quota

備註：
- Nextcloud quota 只作用於使用者的 home storage；外部儲存空間（例如 minio
  amazons3 掛載）預設「不計入」quota（系統設定 quota_include_external_storage
  預設為 false）。因此將 quota 設為 0 可阻擋使用者在個人空間新增檔案，
  但不影響外部掛載點的寫入。

版本：1.0.0
"""

import logging
from typing import TYPE_CHECKING, Any

import httpx

# 重複使用 external storage 模組的 logger 與設定讀取，維持一致性
from tools_external_storage import _default_logger

if TYPE_CHECKING:
    from tools_runtime import NextcloudRuntime

# OCS Provisioning API 共用標頭
_OCS_HEADERS = {
    "OCS-APIRequest": "true",
    "Accept": "application/json",
}


def fetch_all_users(
    nextcloud_url: str,
    username: str,
    password: str,
    logger: logging.Logger | None = None,
    page_size: int = 500,
) -> tuple[bool, str, list[str]]:
    """
    透過 OCS Provisioning API 取得所有使用者 ID 列表（自動分頁）

    對應 API: GET /ocs/v2.php/cloud/users?limit=&offset=

    大型站台（例如 1 萬名使用者）單次請求可能被伺服器限制，因此以 limit/offset
    分頁迴圈取得，直到回傳數量小於 page_size 為止。

    Args:
        nextcloud_url: Nextcloud 伺服器 URL
        username: 管理員帳號
        password: 管理員密碼
        logger: 可選；未提供時使用模組預設 logger
        page_size: 每頁筆數（預設 500）

    Returns:
        (success, message, user_ids) tuple
        - success: 是否成功
        - message: 訊息
        - user_ids: 使用者 ID 列表
    """
    logger = logger or _default_logger()
    base_url = f"{nextcloud_url.rstrip('/')}/ocs/v2.php/cloud/users"
    all_users: list[str] = []
    offset = 0

    try:
        with httpx.Client(timeout=60.0) as client:
            while True:
                logger.info(f"呼叫 OCS API 取得使用者列表 (offset={offset}, limit={page_size})")
                response = client.get(
                    base_url,
                    auth=(username, password),
                    headers=_OCS_HEADERS,
                    params={"limit": page_size, "offset": offset},
                )
                response.raise_for_status()
                data = response.json()

                # OCS v2 JSON 結構: {"ocs": {"meta": {...}, "data": {"users": [...]}}}
                ocs = data.get("ocs", {}) if isinstance(data, dict) else {}
                meta = ocs.get("meta", {})
                status_code = meta.get("statuscode")
                if status_code not in (100, 200):
                    return False, f"OCS 回傳錯誤狀態 {status_code}: {meta.get('message')}", []

                users = ocs.get("data", {}).get("users", [])
                if not isinstance(users, list):
                    return False, f"OCS 回傳格式不正確: 預期 users 為 list，收到 {type(users)}", []

                all_users.extend(users)

                # 回傳數量小於 page_size 表示已到最後一頁
                if len(users) < page_size:
                    break
                offset += page_size

        logger.info(f"成功取得 {len(all_users)} 位使用者")
        return True, "成功取得使用者列表", all_users

    except httpx.TimeoutException:
        error_msg = f"API 請求逾時: {base_url}"
        logger.error(error_msg)
        return False, error_msg, []
    except httpx.HTTPStatusError as e:
        error_msg = f"API 請求失敗 (HTTP {e.response.status_code}): {base_url}"
        logger.error(error_msg)
        return False, error_msg, []
    except Exception as e:
        error_msg = f"呼叫 API 時發生錯誤: {e}"
        logger.error(error_msg)
        return False, error_msg, []


def set_user_quota(
    nextcloud_url: str,
    username: str,
    password: str,
    user_id: str,
    quota: str = "0",
    dry_run: bool = False,
    logger: logging.Logger | None = None,
) -> tuple[bool, str]:
    """
    透過 OCS Provisioning API 設定單一使用者的 quota

    對應 API: PUT /ocs/v2.php/cloud/users/{userId}
              form: key=quota, value=<quota>

    Args:
        nextcloud_url: Nextcloud 伺服器 URL
        username: 管理員帳號
        password: 管理員密碼
        user_id: 目標使用者 ID
        quota: quota 值（例如 "0"、"none"、"1 GB"、"default"；預設 "0"）
        dry_run: 是否為預覽模式
        logger: 可選；未提供時使用模組預設 logger

    Returns:
        (success, message) tuple
    """
    logger = logger or _default_logger()

    if dry_run:
        logger.info(f"[DRY RUN] 將設定 {user_id} 的 quota = {quota}")
        return True, "DRY RUN: 模擬設定成功"

    url = f"{nextcloud_url.rstrip('/')}/ocs/v2.php/cloud/users/{user_id}"

    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.put(
                url,
                auth=(username, password),
                headers=_OCS_HEADERS,
                data={"key": "quota", "value": quota},
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()

        ocs = data.get("ocs", {}) if isinstance(data, dict) else {}
        meta = ocs.get("meta", {})
        status_code = meta.get("statuscode")
        if status_code in (100, 200):
            return True, f"成功設定 quota = {quota}"
        return False, f"OCS 回傳錯誤狀態 {status_code}: {meta.get('message')}"

    except httpx.TimeoutException:
        error_msg = f"API 請求逾時: {url}"
        logger.error(error_msg)
        return False, error_msg
    except httpx.HTTPStatusError as e:
        error_msg = f"API 請求失敗 (HTTP {e.response.status_code}): {url}"
        logger.error(error_msg)
        return False, error_msg
    except Exception as e:
        error_msg = f"設定 quota 時發生錯誤: {e}"
        logger.error(error_msg)
        return False, error_msg


def set_user_quota_occ(
    runtime: "NextcloudRuntime",
    user_id: str,
    quota: str = "0",
    dry_run: bool = False,
    logger: logging.Logger | None = None,
) -> tuple[bool, str]:
    """
    透過 occ user:setting 設定單一使用者的 quota（不需 OCS 環境變數）

    寫入 oc_preferences（app=files, key=quota），與 OCS setQuota 相同欄位。
    """
    logger = logger or _default_logger()

    if dry_run:
        return True, f"DRY RUN: occ user:setting {user_id} files quota {quota}"

    proc = runtime.exec_run_user(
        ["php", "occ", "user:setting", user_id, "files", "quota", quota],
    )
    if proc.returncode == 0:
        return True, f"成功設定 quota = {quota}"
    err = (proc.stderr or proc.stdout or "occ user:setting 失敗").strip()
    logger.error(err)
    return False, err


def ocs_configured(config: dict[str, str]) -> bool:
    return bool(config.get("url") and config.get("username") and config.get("password"))
