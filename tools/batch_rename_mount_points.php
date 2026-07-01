<?php

/**
 * Nextcloud 批次重新命名外部儲存掛載點（單次 bootstrap）
 *
 * 命名規則：
 *   /minio-{Emp_no}           → /個人-{Emp_no}（每位使用者最多 1 個）
 *   /minio-DEPT_{Dept-Name}   → /部門-{Dept_Name}（每位使用者可 0～n 個）
 *
 * config JSON：
 * {
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

$backendFilter = $cfg['backend'] ?? 'amazons3';
$dryRun = (bool)($cfg['dry_run'] ?? false);
$limit = (int)($cfg['limit'] ?? 0);
$progressInterval = max(1, (int)($cfg['progress_interval'] ?? 500));
$bucketSuffix = $cfg['bucket_suffix'] ?? '-filespace';

function out(array $row): void {
	fwrite(STDOUT, json_encode($row, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n");
}

function normalizeMount(string $mp): string {
	return str_starts_with($mp, '/') ? $mp : '/' . $mp;
}

function isLabeledPersonal(string $mp): bool {
	return str_starts_with(normalizeMount($mp), '/個人-');
}

function isLabeledDepartment(string $mp): bool {
	return str_starts_with(normalizeMount($mp), '/部門-');
}

function isLabeled(string $mp): bool {
	return isLabeledPersonal($mp) || isLabeledDepartment($mp);
}

function accountLabelFromBucket(string $bucket, string $suffix): ?string {
	$bucket = trim($bucket);
	if ($bucket === '') {
		return null;
	}
	if ($suffix !== '' && str_ends_with($bucket, $suffix)) {
		$label = trim(substr($bucket, 0, -strlen($suffix)));
		return $label !== '' ? $label : null;
	}
	return null;
}

function personalLabelFromMount(string $mountPoint, string $bucket, string $suffix): ?string {
	$label = accountLabelFromBucket($bucket, $suffix);
	if ($label !== null) {
		return $label;
	}
	$m = ltrim($mountPoint, '/');
	if (str_starts_with($m, 'minio-') && !str_starts_with($m, 'minio-DEPT_')) {
		return substr($m, strlen('minio-'));
	}
	return null;
}

function departmentLabelFromBucket(string $bucket, string $suffix): ?string {
	$name = accountLabelFromBucket($bucket, $suffix);
	if ($name === null) {
		return null;
	}
	$lower = strtolower($name);
	foreach (['dept-', 'dept_', 'dep-', 'dep_'] as $prefix) {
		if (str_starts_with($lower, $prefix)) {
			$name = substr($name, strlen($prefix));
			break;
		}
	}
	$label = strtoupper(str_replace(['-', '.'], '_', $name));
	return $label !== '' ? $label : null;
}

function isGenericPersonal(string $mp): bool {
	return normalizeMount($mp) === '/個人雲端硬碟';
}

function isGenericDepartment(string $mp): bool {
	return normalizeMount($mp) === '/部門雲端硬碟';
}

function departmentLabelFromMount(string $mountPoint, array $groups): ?string {
	$m = ltrim($mountPoint, '/');
	if (str_starts_with($m, 'minio-DEPT_')) {
		return str_replace('-', '_', substr($m, strlen('minio-DEPT_')));
	}
	foreach ($groups as $group) {
		if (str_starts_with(strtolower($group), 'dept_')) {
			return str_replace('-', '_', substr($group, 5));
		}
	}
	return null;
}

/**
 * @param \OCA\Files_External\Lib\StorageConfig $mount
 */
function classifyMount($mount, string $bucketSuffix): ?string {
	$old = normalizeMount($mount->getMountPoint());
	if (isLabeled($old)) {
		return null;
	}
	$bucket = (string)($mount->getBackendOption('bucket') ?? '');
	$groups = $mount->getApplicableGroups();

	if (isGenericPersonal($old)) {
		$label = personalLabelFromMount($old, $bucket, $bucketSuffix);
		return $label !== null ? '/個人-' . $label : null;
	}
	if (isGenericDepartment($old)) {
		$label = departmentLabelFromBucket($bucket, $bucketSuffix) ?? departmentLabelFromMount($old, $groups);
		return $label !== null ? '/部門-' . strtoupper($label) : null;
	}
	if (str_starts_with($old, '/minio-DEPT_')) {
		$label = departmentLabelFromMount($old, $groups);
		return $label !== null ? '/部門-' . strtoupper($label) : null;
	}
	if (str_starts_with($old, '/minio-')) {
		$label = personalLabelFromMount($old, $bucket, $bucketSuffix);
		return $label !== null ? '/個人-' . $label : null;
	}
	return null;
}

/**
 * @param \OCA\Files_External\Lib\StorageConfig $mount
 * @return list<string>
 */
function mountIdentities($mount): array {
	$users = array_values(array_filter(
		$mount->getApplicableUsers(),
		static fn (string $u): bool => $u !== 'admin'
	));
	if (count($users) > 0) {
		return $users;
	}
	$groups = $mount->getApplicableGroups();
	if (count($groups) > 0) {
		return $groups;
	}
	return ['__global__'];
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

$existingPersonal = [];
$personalCandidates = [];
$targetCounts = [];
$planned = [];

foreach ($allMounts as $mount) {
	$backend = $mount->getBackend();
	if ($backend === null || $backend->getIdentifier() !== $backendFilter) {
		continue;
	}

	$oldPoint = normalizeMount($mount->getMountPoint());
	$identities = mountIdentities($mount);

	if (isLabeledPersonal($oldPoint)) {
		foreach ($identities as $uid) {
			$existingPersonal[$uid] = ($existingPersonal[$uid] ?? 0) + 1;
		}
		$stats['already']++;
		continue;
	}

	$newPoint = classifyMount($mount, $bucketSuffix);
	if ($newPoint === null) {
		if (isLabeledDepartment($oldPoint)) {
			$stats['already']++;
		} else {
			$stats['skipped']++;
		}
		continue;
	}

	$stats['eligible']++;
	$isPersonal = str_starts_with($newPoint, '/個人-');

	$planned[] = [
		'mount' => $mount,
		'old' => $oldPoint,
		'new' => $newPoint,
		'users' => $identities,
		'is_personal' => $isPersonal,
	];

	foreach ($identities as $uid) {
		if ($isPersonal) {
			$personalCandidates[$uid] = ($personalCandidates[$uid] ?? 0) + 1;
		}
		$key = $uid . "\0" . $newPoint;
		$targetCounts[$key] = ($targetCounts[$key] ?? 0) + 1;
	}
}

$duplicateTarget = [];
foreach ($targetCounts as $key => $count) {
	if ($count > 1) {
		$duplicateTarget[$key] = true;
	}
}

$personalConflictUsers = [];
foreach ($personalCandidates as $uid => $count) {
	if ($count + ($existingPersonal[$uid] ?? 0) > 1) {
		$personalConflictUsers[$uid] = true;
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
	$isPersonal = $item['is_personal'];

	$hasConflict = false;
	$error = '';
	foreach ($users as $uid) {
		if ($isPersonal && isset($personalConflictUsers[$uid])) {
			$hasConflict = true;
			$error = 'conflict: 同一使用者只能有一個個人外部磁碟';
			break;
		}
		$key = $uid . "\0" . $newPoint;
		if (isset($duplicateTarget[$key])) {
			$hasConflict = true;
			$error = 'conflict: 同一使用者將有多個相同掛載點名稱';
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
			'error' => $error,
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
