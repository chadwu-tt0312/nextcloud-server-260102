<?php

/**
 * Nextcloud 批次重新命名外部儲存掛載點（單次 bootstrap）
 *
 * 用途：將 minio-* 掛載點改為使用者友善名稱：
 *   - /minio-{Emp_no}        → /個人雲端硬碟
 *   - /minio-DEPT_{Dept}     → /部門雲端硬碟
 *
 * 同一使用者可同時擁有「個人雲端硬碟」與「部門雲端硬碟」；
 * 衝突僅在「同一使用者 + 相同目標名稱」出現多筆時（例如兩個個人掛載）。
 *
 * 為何用 PHP（容器內執行）：
 *   - REST API PUT 需 PasswordConfirmation，不適合 1.7 萬筆自動化
 *   - occ files_external:config 逐筆會 bootstrap 上萬次
 *   - 本腳本只 bootstrap 一次，透過 GlobalStoragesService::updateStorage 正確觸發掛載 hook
 *
 * 執行（在容器內，以 www-data 身分）：
 *   php /tmp/batch_rename_mount_points.php /tmp/rename_mount_config.json
 *
 * config JSON 格式：
 * {
 *   "personal_name": "/個人雲端硬碟",
 *   "department_name": "/部門雲端硬碟",
 *   "backend": "amazons3",
 *   "dry_run": true,
 *   "limit": 0,
 *   "progress_interval": 500
 * }
 */

if (PHP_SAPI !== 'cli') {
	fwrite(STDERR, "此腳本僅能在 CLI 執行\n");
	exit(1);
}

$configPath = $argv[1] ?? '';
if ($configPath === '' || !is_file($configPath)) {
	fwrite(STDERR, "用法: php batch_rename_mount_points.php <config.json>\n");
	exit(1);
}

$cfg = json_decode(file_get_contents($configPath), true);
if (!is_array($cfg)) {
	fwrite(STDERR, "config JSON 解析失敗: {$configPath}\n");
	exit(1);
}

$personalName = $cfg['personal_name'] ?? '/個人雲端硬碟';
$departmentName = $cfg['department_name'] ?? '/部門雲端硬碟';
$backendFilter = $cfg['backend'] ?? 'amazons3';
$dryRun = (bool)($cfg['dry_run'] ?? false);
$limit = (int)($cfg['limit'] ?? 0);
$progressInterval = max(1, (int)($cfg['progress_interval'] ?? 500));

function out(array $row): void {
	fwrite(STDOUT, json_encode($row, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n");
}

/**
 * @return string|null 新掛載點，null 表示略過
 */
function classifyMountPoint(
	string $current,
	string $personalName,
	string $departmentName
): ?string {
	$mount = $current;
	if ($mount !== '' && $mount[0] !== '/') {
		$mount = '/' . $mount;
	}
	if ($mount === $personalName || $mount === $departmentName) {
		return null;
	}
	if (str_starts_with($mount, '/minio-DEPT_')) {
		return $departmentName;
	}
	if (str_starts_with($mount, '/minio-')) {
		return $personalName;
	}
	return null;
}

$base = getenv('NC_BASE') ?: '/var/www/html/lib/base.php';
if (!is_file($base)) {
	fwrite(STDERR, "找不到 Nextcloud base.php: {$base}（可用環境變數 NC_BASE 指定）\n");
	exit(1);
}
require_once $base;

\OC_App::loadApps(['files_external']);

/** @var \OCA\Files_External\Service\GlobalStoragesService $service */
$service = \OCP\Server::get(\OCA\Files_External\Service\GlobalStoragesService::class);

$stats = [
	'total' => 0,
	'eligible' => 0,
	'updated' => 0,
	'skipped' => 0,
	'already' => 0,
	'conflicts' => 0,
	'errors' => 0,
	'dry_run' => $dryRun,
];

try {
	$allMounts = $service->getAllStorages();
} catch (\Throwable $e) {
	fwrite(STDERR, "無法取得外部儲存列表: {$e->getMessage()}\n");
	exit(1);
}

$stats['total'] = count($allMounts);

// 預先掃描：同一使用者若會有兩個相同新掛載點，標記衝突
$userTargetCounts = [];
$planned = [];

foreach ($allMounts as $mount) {
	$backend = $mount->getBackend();
	if ($backend === null || $backend->getIdentifier() !== $backendFilter) {
		continue;
	}

	$oldPoint = $mount->getMountPoint();
	$newPoint = classifyMountPoint($oldPoint, $personalName, $departmentName);
	if ($newPoint === null) {
		if ($oldPoint === $personalName || $oldPoint === $departmentName) {
			$stats['already']++;
		} else {
			$stats['skipped']++;
		}
		continue;
	}

	$stats['eligible']++;
	$users = $mount->getApplicableUsers();
	if (count($users) === 0) {
		$users = ['__global__'];
	}

	$planned[] = [
		'mount' => $mount,
		'old' => $oldPoint,
		'new' => $newPoint,
		'users' => $users,
	];

	foreach ($users as $uid) {
		$key = $uid . "\0" . $newPoint;
		$userTargetCounts[$key] = ($userTargetCounts[$key] ?? 0) + 1;
	}
}

$conflictKeys = [];
foreach ($userTargetCounts as $key => $count) {
	if ($count > 1) {
		$conflictKeys[$key] = true;
	}
}

$processed = 0;
foreach ($planned as $item) {
	if ($limit > 0 && $processed >= $limit) {
		break;
	}

	/** @var \OCA\Files_External\Lib\StorageConfig $mount */
	$mount = $item['mount'];
	$oldPoint = $item['old'];
	$newPoint = $item['new'];
	$users = $item['users'];

	$hasConflict = false;
	foreach ($users as $uid) {
		$key = $uid . "\0" . $newPoint;
		if (isset($conflictKeys[$key])) {
			$hasConflict = true;
			break;
		}
	}

	if ($hasConflict) {
		$stats['conflicts']++;
		out([
			'ok' => false,
			'id' => $mount->getId(),
			'old' => $oldPoint,
			'new' => $newPoint,
			'users' => $users,
			'error' => 'conflict: 同一使用者將有多個相同掛載點名稱',
		]);
		$processed++;
		continue;
	}

	if ($dryRun) {
		$stats['updated']++;
		out([
			'ok' => true,
			'dry_run' => true,
			'id' => $mount->getId(),
			'old' => $oldPoint,
			'new' => $newPoint,
			'users' => $users,
		]);
		$processed++;
		if ($processed % $progressInterval === 0) {
			out(['progress' => $processed, 'eligible' => $stats['eligible']]);
		}
		continue;
	}

	try {
		$mount->setMountPoint($newPoint);
		$service->updateStorage($mount);
		$stats['updated']++;
		out([
			'ok' => true,
			'id' => $mount->getId(),
			'old' => $oldPoint,
			'new' => $newPoint,
			'users' => $users,
		]);
	} catch (\Throwable $e) {
		$stats['errors']++;
		out([
			'ok' => false,
			'id' => $mount->getId(),
			'old' => $oldPoint,
			'new' => $newPoint,
			'users' => $users,
			'error' => $e->getMessage(),
		]);
	}

	$processed++;
	if ($processed % $progressInterval === 0) {
		out(['progress' => $processed, 'eligible' => $stats['eligible']]);
	}
}

out(['summary' => $stats]);
