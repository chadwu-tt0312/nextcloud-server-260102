#!/usr/bin/env python3
"""
Nextcloud External Storage 工具共用模組

供 create_external_storage.py、gen_external_storage.py 等腳本共用：
- Nextcloud 連線設定與 API（取得/建立外部儲存）
- MinIO/S3 環境設定
- Logger 設定
- amazons3 掛載格式轉換與 bucket 命名

版本：1.0.0
"""

import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# 設定
# ============================================================================


def get_nextcloud_config() -> dict[str, str]:
    """
    從環境變數讀取 Nextcloud 連線設定

    Returns:
        包含 url, username, password 的字典
    """
    return {
        "url": os.getenv("ENV_NEXTCLOUD_URL", ""),
        "username": os.getenv("ENV_NEXTCLOUD_USER", ""),
        "password": os.getenv("ENV_NEXTCLOUD_PASSWORD", ""),
    }


def get_env_config() -> dict[str, str]:
    """
    從環境變數讀取 MinIO 連線設定

    Returns:
        包含 hostname, port, region, access_key, secret_key 的字典
    """
    from urllib.parse import urlparse

    minio_url = os.getenv("ENV_MINIO_URL", "http://localhost:9000")
    parsed = urlparse(minio_url)

    return {
        "hostname": parsed.hostname or "localhost",
        "port": str(parsed.port or 9000),
        "region": os.getenv("ENV_REGION", "us-east-1"),
        "access_key": os.getenv("ENV_MINIO_ACCESS_KEY", ""),
        "secret_key": os.getenv("ENV_MINIO_SECRET_KEY", ""),
    }


# ============================================================================
# Logger
# ============================================================================


def setup_logger(
    log_file: Path | None = None,
    logger_name: str = "external_storage",
) -> logging.Logger:
    """
    設定 logger（使用 Python logging 模組）
    - 總是輸出到 console（使用 StreamHandler）
    - 若提供 log_file，同時輸出到檔案
    - Windows 下嘗試將 stdout/stderr 設為 UTF-8，避免 emoji 輸出錯誤

    Args:
        log_file: 記錄檔案路徑（None 時只輸出到 console）
        logger_name: Logger 名稱（供各腳本區分，如 create_external_storage、gen_external_storage）

    Returns:
        Logger 物件
    """
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(message)s")

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as e:
            logger.warning(f"⚠️  警告：無法設定日誌檔案 {log_file}: {e}")

    return logger


# ============================================================================
# Nextcloud API（files_external）
# ============================================================================


def _default_logger() -> logging.Logger:
    """取得預設 logger（供本模組內 API 函數使用）。"""
    return logging.getLogger("external_storage")


def fetch_all_external_storages(
    nextcloud_url: str,
    username: str,
    password: str,
    logger: logging.Logger | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """
    從 Nextcloud API 取得所有外部儲存空間

    Args:
        nextcloud_url: Nextcloud 伺服器 URL
        username: 管理員帳號
        password: 管理員密碼
        logger: 可選；未提供時使用模組預設 logger

    Returns:
        (success, message, data) tuple
        - success: 是否成功
        - message: 訊息
        - data: JSON 資料字典（key 為 ID，value 為儲存空間資訊）
    """
    logger = logger or _default_logger()
    url = f"{nextcloud_url.rstrip('/')}/apps/files_external/globalstorages"

    try:
        logger.info(f"呼叫 Nextcloud API: {url}")
        with httpx.Client(timeout=60.0) as client:
            response = client.get(
                url,
                auth=(username, password),
                headers={
                    "OCS-APIRequest": "true",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            data = response.json()

            storages_dict: dict[str, Any] = {}

            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    storage_id = str(item.get("id", ""))
                    if storage_id:
                        storages_dict[storage_id] = item
            elif isinstance(data, dict):
                storages_dict = data
            else:
                return False, f"API 回傳格式不正確: 預期 list 或 dict，收到 {type(data)}", {}

            logger.info(f"成功取得 {len(storages_dict)} 個外部儲存空間")
            return True, "成功取得外部儲存空間列表", storages_dict

    except httpx.TimeoutException:
        error_msg = f"API 請求逾時: {url}"
        logger.error(error_msg)
        return False, error_msg, {}
    except httpx.HTTPStatusError as e:
        error_msg = f"API 請求失敗 (HTTP {e.response.status_code}): {url}"
        logger.error(error_msg)
        return False, error_msg, {}
    except Exception as e:
        error_msg = f"呼叫 API 時發生錯誤: {e}"
        logger.error(error_msg)
        return False, error_msg, {}


def create_external_storage(
    nextcloud_url: str,
    username: str,
    password: str,
    api_data: dict[str, Any],
    dry_run: bool = False,
    logger: logging.Logger | None = None,
) -> tuple[bool, str]:
    """
    建立新的外部儲存空間

    Args:
        nextcloud_url: Nextcloud 伺服器 URL
        username: 管理員帳號
        password: 管理員密碼
        api_data: API 格式的資料
        dry_run: 是否為預覽模式
        logger: 可選；未提供時使用模組預設 logger

    Returns:
        (success, message) tuple
    """
    logger = logger or _default_logger()
    if dry_run:
        logger.info(f"[DRY RUN] 將建立外部儲存空間: {api_data.get('mountPoint')}")
        return True, "DRY RUN: 模擬建立成功"

    url = f"{nextcloud_url.rstrip('/')}/apps/files_external/globalstorages"

    try:
        log_message = f"[{datetime.now().isoformat()}] 建立: {api_data.get('mountPoint')}"
        logger.info(f"   ➕ {log_message}")
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                url,
                auth=(username, password),
                headers={
                    "OCS-APIRequest": "true",
                    "Content-Type": "application/json",
                },
                json=api_data,
            )
            response.raise_for_status()

            if response.status_code == 201:
                return True, f"成功建立 (HTTP {response.status_code})"
            warning_msg = f"⚠️  建立外部儲存空間回傳非預期狀態碼: {response.status_code}"
            logger.warning(warning_msg)
            return False, warning_msg

    except httpx.TimeoutException:
        error_msg = f"API 請求逾時: {url}"
        logger.error(error_msg)
        return False, error_msg
    except httpx.HTTPStatusError as e:
        error_msg = f"API 請求失敗 (HTTP {e.response.status_code}): {url}"
        logger.error(error_msg)
        return False, error_msg
    except Exception as e:
        error_msg = f"建立外部儲存空間時發生錯誤: {e}"
        logger.error(error_msg)
        return False, error_msg


# ============================================================================
# amazons3 掛載格式與 Bucket
# ============================================================================


def build_amazons3_configuration(
    bucket: str = "",
    hostname: str = "",
    port: str = "9000",
    region: str = "us-east-1",
    use_ssl: bool = False,
    use_path_style: bool = True,
    key: str = "",
    secret: str = "",
    legacy_auth: bool = False,
    use_multipart_copy: bool = True,
) -> dict[str, Any]:
    """
    組裝 amazons3 backend 的 configuration / backendOptions 字典（共用欄位與預設值）。

    供 convert_mount_to_api_format、generate_mount_config 等使用。

    Args:
        bucket: Bucket 名稱
        hostname: 主機位址
        port: 連接埠
        region: AWS Region
        use_ssl: 是否啟用 SSL
        use_path_style: 是否啟用 Path Style
        key: Access Key
        secret: Secret Key
        legacy_auth: 是否使用舊版認證
        use_multipart_copy: 是否使用 Multipart Copy

    Returns:
        符合 Nextcloud amazons3 的設定字典
    """
    return {
        "bucket": bucket,
        "hostname": hostname,
        "port": port,
        "region": region,
        "use_ssl": use_ssl,
        "use_path_style": use_path_style,
        "legacy_auth": legacy_auth,
        "useMultipartCopy": use_multipart_copy,
        "key": key,
        "secret": secret,
    }


def extract_amazons3_buckets(storages: dict[str, Any]) -> list[str]:
    """
    從外部儲存空間列表中提取所有 amazons3 backend 的 bucket 列表

    Args:
        storages: 外部儲存空間字典（key 為 ID，value 為儲存空間資訊）

    Returns:
        bucket 名稱列表（已轉為小寫）
    """
    buckets = []
    for storage_info in storages.values():
        if storage_info.get("backend") == "amazons3":
            backend_options = storage_info.get("backendOptions", {})
            bucket = backend_options.get("bucket", "")
            if bucket:
                buckets.append(bucket.lower())
    return buckets


def convert_mount_to_api_format(mount: dict[str, Any]) -> dict[str, Any]:
    """
    將 mounts.json 格式轉換為 Nextcloud API 格式

    Args:
        mount: mounts.json 中的單筆資料

    Returns:
        API 格式的字典
    """
    configuration = mount.get("configuration", {})
    backend_options = build_amazons3_configuration(
        bucket=configuration.get("bucket", ""),
        hostname=configuration.get("hostname", ""),
        port=configuration.get("port", "9000"),
        region=configuration.get("region", "us-east-1"),
        use_ssl=configuration.get("use_ssl", False),
        use_path_style=configuration.get("use_path_style", True),
        legacy_auth=configuration.get("legacy_auth", False),
        use_multipart_copy=configuration.get("useMultipartCopy", True),
        key=configuration.get("key", ""),
        secret=configuration.get("secret", ""),
    )

    return {
        "mountPoint": mount.get("mount_point", ""),
        "backend": "amazons3",
        "authMechanism": mount.get("authentication_type", "amazons3::accesskey"),
        "backendOptions": backend_options,
        "mountOptions": mount.get("options", {}),
        "applicableUsers": mount.get("applicable_users", []),
        "applicableGroups": mount.get("applicable_groups", []),
    }


def sanitize_bucket_name(name: str) -> str:
    """
    將名稱轉換為符合 S3/MinIO bucket 命名規則的格式

    規則：僅允許 a-z、0-9、點 (.)、連字號 (-)，長度 3–63 字元，
    開頭結尾不得為 . 或 -，不得有連續的點。

    Args:
        name: 原始名稱

    Returns:
        符合規則的 bucket 名稱
    """
    result = name.lower()
    result = result.replace("_", "-").replace("/", "-").replace("\\", "-")
    result = re.sub(r"[^a-z0-9.-]", "", result)
    result = re.sub(r"\.{2,}", ".", result)
    result = re.sub(r"-{2,}", "-", result)
    result = result.lstrip(".-").rstrip(".-")
    if len(result) < 3:
        result = result.ljust(3, "0")
    elif len(result) > 63:
        result = result[:63].rstrip(".-")
    return result


from tools_mount_point import (  # noqa: E402  re-export
    DEFAULT_BUCKET_SUFFIX,
    MOUNT_POINT_DEPARTMENT,
    MOUNT_POINT_PERSONAL,
    MOUNT_POINT_STYLE_ACCOUNT,
    MOUNT_POINT_STYLE_DISPLAY,
    account_label_from_bucket,
    apply_mount_point_style_to_mount,
    apply_mount_point_style_to_mounts,
    classify_mount_point_rename,
    resolve_mount_point,
    resolve_mount_point_display,
)


def generate_bucket_name_from_user_id(user_id: str, bucket_suffix: str = "-filespace") -> str:
    """
    從 user_id 產生符合 MinIO 命名規則的 bucket 名稱

    轉換邏輯：移除 "minio-" 前綴（若存在）→ sanitize_bucket_name → 加上 bucket_suffix

    Args:
        user_id: 使用者 ID（例如 minio-DEPT_SMG_ARC1）
        bucket_suffix: Bucket 名稱後綴（預設 -filespace）

    Returns:
        符合 MinIO 命名規則的 bucket 名稱
    """
    base_name = user_id[6:] if user_id.startswith("minio-") else user_id
    sanitized = sanitize_bucket_name(base_name)
    return f"{sanitized}{bucket_suffix}"
