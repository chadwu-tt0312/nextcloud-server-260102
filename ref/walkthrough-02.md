# Walkthrough: generate_external_storage.py 修改

## 變更摘要

根據使用者需求，對 `generate_external_storage.py` 進行以下修改：

| 項目 | 說明 |
|------|------|
| 移除 `generate_from_range()` | 不再支援編號範圍產生，移除 `--start`、`--end`、`--bucket-prefix` 參數 |
| 環境變數整合 | 使用 `python-dotenv` 從 `.env` 讀取 `ENV_MINIO_URL`、`ENV_REGION`、`ENV_MINIO_ACCESS_KEY`、`ENV_MINIO_SECRET_KEY` |
| CSV 欄位順序 | 調整為 `user_id,bucket_name,access_key,secret_key`；空白時使用環境變數 |
| 新增 `--import-csv` | 支援 `import-accounts.csv` 格式（`Dept_name,Emp_no,Capacity`） |

---

## 修改的檔案

- [generate_external_storage.py](file:///d:/_Code/_GitHub/nextcloud-server-260102/tools/generate_external_storage.py)
- [users-sample.csv](file:///d:/_Code/_GitHub/nextcloud-server-260102/tools/users-sample.csv)

---

## 驗證結果

### 測試 `--csv users-sample.csv`

```bash
python generate_external_storage.py --csv users-sample.csv -o test_users.json
# ✅ 已產生 3 個掛載設定至 test_users.json
```

- 第 1、2 筆使用 CSV 內的 access_key/secret_key
- 第 3 筆（空白）正確使用環境變數的 key/secret

### 測試 `--import-csv import-accounts.csv`

```bash
python generate_external_storage.py --import-csv import-accounts.csv -o test_import.json
# ✅ 已產生 6 個掛載設定至 test_import.json
```

user_id 組裝結果：

| Dept_name | Emp_no | user_id |
|-----------|--------|---------|
| SMG_ARC1 | (空白) | `minio-DEPT_SMG_ARC1` |
| SMG_ARC5 | (空白) | `minio-DEPT_SMG_ARC5` |
| SMG_ARC5 | 00059094 | `minio-00059094` |
| SMG_ARC1 | 00059012 | `minio-00059012` |
| (空白) | SMG_ARC304 | `minio-SMG_ARC304` |
| (空白) | SMG_Arc305 | `minio-SMG_Arc305` |
