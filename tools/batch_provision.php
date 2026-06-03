<?php

/**
 * Nextcloud 批次帳號處理腳本（單次 bootstrap，內部迴圈）
 *
 * 用途：對大量帳號（例如 1 萬名）一次性完成：
 *   1. 清除 home 內的 skeleton 範本檔（或清空整個 home）
 *   2. 設定 quota（例如 0 B）
 *   3. 設定 Files app 個人偏好（例如取消「其他設定」勾選）
 *
 * 為何用 PHP：
 *   - 清除「其他使用者」的檔案無法用 OCS/WebDAV（admin 無他人 home 權限），
 *     只能透過 Nextcloud 檔案系統 API（IRootFolder->getUserFolder）。
 *   - occ 逐筆呼叫在 1 萬筆規模會 bootstrap 上萬次（數小時）；本腳本只
 *     bootstrap 一次，迴圈處理所有帳號，耗時數秒～數分。
 *
 * 執行（在容器內，以 www-data 身分）：
 *   php /tmp/batch_provision.php /tmp/batch_config.json
 *
 * config JSON 格式：
 * {
 *   "users": ["uid1", "uid2"],          // 必填，目標帳號
 *   "clear_files": "skeleton",          // skeleton | all | none（預設 none）
 *   "skeleton_names": ["Documents", ...],// skeleton 模式要刪的頂層名稱白名單
 *   "set_quota": "0 B",                 // null 表示不改 quota
 *   "prefs": { "folder_tree": "0" },    // 要設定的 files 偏好（key => value）
 *   "dry_run": true                      // true 時只試算不變更
 * }
 *
 * 輸出：每位帳號一行 JSON（stdout），最後一行為 summary。
 */

if (PHP_SAPI !== 'cli') {
	fwrite(STDERR, "此腳本僅能在 CLI 執行\n");
	exit(1);
}

// ---- 解析參數 ----
$configPath = $argv[1] ?? '';
if ($configPath === '' || !is_file($configPath)) {
	fwrite(STDERR, "用法: php batch_provision.php <config.json>\n");
	exit(1);
}

$raw = file_get_contents($configPath);
$cfg = json_decode($raw, true);
if (!is_array($cfg)) {
	fwrite(STDERR, "config JSON 解析失敗: {$configPath}\n");
	exit(1);
}

$users = $cfg['users'] ?? [];
$clearFiles = $cfg['clear_files'] ?? 'none';        // skeleton | all | none
$skeletonNames = $cfg['skeleton_names'] ?? [];
$setQuota = array_key_exists('set_quota', $cfg) ? $cfg['set_quota'] : null;
$prefs = $cfg['prefs'] ?? [];
$dryRun = (bool)($cfg['dry_run'] ?? false);

if (!is_array($users) || count($users) === 0) {
	fwrite(STDERR, "config 內 users 為空\n");
	exit(1);
}

// ---- Bootstrap Nextcloud ----
$base = getenv('NC_BASE') ?: '/var/www/html/lib/base.php';
if (!is_file($base)) {
	fwrite(STDERR, "找不到 Nextcloud base.php: {$base}（可用環境變數 NC_BASE 指定）\n");
	exit(1);
}
require_once $base;

\OC_App::loadApps();

/** @var \OCP\IUserManager $userManager */
$userManager = \OCP\Server::get(\OCP\IUserManager::class);
/** @var \OCP\Files\IRootFolder $rootFolder */
$rootFolder = \OCP\Server::get(\OCP\Files\IRootFolder::class);
/** @var \OCP\IConfig $config */
$config = \OCP\Server::get(\OCP\IConfig::class);

function out(array $row): void {
	fwrite(STDOUT, json_encode($row, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n");
}

$stats = ['total' => count($users), 'ok' => 0, 'failed' => 0, 'deleted_files' => 0];
$start = microtime(true);

foreach ($users as $uid) {
	$result = ['uid' => $uid, 'ok' => true, 'deleted' => [], 'quota' => null, 'prefs' => [], 'errors' => []];

	$user = $userManager->get($uid);
	if ($user === null) {
		$result['ok'] = false;
		$result['errors'][] = 'user not found';
		$stats['failed']++;
		out($result);
		continue;
	}

	// 1) 清除檔案
	if ($clearFiles === 'skeleton' || $clearFiles === 'all') {
		try {
			$userFolder = $rootFolder->getUserFolder($uid);
			foreach ($userFolder->getDirectoryListing() as $node) {
				$name = $node->getName();
				$shouldDelete = ($clearFiles === 'all')
					|| ($clearFiles === 'skeleton' && in_array($name, $skeletonNames, true));
				if (!$shouldDelete) {
					continue;
				}
				try {
					if (!$dryRun) {
						$node->delete();
					}
					$result['deleted'][] = $name;
					$stats['deleted_files']++;
				} catch (\Throwable $e) {
					$result['ok'] = false;
					$result['errors'][] = "delete {$name}: " . $e->getMessage();
				}
			}
		} catch (\Throwable $e) {
			$result['ok'] = false;
			$result['errors'][] = 'list/clear: ' . $e->getMessage();
		}
	}

	// 2) 設定 quota
	if ($setQuota !== null) {
		try {
			if (!$dryRun) {
				$user->setQuota((string)$setQuota);
			}
			$result['quota'] = (string)$setQuota;
		} catch (\Throwable $e) {
			$result['ok'] = false;
			$result['errors'][] = 'setQuota: ' . $e->getMessage();
		}
	}

	// 3) 設定 files 偏好
	foreach ($prefs as $key => $value) {
		try {
			if (!$dryRun) {
				$config->setUserValue($uid, 'files', (string)$key, (string)$value);
			}
			$result['prefs'][$key] = (string)$value;
		} catch (\Throwable $e) {
			$result['ok'] = false;
			$result['errors'][] = "pref {$key}: " . $e->getMessage();
		}
	}

	if ($result['ok']) {
		$stats['ok']++;
	} else {
		$stats['failed']++;
	}
	out($result);

	// 釋放此使用者的檔案系統設定，避免長迴圈累積記憶體 / mount
	\OC\Files\Filesystem::tearDown();
}

$stats['elapsed_sec'] = round(microtime(true) - $start, 2);
$stats['dry_run'] = $dryRun;
out(['summary' => $stats]);

exit($stats['failed'] === 0 ? 0 : 1);
