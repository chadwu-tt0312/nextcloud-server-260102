"""
匯出 AD 使用者至 CSV，篩選條件與 Nextcloud ldapUserFilter 對齊：
  真人使用者 (objectCategory=person, objectClass=user, sAMAccountName=*)  OR  部門帳號。

Nextcloud 設定同一條件（依本檔 DEPT_* 設定擇一）：
  occ: php occ ldap:set-config s01 ldapUserFilter "<這裡用 _build_ldap_filter() 輸出的字串>"
  例（僅真人）: (&(objectCategory=person)(objectClass=user)(sAMAccountName=*))
  例（真人 OR 部門群組）: (|(&(objectCategory=person)(objectClass=user)(sAMAccountName=*))(memberOf=CN=Dept,OU=...,DC=UMC,DC=com))
  例（真人 OR 前綴 DEPT_）: (|(&(objectCategory=person)(objectClass=user)(sAMAccountName=*))(sAMAccountName=DEPT_*))
"""
import csv

from ldap3 import ALL, SUBTREE, Connection, Server

# --- 1. 配置區域 ---
AD_SERVER = "ldap://your_domain_controller"
AD_USER = "DOMAIN\\Administrator"
AD_PASSWORD = "YourPassword"
SEARCH_BASE = "DC=example,DC=com"
OUTPUT_FILE = "ad_users_list.csv"

# --- 2. LDAP 使用者篩選（與 Nextcloud ldapUserFilter 對齊）---
# 真人使用者：objectCategory=person、objectClass=user、具 sAMAccountName
USER_FILTER = "(&(objectCategory=person)(objectClass=user)(sAMAccountName=*))"

# 部門帳號條件（擇一或自訂，再與 USER_FILTER 用 OR 合併）：
# 方式 A：依「群組」— 屬於某個部門帳號群組的物件（請改為貴公司群組 DN）
DEPT_GROUP_DN = "CN=DepartmentAccounts,OU=Groups,DC=UMC,DC=com"  # 若不用請設為 None
# 方式 B：依「sAMAccountName 前綴」— 例如 DEPT_、SMG_ 開頭（若不用請設為 None）
DEPT_SAM_PREFIX = None  # 例如 "DEPT_" 或 "SMG_"

def _build_ldap_filter():
    """組出與 Nextcloud ldapUserFilter 一致的篩選：真人使用者 OR 部門帳號。"""
    conditions = [USER_FILTER]
    if DEPT_GROUP_DN:
        conditions.append(f"(memberOf={DEPT_GROUP_DN})")
    if DEPT_SAM_PREFIX:
        # sAMAccountName 前綴比對（AD 可用 * 萬用字元）
        conditions.append(f"(sAMAccountName={DEPT_SAM_PREFIX}*)")
    if len(conditions) == 1:
        return conditions[0]
    return "(|" + "".join(conditions) + ")"


def export_ad_users_to_csv():
    # 定義伺服器
    server = Server(AD_SERVER, get_info=ALL)

    try:
        # 建立連線
        with Connection(server, user=AD_USER, password=AD_PASSWORD, auto_bind=True) as conn:
            # AD 過濾器：真人使用者 + 部門帳號（與 Nextcloud ldapUserFilter 一致）
            search_filter = _build_ldap_filter()

            # 指定屬性：增加 'mail'
            attrs = ["sAMAccountName", "displayName", "department", "mail"]

            # 使用 paged_search 處理超過 1000 筆數據
            # paged_size 建議設定 100-500 之間
            entry_generator = conn.extend.standard.paged_search(
                search_base=SEARCH_BASE,
                search_filter=search_filter,
                search_scope=SUBTREE,
                attributes=attrs,
                paged_size=500,
                generator=True,
            )

            # 準備寫入 CSV (使用 utf-8-sig 確保 Excel 開啟不亂碼)
            with open(OUTPUT_FILE, mode="w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                # 寫入表頭
                writer.writerow(["帳號", "姓名", "部門", "Email"])

                count = 0
                for entry in entry_generator:
                    # 檢查 attributes 是否存在，避免 KeyError
                    curr_attrs = entry.get("attributes", {})

                    sam_account_name = curr_attrs.get("sAMAccountName")
                    if isinstance(sam_account_name, list):
                        sam_account_name = sam_account_name[0] if sam_account_name else ""
                    if isinstance(sam_account_name, str):
                        sam_account_name = sam_account_name.strip()
                    if not sam_account_name:
                        continue

                    row = [
                        sam_account_name,
                        curr_attrs.get("displayName", ""),
                        curr_attrs.get("department", ""),
                        curr_attrs.get("mail", ""),
                    ]
                    writer.writerow(row)
                    count += 1

            print(f"成功匯出 {count} 筆使用者資料至 {OUTPUT_FILE}")

    except Exception as e:
        print(f"執行過程中發生錯誤: {e}")


if __name__ == "__main__":
    export_ad_users_to_csv()
