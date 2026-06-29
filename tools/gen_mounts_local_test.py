#!/usr/bin/env python3
"""
產生本機 Docker 測試用 mounts.json

用法:
  tools/run_python.sh tools/gen_mounts_local_test.py -o docker/test/mounts-local-test-account.json
  tools/run_python.sh tools/gen_mounts_local_test.py --users chad --style account -o /tmp/mounts.json

需先在 tools/ 執行 uv sync 建立 .venv（內含 httpx 等相依套件）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools_mount_point import (
    MOUNT_POINT_STYLE_ACCOUNT,
    MOUNT_POINT_STYLE_DISPLAY,
    resolve_mount_point,
)

DEFAULT_USERS = ("chad", "usr01", "00059094")
DEFAULT_MINIO = {
    "hostname": "minio1",
    "port": "9000",
    "region": "us-east-1",
    "use_ssl": False,
    "use_path_style": True,
    "key": "minioadmin",
    "secret": "minioadminpw",
}


def build_local_mount(
    account: str,
    mount_id: int,
    *,
    style: str,
) -> dict:
    bucket = f"{account}-filespace"
    applicable_users = [account]
    # display 模式沿用 minio-{帳號} 慣例，才會對應 /個人雲端硬碟
    user_id = f"minio-{account}" if style == MOUNT_POINT_STYLE_DISPLAY else account
    return {
        "mount_id": mount_id,
        "mount_point": resolve_mount_point(
            user_id,
            style=style,
            applicable_users=applicable_users,
            bucket=bucket,
        ),
        "storage": "\\OCA\\Files_External\\Lib\\Storage\\AmazonS3",
        "authentication_type": "amazons3::accesskey",
        "configuration": {
            "bucket": bucket,
            **DEFAULT_MINIO,
        },
        "options": {
            "filesystem_check_changes": 1,
        },
        "applicable_users": applicable_users,
        "applicable_groups": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="產生本機測試用 external storage JSON")
    parser.add_argument(
        "--users",
        type=str,
        default=",".join(DEFAULT_USERS),
        help="逗號分隔帳號（預設: chad,usr01,00059094）",
    )
    parser.add_argument(
        "--style",
        choices=[MOUNT_POINT_STYLE_DISPLAY, MOUNT_POINT_STYLE_ACCOUNT],
        default=MOUNT_POINT_STYLE_DISPLAY,
        help="display=/個人雲端硬碟（預設）；account=每人資料夾（名稱取自 bucket）",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="-",
        help="輸出路徑（預設 stdout）",
    )
    args = parser.parse_args()

    users = [u.strip() for u in args.users.split(",") if u.strip()]
    mounts = [
        build_local_mount(user, mount_id=i + 1, style=args.style)
        for i, user in enumerate(users)
    ]

    text = json.dumps(mounts, indent=2, ensure_ascii=False) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"✅ 已寫入 {len(mounts)} 筆 → {args.output} (style={args.style})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
