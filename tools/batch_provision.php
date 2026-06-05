<?php

/**
 * Nextcloud 批次帳號處理腳本（單次 bootstrap，內部迴圈）
 *
 * 用途：對大量帳號（例如 1 萬名）一次性完成：
 *   1. 清除 home 內的 skeleton 範本檔（或清空整個 home）
 *   2. 設定 quota（例如 0 B）
 *   3. 取消「檔案設定 → 其他設定」兩個勾選（recommendations + text）
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
 *   "user_settings": {                  // 要設定的 per-user 偏好（app => { key => value }）
 *     "recommendations": { "enabled": "0" },
 *     "text": { "workspace_enabled": "0" }
 *   },
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
$userSettings = $cfg['user_settings'] ?? [];
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

/**
 * 解析 skeleton 路徑（支援 {lang} 後綴，與 OC_Util::copySkeleton 邏輯對齊）
 */
function resolveSkeletonPath(string $plainPath, string $userLang = 'default'): string {
	if (strpos($plainPath, '{lang}') === false) {
		return $plainPath;
	}
	$candidates = [$userLang];
	$dialectStart = strpos($userLang, '_');
	if ($dialectStart !== false) {
		$candidates[] = substr($userLang, 0, $dialectStart);
	}
	$candidates[] = 'default';
	foreach ($candidates as $lang) {
		$path = str_replace('{lang}', $lang, $plainPath);
		if (is_dir($path)) {
			return $path;
		}
	}
	return str_replace('{lang}', 'default', $plainPath);
}

/**
 * 從 core/skeleton（及自訂 skeletondirectory）掃描頂層檔名，併入白名單。
 * 即使已設 skeletondirectory=""，仍掃描預設路徑以清除舊帳號殘留（如 Templates credits.md）。
 */
function getSkeletonTopLevelNames(\OCP\IConfig $config, array $fallbackNames): array {
	$names = $fallbackNames;
	$serverRoot = \OC::$SERVERROOT;
	$defaultSkeleton = $serverRoot . '/core/skeleton';
	$configured = $config->getSystemValueString('skeletondirectory', $defaultSkeleton);

	$pathsToScan = [$defaultSkeleton];
	if ($configured !== '' && $configured !== $defaultSkeleton) {
		$pathsToScan[] = resolveSkeletonPath($configured);
	}

	foreach ($pathsToScan as $path) {
		if ($path === '' || !is_dir($path)) {
			continue;
		}
		$entries = scandir($path);
		if ($entries === false) {
			continue;
		}
		foreach ($entries as $entry) {
			if ($entry === '.' || $entry === '..') {
				continue;
			}
			$names[] = $entry;
		}
	}

	// Templates 資料夾會依語系重新命名（例如 zh_TW →「範本」），一併納入
	try {
		$l10nFactory = \OCP\Server::get(\OCP\L10N\IFactory::class);
		foreach (['en', 'zh_TW', 'zh_CN', 'de', 'fr', 'ja'] as $lang) {
			$l10n = $l10nFactory->get('lib', $lang);
			$localized = $l10n->t('Templates');
			if ($localized !== '' && $localized !== 'Templates') {
				$names[] = $localized;
			}
		}
	} catch (\Throwable $e) {
		// 略過：fallback 清單仍可用
	}

	return array_values(array_unique($names));
}

/**
 * skeleton 模式：是否應刪除此頂層項目（白名單 + 已知範本殘留 pattern）
 */
function shouldDeleteSkeletonEntry(string $name, array $skeletonNames): bool {
	if (in_array($name, $skeletonNames, true)) {
		return true;
	}
	// 範本授權說明（如 Templates credits.md），可能不在精簡版 core/skeleton 掃描結果中
	if (str_ends_with($name, ' credits.md')) {
		return true;
	}
	return false;
}

$stats = ['total' => count($users), 'ok' => 0, 'failed' => 0, 'deleted_files' => 0];
$start = microtime(true);

if ($clearFiles === 'skeleton') {
	$skeletonNames = getSkeletonTopLevelNames($config, $skeletonNames);
}

foreach ($users as $uid) {
	$result = ['uid' => $uid, 'ok' => true, 'deleted' => [], 'quota' => null, 'user_settings' => [], 'errors' => []];

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
			$listing = $userFolder->getDirectoryListing();
			foreach ($listing as $node) {
				$name = $node->getName();
				$shouldDelete = ($clearFiles === 'all')
					|| ($clearFiles === 'skeleton' && shouldDeleteSkeletonEntry($name, $skeletonNames));
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
			// 清檔後回報 home 頂層殘留，方便驗證
			$result['remaining'] = array_map(
				static fn ($n) => $n->getName(),
				$dryRun ? $listing : $userFolder->getDirectoryListing()
			);
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

	// 3) 設定 per-user 偏好（Q2：其他設定兩個勾選）
	foreach ($userSettings as $appId => $settings) {
		if (!is_array($settings)) {
			$result['ok'] = false;
			$result['errors'][] = "user_settings.{$appId}: expected object";
			continue;
		}
		foreach ($settings as $key => $value) {
			try {
				if (!$dryRun) {
					$config->setUserValue($uid, (string)$appId, (string)$key, (string)$value);
				}
				$result['user_settings'][(string)$appId][(string)$key] = (string)$value;
			} catch (\Throwable $e) {
				$result['ok'] = false;
				$result['errors'][] = "user_setting {$appId}.{$key}: " . $e->getMessage();
			}
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
if ($clearFiles === 'skeleton') {
	$stats['skeleton_names_count'] = count($skeletonNames);
	$stats['skeleton_names_sample'] = array_slice($skeletonNames, 0, 15);
}
out(['summary' => $stats]);

exit($stats['failed'] === 0 ? 0 : 1);
