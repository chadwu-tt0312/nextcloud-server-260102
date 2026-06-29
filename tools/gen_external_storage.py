#!/usr/bin/env python3
"""
Nextcloud External Storage Configuration Generator

功能：
- 從 CSV 檔案讀取使用者清單
- 產生 Nextcloud files_external:import 所需的 JSON 設定檔
- 支援斷點續傳功能（中斷後可繼續處理）
- 自動記錄執行過程（檔案與 console 顯示）
- 支援資料變更偵測（使用 SHA256 雜湊值）

版本：1.0.0

用法:
    python gen_external_storage.py --help
    python gen_external_storage.py  # 使用預設值：import/import-accounts.csv 和 import/mounts.json
    python gen_external_storage.py --csv import/import-accounts.csv --output import/mounts.json

CSV 格式範例 (import-accounts.csv):
    Dept_name,Emp_no,Capacity
    SMG_ARC1,,10GiB
    SMG_ARC5,00059094,2GiB

注意事項：
- CSV 檔案路徑相同即視為相同檔案（用於斷點續傳驗證）
- 支援最多 50,000 筆資料（目前已知使用場景）
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx
from tools_external_storage import (
    build_amazons3_configuration,
    generate_bucket_name_from_user_id,
    get_env_config,
    get_nextcloud_config,
    MOUNT_POINT_STYLE_ACCOUNT,
    MOUNT_POINT_STYLE_DISPLAY,
    resolve_mount_point,
    sanitize_bucket_name,
    setup_logger,
)

# ============================================================================
# 常數定義
# ============================================================================

# 進度顯示與狀態儲存間隔
PROGRESS_REPORT_INTERVAL = 100  # 每 100 筆顯示進度
STATE_SAVE_INTERVAL = 500  # 每 500 筆儲存狀態
STATE_SAVE_TIME_INTERVAL = 30  # 每 30 秒儲存狀態


# ============================================================================
# 工具函數（Nextcloud UID、狀態檔、掛載產生等，仍為本腳本專用）
# ============================================================================


def fetch_nextcloud_user_uid(
    client: httpx.Client,
    nextcloud_url: str,
    username: str,
    password: str,
    search: str,
) -> tuple[bool, str, str | None]:
    """
    從 Nextcloud OCS API 查詢使用者 UID（帳號名稱，即 getUID() 值，用於 applicable_users）

    對應 api-nextcloud.http 的請求：
      GET /ocs/v1.php/cloud/users?search=<keyword>&format=json
    API 回傳的 data.users 為 UID 陣列（與「設定→使用者」的「帳號名稱」一致）。

    Args:
        client: httpx Client（可重複使用以降低連線成本）
        nextcloud_url: Nextcloud 伺服器 URL（例如 http://host:port/）
        username: Nextcloud 管理者帳號
        password: Nextcloud 管理者密碼
        search: 搜尋關鍵字（工號、顯示名稱等；LDAP 需設定「用於使用者搜尋的屬性」才能以工號搜到）

    Returns:
        (success, message, uid) 其中 uid 為 Nextcloud 帳號名稱，可直接用於 applicable_users
    """
    search = search.strip()
    if not search:
        return False, "search 不能為空字串", None

    url = f"{nextcloud_url.rstrip('/')}/ocs/v1.php/cloud/users"
    try:
        response = client.get(
            url,
            params={"search": search, "format": "json"},
            auth=(username, password),
            headers={
                "OCS-APIRequest": "true",
                "Accept": "application/json",
            },
        )
        response.raise_for_status()
        payload = response.json()

        ocs = payload.get("ocs", {}) if isinstance(payload, dict) else {}
        meta = ocs.get("meta", {}) if isinstance(ocs, dict) else {}
        statuscode = meta.get("statuscode", 100)
        try:
            statuscode_int = int(statuscode)
        except (TypeError, ValueError):
            statuscode_int = 100

        if statuscode_int != 100:
            return False, f"OCS API 回傳 statuscode={statuscode}", None

        data = ocs.get("data", {}) if isinstance(ocs, dict) else {}
        users = data.get("users", []) if isinstance(data, dict) else []

        candidates: list[str] = []
        if isinstance(users, list):
            candidates = [u for u in users if isinstance(u, str)]

        if not candidates:
            return False, f"找不到符合的使用者（search={search}）", None

        if search in candidates:
            return True, "查詢成功", search

        search_lower = search.lower()
        for candidate in candidates:
            if candidate.lower() == search_lower:
                return True, "查詢成功", candidate

        return True, "查詢成功", candidates[0]

    except httpx.TimeoutException:
        return False, f"Nextcloud API 連線逾時: {url}", None
    except httpx.HTTPStatusError as e:
        return (
            False,
            f"Nextcloud API 回應錯誤 (HTTP {e.response.status_code}): {url}",
            None,
        )
    except Exception as e:
        return False, f"Nextcloud UID 查詢失敗: {e}", None


def build_nextcloud_uid_resolver(
    client: httpx.Client,
    nextcloud_url: str,
    username: str,
    password: str,
    logger: logging.Logger,
) -> Callable[[str], str]:
    """
    建立帶快取的 Nextcloud UID 查詢函數

    Args:
        client: httpx Client
        nextcloud_url: Nextcloud URL
        username: 帳號
        password: 密碼
        logger: Logger

    Returns:
        resolve(search) -> uid 的函數；查不到時會 raise ValueError
    """
    cache: dict[str, str | None] = {}

    def resolve(search: str) -> str:
        key = search.strip()
        if key in cache:
            uid = cache[key]
            if not uid:
                raise ValueError(f"無法取得 Nextcloud UID（search={key}）：已快取為查無此人")
            return uid

        success, message, uid = fetch_nextcloud_user_uid(
            client=client,
            nextcloud_url=nextcloud_url,
            username=username,
            password=password,
            search=key,
        )
        if not success or not uid:
            cache[key] = None
            raise ValueError(f"無法取得 Nextcloud UID（search={key}）：{message}")

        if uid != key:
            logger.info(f"🔎 Nextcloud UID 映射: {key} -> {uid}")
        cache[key] = uid
        return uid

    return resolve


def calculate_row_hash(row: dict[str, str]) -> str:
    """
    計算 CSV 資料列的雜湊值（用於比較資料是否變更）

    Args:
        row: CSV 資料列字典

    Returns:
        SHA256 雜湊值（hex 字串）
    """
    # 將所有欄位值排序後組合
    sorted_items = sorted(row.items())
    row_str = "|".join(f"{k}:{v}" for k, v in sorted_items)
    return hashlib.sha256(row_str.encode("utf-8")).hexdigest()


def load_state_file(state_path: Path, csv_path: str) -> dict[str, dict[str, Any]]:
    """
    載入狀態檔案並驗證 CSV 檔案路徑

    Args:
        state_path: 狀態檔案路徑
        csv_path: 當前 CSV 檔案路徑（用於驗證）

    Returns:
        已處理的資料字典（key: user_id, value: 資料字典），如果檔案不存在或路徑不匹配則返回空字典
    """
    if not state_path.exists():
        return {}

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

        # 驗證 CSV 檔案路徑是否相同（路徑相同即視為相同檔案）
        state_csv_path = state.get("csv_path", "")
        if state_csv_path != csv_path:
            print(
                f"⚠️  警告：狀態檔案對應的 CSV 檔案不同（狀態: {state_csv_path}, 當前: {csv_path}），將重新開始",
                file=sys.stderr,
            )
            return {}

        processed_data = state.get("processed_data", {})
        return processed_data

    except (json.JSONDecodeError, IOError) as e:
        # 注意：這裡無法使用 logger，因為 logger 可能尚未初始化
        # 使用 print 作為後備方案
        print(f"⚠️  警告：無法讀取狀態檔案 {state_path}: {e}", file=sys.stderr)
        return {}


def save_state_file(
    state_path: Path,
    csv_path: str,
    processed_data: dict[str, dict[str, Any]],
    total_rows: int,
    processed_count: int,
) -> None:
    """
    儲存狀態檔案

    Args:
        state_path: 狀態檔案路徑
        csv_path: CSV 檔案路徑
        processed_data: 已處理的資料（key: user_id, value: 資料字典）
        total_rows: 總資料筆數
        processed_count: 已處理筆數
    """
    state = {
        "csv_path": csv_path,
        "last_updated": datetime.now().isoformat(),
        "total_rows": total_rows,
        "processed_count": processed_count,
        "processed_data": processed_data,
    }

    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except IOError as e:
        # 注意：這裡無法使用 logger，因為 logger 可能尚未初始化
        # 使用 print 作為後備方案
        print(f"⚠️  警告：無法儲存狀態檔案 {state_path}: {e}", file=sys.stderr)


def generate_mount_config(
    mount_id: int,
    user_id: str,
    bucket_name: str,
    access_key: str,
    secret_key: str,
    hostname: str = "localhost",
    port: str = "9000",
    region: str = "us-east-1",
    use_ssl: bool = False,
    use_path_style: bool = True,
    applicable_users: list[str] | None = None,
    applicable_groups: list[str] | None = None,
    mount_point_style: str = MOUNT_POINT_STYLE_DISPLAY,
    bucket_suffix: str = "-filespace",
) -> dict[str, Any]:
    """
    產生單筆 Nextcloud External Storage 掛載設定

    Args:
        mount_id: 掛載 ID（用於 import 時識別）
        user_id: 內部識別（用於 bucket 命名；掛載點由 resolve_mount_point 決定）
        mount_point_style: display（預設）或 account（每人資料夾，名稱取自 bucket）
        bucket_suffix: account 模式時自 bucket 移除的後綴（預設 -filespace）
        bucket_name: S3 Bucket 名稱
        access_key: S3 Access Key
        secret_key: S3 Secret Key
        hostname: MinIO/S3 主機位址
        port: 連接埠
        region: AWS Region
        use_ssl: 是否啟用 SSL
        use_path_style: 是否啟用 Path Style
        applicable_users: 適用使用者清單（必須為 Nextcloud 的 UID／帳號名稱，與「設定→使用者」中的「帳號名稱」一致；None 時由 user_id 推導為單一使用者）
        applicable_groups: 適用群組清單

    Returns:
        掛載設定字典
    """
    # 未傳入時由 user_id 推導：去掉 minio- 前綴。注意：Nextcloud 外部儲存比對的是 getUID()，故必須使用「帳號名稱」而非顯示名稱
    effective_users: list[str] = (
        applicable_users if applicable_users is not None else [user_id.removeprefix("minio-")]
    )
    configuration = build_amazons3_configuration(
        bucket=bucket_name,
        hostname=hostname,
        port=port,
        region=region,
        use_ssl=use_ssl,
        use_path_style=use_path_style,
        key=access_key,
        secret=secret_key,
    )
    return {
        "mount_id": mount_id,
        "mount_point": resolve_mount_point(
            user_id,
            style=mount_point_style,
            applicable_users=effective_users,
            bucket=bucket_name,
            bucket_suffix=bucket_suffix,
        ),
        "storage": "\\OCA\\Files_External\\Lib\\Storage\\AmazonS3",
        "authentication_type": "amazons3::accesskey",
        "configuration": configuration,
        "options": {},
        "applicable_users": effective_users,
        "applicable_groups": applicable_groups or [],
    }


# ============================================================================
# 主要處理函數
# ============================================================================


def process_csv_rows(
    rows: list[dict[str, str]],
    user_id_extractor: Callable[[dict[str, str]], str],
    mount_generator: Callable[[dict[str, str], str, dict[str, str], int], dict[str, Any]],
    csv_path: str,
    state_path: Path | None,
    resume: bool,
    logger: logging.Logger,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    共用的 CSV 處理邏輯

    Args:
        rows: CSV 資料列清單
        user_id_extractor: 從 CSV 列提取 user_id 的函數
        mount_generator: 產生掛載設定的函數（參數: row, user_id, env_config, mount_id）
        csv_path: CSV 檔案路徑
        state_path: 狀態檔案路徑
        resume: 是否啟用斷點續傳
        logger: Logger 物件

    Returns:
        (掛載設定清單, 統計資訊字典)
    """
    mounts = []
    processed_data: dict[str, dict[str, Any]] = {}
    stats = {
        "total": 0,
        "skipped": 0,
        "updated": 0,
        "new": 0,
        "errors": 0,
    }

    # 載入狀態檔案
    if resume and state_path:
        processed_data = load_state_file(state_path, csv_path)
        if processed_data:
            logger.info(f"📋 載入狀態檔案：已處理 {len(processed_data)} 筆資料")

    stats["total"] = len(rows)
    logger.info(f"📋 開始處理 {stats['total']} 筆資料...")

    # 記錄開始時間
    start_time = datetime.now()
    last_save_time = start_time
    env_config = get_env_config()

    for i, row in enumerate(rows, start=1):
        try:
            # 提取 user_id
            user_id = user_id_extractor(row)
            if not user_id:
                logger.warning(f"⚠️  第 {i} 筆：無法提取 user_id，跳過")
                stats["errors"] += 1
                continue

            # 計算資料雜湊值
            row_hash = calculate_row_hash(row)

            # 檢查是否已處理過
            if resume and user_id in processed_data:
                existing = processed_data[user_id]
                if existing.get("hash") == row_hash:
                    # 資料完全相同，跳過（但需要更新 mount_id）
                    stats["skipped"] += 1
                    existing_mount = existing["mount"].copy()
                    existing_mount["mount_id"] = i
                    mounts.append(existing_mount)
                    if i % PROGRESS_REPORT_INTERVAL == 0:
                        logger.info(
                            f"   📊 進度: {i}/{stats['total']} "
                            f"(跳過: {stats['skipped']}, 新增: {stats['new']}, 更新: {stats['updated']})"
                        )
                    continue
                else:
                    # 資料有變更，需要更新
                    stats["updated"] += 1
                    log_message = f"[{datetime.now().isoformat()}] 更新: {user_id} (第 {i} 筆)"
                    logger.info(f"   🔄 {log_message}")
            else:
                # 新資料
                stats["new"] += 1
                if stats["new"] <= 10 or stats["new"] % 100 == 0:
                    log_message = f"[{datetime.now().isoformat()}] 新增: {user_id} (第 {i} 筆)"
                    logger.info(f"   ➕ {log_message}")

            # 產生掛載設定
            mount = mount_generator(row, user_id, env_config, i)

            mounts.append(mount)

            # 更新已處理資料
            processed_data[user_id] = {
                "hash": row_hash,
                "mount": mount,
                "processed_at": datetime.now().isoformat(),
            }

            # 定期顯示進度並儲存狀態
            if i % PROGRESS_REPORT_INTERVAL == 0:
                elapsed = (datetime.now() - start_time).total_seconds()
                rate = i / elapsed if elapsed > 0 else 0
                remaining = (stats["total"] - i) / rate if rate > 0 else 0
                logger.info(
                    f"   📊 進度: {i}/{stats['total']} "
                    f"(跳過: {stats['skipped']}, 新增: {stats['new']}, 更新: {stats['updated']}) "
                    f"速度: {rate:.1f} 筆/秒, 預估剩餘: {remaining:.0f} 秒"
                )

            # 定期儲存狀態
            current_time = datetime.now()
            if state_path and (
                i % STATE_SAVE_INTERVAL == 0
                or (current_time - last_save_time).total_seconds() >= STATE_SAVE_TIME_INTERVAL
            ):
                save_state_file(state_path, csv_path, processed_data, stats["total"], i)
                last_save_time = current_time

        except KeyError as e:
            logger.error(f"❌ 第 {i} 筆：缺少必要欄位 {e}")
            stats["errors"] += 1
            logger.error(f"[{datetime.now().isoformat()}] 錯誤: 第 {i} 筆 - 缺少欄位 {e}")
        except ValueError as e:
            logger.error(f"❌ 第 {i} 筆：資料格式錯誤 {e}")
            stats["errors"] += 1
            logger.error(f"[{datetime.now().isoformat()}] 錯誤: 第 {i} 筆 - 格式錯誤 {e}")
        except Exception as e:
            logger.error(f"❌ 第 {i} 筆處理失敗: {e}")
            stats["errors"] += 1
            logger.error(f"[{datetime.now().isoformat()}] 錯誤: 第 {i} 筆 - {e}")

    # 最終儲存狀態
    if state_path:
        save_state_file(state_path, csv_path, processed_data, stats["total"], stats["total"])

    # 顯示完成統計
    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info("")
    logger.info("✅ 處理完成！")
    logger.info(f"   總筆數: {stats['total']}")
    logger.info(f"   跳過: {stats['skipped']} 筆（資料未變更）")
    logger.info(f"   新增: {stats['new']} 筆")
    logger.info(f"   更新: {stats['updated']} 筆")
    logger.info(f"   錯誤: {stats['errors']} 筆")
    logger.info(f"   耗時: {elapsed:.1f} 秒")
    if stats["total"] > 0:
        logger.info(f"   平均速度: {stats['total'] / elapsed:.1f} 筆/秒")

    return mounts, stats


def generate_from_import_csv(
    csv_path: str,
    logger: logging.Logger,
    bucket_suffix: str = "-filespace",
    hostname: str | None = None,
    port: str | None = None,
    region: str | None = None,
    use_ssl: bool = False,
    use_path_style: bool = True,
    nextcloud_url: str | None = None,
    nextcloud_username: str | None = None,
    nextcloud_password: str | None = None,
    resolve_nextcloud_uid: bool = True,
    state_path: Path | None = None,
    resume: bool = True,
    mount_point_style: str = MOUNT_POINT_STYLE_DISPLAY,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    從 import-accounts.csv 格式讀取使用者清單並產生掛載設定

    CSV 欄位:
        - Dept_name: 部門名稱
        - Emp_no: 員工編號
        - Capacity: 容量（目前不使用）

    user_id 組裝邏輯:
        - Emp_no 有值時: f"minio-{Emp_no}"
        - Emp_no 空白時: f"minio-DEPT_{Dept_name}"

    Args:
        csv_path: CSV 檔案路徑
        bucket_suffix: Bucket 名稱後綴
        hostname: MinIO/S3 主機位址（None 時從環境變數讀取）
        port: 連接埠（None 時從環境變數讀取）
        region: AWS Region（None 時從環境變數讀取）
        use_ssl: 是否啟用 SSL
        use_path_style: 是否啟用 Path Style
        state_path: 狀態檔案路徑（用於斷點續傳）
        resume: 是否啟用斷點續傳

    Returns:
        (掛載設定清單, 統計資訊字典)
    """
    nextcloud_client: httpx.Client | None = None
    resolve_uid: Callable[[str], str] | None = None

    if resolve_nextcloud_uid:
        env_nextcloud = get_nextcloud_config()
        effective_nextcloud_url = nextcloud_url or env_nextcloud["url"]
        effective_nextcloud_username = nextcloud_username or env_nextcloud["username"]
        effective_nextcloud_password = nextcloud_password or env_nextcloud["password"]

        if (
            effective_nextcloud_url
            and effective_nextcloud_username
            and effective_nextcloud_password
        ):
            nextcloud_client = httpx.Client(timeout=30.0)
            resolve_uid = build_nextcloud_uid_resolver(
                client=nextcloud_client,
                nextcloud_url=effective_nextcloud_url,
                username=effective_nextcloud_username,
                password=effective_nextcloud_password,
                logger=logger,
            )
        else:
            raise RuntimeError(
                "❌ 未提供完整 Nextcloud 連線設定（ENV_NEXTCLOUD_URL / ENV_NEXTCLOUD_USER / ENV_NEXTCLOUD_PASSWORD），"
                "無法查詢使用者真正的 UID。applicable_users 必須使用 Nextcloud 的 UID（帳號名稱），"
                "否則使用者將無法看到外部磁碟。\n"
                "  → 請設定上述環境變數（或在 .env 檔案中加入），再重新執行。\n"
                "  → 若確定 CSV 的 Emp_no / Dept_name 即為 Nextcloud 帳號名稱，可加上 --no-nextcloud-uid-lookup 跳過此檢查。"
            )

    def extract_user_id(row: dict[str, str]) -> str:
        """從 CSV 列提取 user_id"""
        emp_no = row.get("Emp_no", "").strip()
        dept_name = row.get("Dept_name", "").strip()

        if emp_no:
            return f"minio-{emp_no}"
        elif dept_name:
            return f"minio-DEPT_{dept_name}"
        else:
            logger.info(f"dept_name={dept_name}, emp_no={emp_no}, row={row}")
            return ""  # 無法提取，將在 process_csv_rows 中處理為錯誤

    def generate_mount(
        row: dict[str, str], user_id: str, config: dict[str, str], mount_id: int
    ) -> dict[str, Any]:
        """產生掛載設定"""
        bucket_name = generate_bucket_name_from_user_id(user_id, bucket_suffix)
        nextcloud_user_key = user_id.removeprefix("minio-")
        # 必須使用 Nextcloud UID（帳號名稱）；resolve_uid 透過 OCS API 查詢取得正確 UID，LDAP 若為 UUID 帳號名稱時不可用 --no-nextcloud-uid-lookup
        applicable_users = (
            [resolve_uid(nextcloud_user_key)] if resolve_uid else [nextcloud_user_key]
        )

        return generate_mount_config(
            mount_id=mount_id,
            user_id=user_id,
            bucket_name=bucket_name,
            access_key=config["access_key"],
            secret_key=config["secret_key"],
            hostname=hostname or config["hostname"],
            port=port or config["port"],
            region=region or config["region"],
            use_ssl=use_ssl,
            use_path_style=use_path_style,
            applicable_users=applicable_users,
            mount_point_style=mount_point_style,
            bucket_suffix=bucket_suffix,
        )

    # 讀取 CSV 檔案
    with open(csv_path, "r", encoding="utf-8-sig") as f:  # "utf-8"
        reader = csv.DictReader(f)
        rows = list(reader)

    try:
        # 使用共用處理邏輯
        mounts, stats = process_csv_rows(
            rows=rows,
            user_id_extractor=extract_user_id,
            mount_generator=generate_mount,
            csv_path=csv_path,
            state_path=state_path,
            resume=resume,
            logger=logger,
        )
        return mounts, stats
    finally:
        if nextcloud_client:
            nextcloud_client.close()


def save_mounts_json(mounts: list[dict[str, Any]], output_path: str) -> None:
    """
    儲存掛載設定為 JSON 檔案

    Args:
        mounts: 掛載設定清單
        output_path: 輸出檔案路徑
    """
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(mounts, f, indent=2, ensure_ascii=False)

    file_size = output_file.stat().st_size
    file_size_mb = file_size / (1024 * 1024)

    # 使用 logger 輸出（若主程式已呼叫 setup_logger，則與主程式共用同一 logger）
    logger = logging.getLogger("gen_external_storage")
    if logger.handlers:
        logger.info(f"✅ 已產生 {len(mounts)} 個掛載設定至 {output_path}")
        logger.info(f"   檔案大小: {file_size_mb:.2f} MB")
    else:
        # 後備方案：如果 logger 尚未初始化，使用 print
        print(f"✅ 已產生 {len(mounts)} 個掛載設定至 {output_path}")
        print(f"   檔案大小: {file_size_mb:.2f} MB")


# ============================================================================
# 主程式
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nextcloud External Storage Configuration Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例:
  # 使用預設值（import/import-accounts.csv 和 import/mounts.json）
  python gen_external_storage.py

  # 從 import 目錄下的 import-accounts.csv 讀取（欄位: Dept_name,Emp_no,Capacity）
  python gen_external_storage.py --csv import-accounts.csv --output import/mounts.json

  # 指定完整路徑（仍會自動加上 import/ 前綴）
  python gen_external_storage.py --csv import/import-accounts.csv --output import/mounts.json

  # 自訂 MinIO 連線設定（覆蓋環境變數）
  python gen_external_storage.py --csv import-accounts.csv \\
      --hostname minio.example.com --port 443 --use-ssl --output import/mounts.json

  # 停用斷點續傳
  python gen_external_storage.py --csv import-accounts.csv --output import/mounts.json --no-resume

匯入指令:
  php occ files_external:import --dry import/mounts.json  # 預覽
  php occ files_external:import import/mounts.json        # 正式匯入
        """,
    )

    # 資料來源（可選，預設為 import/import-accounts.csv）
    parser.add_argument(
        "--csv",
        type=str,
        default="import/import-accounts.csv",
        dest="import_csv",
        help="import-accounts.csv 格式檔案路徑（欄位: Dept_name, Emp_no, Capacity），檔案會從 import 目錄讀取（預設: import/import-accounts.csv）",
    )

    # 輸出設定
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="import/mounts.json",
        help="輸出 JSON 檔案路徑（預設: import/mounts.json）",
    )

    # MinIO/S3 連線設定（可選，預設從環境變數讀取）
    parser.add_argument(
        "--hostname",
        type=str,
        default=None,
        help="MinIO/S3 主機位址（預設從 ENV_MINIO_URL 讀取）",
    )
    parser.add_argument(
        "--port",
        type=str,
        default=None,
        help="連接埠（預設從 ENV_MINIO_URL 讀取）",
    )
    parser.add_argument(
        "--region",
        type=str,
        default=None,
        help="AWS Region（預設從 ENV_REGION 讀取）",
    )
    parser.add_argument(
        "--use-ssl",
        action="store_true",
        help="啟用 SSL（預設: 關閉）",
    )
    parser.add_argument(
        "--no-path-style",
        action="store_true",
        help="停用 Path Style（預設: 啟用）",
    )
    parser.add_argument(
        "--no-nextcloud-uid-lookup",
        action="store_true",
        help="停用 Nextcloud UID 查詢，沿用 CSV 鍵值（如 Emp_no）作為 applicable_users；僅當該鍵值即為 Nextcloud 帳號名稱時使用（LDAP 若為 UUID 帳號名稱請勿使用）",
    )

    parser.add_argument(
        "--mount-point-style",
        type=str,
        choices=[MOUNT_POINT_STYLE_DISPLAY, MOUNT_POINT_STYLE_ACCOUNT],
        default=os.environ.get("ENV_MOUNT_POINT_STYLE", MOUNT_POINT_STYLE_DISPLAY),
        help="掛載點命名：display=/個人雲端硬碟（預設）；account=每人資料夾（名稱取自 bucket 去掉 -filespace）",
    )

    # Bucket 命名設定
    parser.add_argument(
        "--bucket-suffix",
        type=str,
        default="-filespace",
        help="Bucket 名稱後綴（預設: -filespace）",
    )

    # 斷點續傳設定
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="停用斷點續傳功能（預設: 啟用）",
    )
    parser.add_argument(
        "--state-file",
        type=str,
        default=None,
        help="狀態檔案路徑（預設: import/{output 檔名}.state.json）",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="記錄檔案路徑（預設: logs/gen-mount.log）",
    )

    args = parser.parse_args()

    # 設定狀態檔案和記錄檔案路徑（預設狀態檔一律在 import/ 下，檔名為輸出檔名加 .state.json）
    output_path = Path(args.output)
    state_path = (
        Path(args.state_file)
        if args.state_file
        else Path("import") / f"{output_path.stem}.state.json"
    )
    log_path = Path(args.log_file) if args.log_file else Path("logs/gen-mount.log")

    # 初始化 logger（總是初始化，即使沒有 log_file 也會輸出到 console）
    logger = setup_logger(log_path, logger_name="gen_external_storage")
    logger.info(f"=== 執行記錄開始: {datetime.now().isoformat()} ===")

    # 統一檔案來源為 import 目錄
    csv_path_str = args.import_csv
    csv_path = Path(csv_path_str)

    # 如果路徑不是絕對路徑且不包含 import 目錄，自動加上 import/ 前綴
    if not csv_path.is_absolute():
        # 檢查路徑的第一個部分是否為 "import"
        parts = csv_path.parts
        if len(parts) == 0 or parts[0] != "import":
            csv_path = Path("import") / csv_path_str
            csv_path_str = str(csv_path)

    if not csv_path.exists():
        logger.error(f"❌ CSV 檔案不存在: {csv_path}")
        return 1

    logger.info(f"CSV 檔案: {csv_path}")
    logger.info(f"輸出檔案: {args.output}")
    logger.info(f"狀態檔案: {state_path}")
    logger.info("=" * 60)
    logger.info("")

    # 產生掛載設定
    try:
        resume = not args.no_resume

        mounts, stats = generate_from_import_csv(
            csv_path=csv_path_str,
            logger=logger,
            bucket_suffix=args.bucket_suffix,
            hostname=args.hostname,
            port=args.port,
            region=args.region,
            use_ssl=args.use_ssl,
            use_path_style=not args.no_path_style,
            resolve_nextcloud_uid=not args.no_nextcloud_uid_lookup,
            state_path=state_path if resume else None,
            resume=resume,
            mount_point_style=args.mount_point_style,
        )

        if not mounts:
            logger.error("❌ 未產生任何掛載設定")
            return 1

        save_mounts_json(mounts, args.output)

        # 寫入最終統計到記錄檔案
        logger.info("")
        logger.info(f"=== 執行記錄結束: {datetime.now().isoformat()} ===")
        logger.info(f"總筆數: {stats['total']}")
        logger.info(f"跳過: {stats['skipped']} 筆")
        logger.info(f"新增: {stats['new']} 筆")
        logger.info(f"更新: {stats['updated']} 筆")
        logger.info(f"錯誤: {stats['errors']} 筆")

        logger.info("")
        if log_path:
            logger.info(f"📝 記錄檔案: {log_path}")
        if resume:
            logger.info(f"💾 狀態檔案: {state_path}")

        return 0

    except Exception as e:
        # 若 logger 尚未初始化，使用 print 作為後備方案
        logger = logging.getLogger("gen_external_storage")
        if logger.handlers:
            logger.error("")
            logger.error(f"❌ 發生錯誤: {e}")
            logger.error(traceback.format_exc())
        else:
            print(f"❌ 發生錯誤: {e}", file=sys.stderr)
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
