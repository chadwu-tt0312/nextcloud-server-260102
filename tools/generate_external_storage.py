#!/usr/bin/env python3
"""
Nextcloud External Storage Configuration Generator

產生 Nextcloud files_external:import 所需的 JSON 掛載設定檔。
支援從 CSV 讀取使用者清單與 AccessKey/SecretKey。

用法:
    python generate_external_storage.py --help
    python generate_external_storage.py --csv users.csv --output mounts.json
    python generate_external_storage.py --import-csv import-accounts.csv --output mounts.json

CSV 格式範例 (users.csv):
    user_id,bucket_name,access_key,secret_key
    minio-user00001,minio-user00001-filespace,ACCESS_KEY,SECRET_KEY
    minio-user00002,minio-user00002-filespace,,  # 空白時使用環境變數

CSV 格式範例 (import-accounts.csv):
    Dept_name,Emp_no,Capacity
    SMG_ARC1,,10GiB
    SMG_ARC5,00059094,2GiB
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

# 載入 .env 環境變數
load_dotenv()


def get_env_config() -> dict[str, str]:
    """
    從環境變數讀取 MinIO 連線設定

    Returns:
        包含 hostname, port, region, access_key, secret_key 的字典
    """
    minio_url = os.getenv("ENV_MINIO_URL", "http://localhost:9000")
    parsed = urlparse(minio_url)

    return {
        "hostname": parsed.hostname or "localhost",
        "port": str(parsed.port or 9000),
        "region": os.getenv("ENV_REGION", "us-east-1"),
        "access_key": os.getenv("ENV_MINIO_ACCESS_KEY", ""),
        "secret_key": os.getenv("ENV_MINIO_SECRET_KEY", ""),
    }


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
) -> dict[str, Any]:
    """
    產生單筆 Nextcloud External Storage 掛載設定

    Args:
        mount_id: 掛載 ID（用於 import 時識別）
        user_id: 使用者 ID（也作為掛載點名稱）
        bucket_name: S3 Bucket 名稱
        access_key: S3 Access Key
        secret_key: S3 Secret Key
        hostname: MinIO/S3 主機位址
        port: 連接埠
        region: AWS Region
        use_ssl: 是否啟用 SSL
        use_path_style: 是否啟用 Path Style
        applicable_users: 適用使用者清單（空陣列表示所有使用者）
        applicable_groups: 適用群組清單

    Returns:
        掛載設定字典
    """
    return {
        "mount_id": mount_id,
        "mount_point": f"/{user_id}",
        "storage": "\\OCA\\Files_External\\Lib\\Storage\\AmazonS3",
        "authentication_type": "amazons3::accesskey",
        "configuration": {
            "bucket": bucket_name,
            "hostname": hostname,
            "port": port,
            "region": region,
            "use_ssl": use_ssl,
            "use_path_style": use_path_style,
            "key": access_key,
            "secret": secret_key,
        },
        "options": {},
        "applicable_users": applicable_users or [],
        "applicable_groups": applicable_groups or [],
    }


def generate_from_csv(
    csv_path: str,
    bucket_suffix: str = "-filespace",
    hostname: str | None = None,
    port: str | None = None,
    region: str | None = None,
    use_ssl: bool = False,
    use_path_style: bool = True,
) -> list[dict[str, Any]]:
    """
    從 CSV 檔案讀取使用者清單並產生掛載設定

    CSV 欄位:
        - user_id: 使用者 ID（必填）
        - bucket_name: Bucket 名稱（選填，預設為 {user_id}{bucket_suffix}）
        - access_key: Access Key（選填，空白時使用環境變數）
        - secret_key: Secret Key（選填，空白時使用環境變數）

    Args:
        csv_path: CSV 檔案路徑
        bucket_suffix: Bucket 名稱後綴（當 CSV 未提供 bucket_name 時使用）
        hostname: MinIO/S3 主機位址（None 時從環境變數讀取）
        port: 連接埠（None 時從環境變數讀取）
        region: AWS Region（None 時從環境變數讀取）
        use_ssl: 是否啟用 SSL
        use_path_style: 是否啟用 Path Style

    Returns:
        掛載設定清單
    """
    env_config = get_env_config()
    mounts = []

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for i, row in enumerate(reader, start=1):
            user_id = row["user_id"].strip()
            bucket_name = row.get("bucket_name", "").strip() or f"{user_id}{bucket_suffix}"
            access_key = row.get("access_key", "").strip() or env_config["access_key"]
            secret_key = row.get("secret_key", "").strip() or env_config["secret_key"]

            mount = generate_mount_config(
                mount_id=i,
                user_id=user_id,
                bucket_name=bucket_name,
                access_key=access_key,
                secret_key=secret_key,
                hostname=hostname or env_config["hostname"],
                port=port or env_config["port"],
                region=region or env_config["region"],
                use_ssl=use_ssl,
                use_path_style=use_path_style,
            )
            mounts.append(mount)

    return mounts


def generate_from_import_csv(
    csv_path: str,
    bucket_suffix: str = "-filespace",
    hostname: str | None = None,
    port: str | None = None,
    region: str | None = None,
    use_ssl: bool = False,
    use_path_style: bool = True,
) -> list[dict[str, Any]]:
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

    Returns:
        掛載設定清單
    """
    env_config = get_env_config()
    mounts = []

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for i, row in enumerate(reader, start=1):
            emp_no = row.get("Emp_no", "").strip()
            dept_name = row.get("Dept_name", "").strip()

            # 組裝 user_id
            if emp_no:
                user_id = f"minio-{emp_no}"
            else:
                user_id = f"minio-DEPT_{dept_name}"

            bucket_name = f"{user_id}{bucket_suffix}"

            mount = generate_mount_config(
                mount_id=i,
                user_id=user_id,
                bucket_name=bucket_name,
                access_key=env_config["access_key"],
                secret_key=env_config["secret_key"],
                hostname=hostname or env_config["hostname"],
                port=port or env_config["port"],
                region=region or env_config["region"],
                use_ssl=use_ssl,
                use_path_style=use_path_style,
            )
            mounts.append(mount)

    return mounts


def save_mounts_json(mounts: list[dict[str, Any]], output_path: str) -> None:
    """
    儲存掛載設定為 JSON 檔案

    Args:
        mounts: 掛載設定清單
        output_path: 輸出檔案路徑
    """
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(mounts, f, indent=2, ensure_ascii=False)

    print(f"✅ 已產生 {len(mounts)} 個掛載設定至 {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nextcloud External Storage Configuration Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例:
  # 從 users.csv 讀取使用者清單（欄位: user_id,bucket_name,access_key,secret_key）
  python generate_external_storage.py --csv users.csv --output mounts.json

  # 從 import-accounts.csv 讀取（欄位: Dept_name,Emp_no,Capacity）
  python generate_external_storage.py --import-csv import-accounts.csv --output mounts.json

  # 自訂 MinIO 連線設定（覆蓋環境變數）
  python generate_external_storage.py --csv users.csv \\
      --hostname minio.example.com --port 443 --use-ssl --output mounts.json

匯入指令:
  php occ files_external:import --dry mounts.json  # 預覽
  php occ files_external:import mounts.json        # 正式匯入
        """,
    )

    # 資料來源（二擇一）
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--csv",
        type=str,
        help="CSV 檔案路徑（欄位: user_id, bucket_name, access_key, secret_key）",
    )
    source_group.add_argument(
        "--import-csv",
        type=str,
        dest="import_csv",
        help="import-accounts.csv 格式檔案路徑（欄位: Dept_name, Emp_no）",
    )

    # 輸出設定
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="external_storage_mounts.json",
        help="輸出 JSON 檔案路徑（預設: external_storage_mounts.json）",
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

    # Bucket 命名設定
    parser.add_argument(
        "--bucket-suffix",
        type=str,
        default="-filespace",
        help="Bucket 名稱後綴（預設: -filespace）",
    )

    args = parser.parse_args()

    # 產生掛載設定
    try:
        if args.csv:
            csv_path = Path(args.csv)
            if not csv_path.exists():
                print(f"❌ CSV 檔案不存在: {args.csv}", file=sys.stderr)
                return 1

            mounts = generate_from_csv(
                csv_path=args.csv,
                bucket_suffix=args.bucket_suffix,
                hostname=args.hostname,
                port=args.port,
                region=args.region,
                use_ssl=args.use_ssl,
                use_path_style=not args.no_path_style,
            )
        else:
            csv_path = Path(args.import_csv)
            if not csv_path.exists():
                print(f"❌ CSV 檔案不存在: {args.import_csv}", file=sys.stderr)
                return 1

            mounts = generate_from_import_csv(
                csv_path=args.import_csv,
                bucket_suffix=args.bucket_suffix,
                hostname=args.hostname,
                port=args.port,
                region=args.region,
                use_ssl=args.use_ssl,
                use_path_style=not args.no_path_style,
            )

        if not mounts:
            print("❌ 未產生任何掛載設定", file=sys.stderr)
            return 1

        save_mounts_json(mounts, args.output)
        return 0

    except Exception as e:
        print(f"❌ 發生錯誤: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
