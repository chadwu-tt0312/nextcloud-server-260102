# 本機測試：MinIO bucket + Nextcloud「個人雲端硬碟」掛載（Windows PowerShell）
#
# 用法（在 repo 根目錄）:
#   .\docker\test\setup-test-env.ps1
#   .\docker\test\setup-test-env.ps1 -Users chad
#   .\docker\test\setup-test-env.ps1 -DryRun

param(
    [string]$MinioContainer = "",
    [string]$NextcloudContainer = "",
    [string]$Users = "",
    [switch]$DryRun,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

function Find-Container([string]$Pattern) {
    $names = docker ps --format "{{.Names}}" 2>$null
    if (-not $names) { return $null }
    return ($names | Where-Object { $_ -match $Pattern } | Select-Object -First 1)
}

if (-not $NextcloudContainer) {
    $all = @(docker ps --format "{{.Names}}" 2>$null)
    $NextcloudContainer = ($all | Where-Object { $_ -match "nextcloud" -and $_ -notmatch "db" } | Select-Object -First 1)
}

if (-not $NextcloudContainer) {
    Write-Error "找不到 Nextcloud 容器，請用 -NextcloudContainer 指定"
}

$MountsFile = if ($Users -eq "chad") {
    Join-Path $ScriptDir "mounts-local-test-chad.json"
} elseif ($Users) {
    Join-Path $ScriptDir "mounts-local-test.json"
} else {
    Join-Path $ScriptDir "mounts-local-test.json"
}

Write-Host "========================================"
Write-Host " Nextcloud + MinIO 測試環境設定"
Write-Host "========================================"
Write-Host "Nextcloud 容器: $NextcloudContainer"
Write-Host "掛載設定檔:     $MountsFile"
Write-Host ""

# 1. MinIO buckets
$bucketArgs = @()
if ($MinioContainer) { $bucketArgs += @("-m", $MinioContainer) }
if ($Users) { $bucketArgs += @("-u", $Users) }

Write-Host "[1/4] 建立 MinIO bucket..."
if ($IsWindows -or $env:OS -match "Windows") {
    bash (Join-Path $ScriptDir "setup-minio-buckets.sh") @bucketArgs
} else {
    & (Join-Path $ScriptDir "setup-minio-buckets.sh") @bucketArgs
}

# 2. Enable files_external
Write-Host ""
Write-Host "[2/4] 啟用 files_external app..."
if ($DryRun) {
    Write-Host "  (dry-run) occ app:enable files_external"
} else {
    docker exec -u www-data $NextcloudContainer php occ app:enable files_external --force 2>$null
}

# 3. Import mounts
Write-Host ""
Write-Host "[3/4] 匯入外部儲存掛載..."
$TempPath = "/tmp/mounts-local-test.json"
docker cp $MountsFile "${NextcloudContainer}:${TempPath}"

Write-Host "  預覽 (dry-run)..."
docker exec -u www-data $NextcloudContainer php occ files_external:import --dry $TempPath
if ($LASTEXITCODE -ne 0) { throw "預覽失敗" }

if (-not $DryRun) {
    if (-not $Yes) {
        $reply = Read-Host "是否繼續匯入？(y/N)"
        if ($reply -notmatch "^[Yy]$") { Write-Host "已取消"; exit 0 }
    }
    docker exec -u www-data $NextcloudContainer php occ files_external:import $TempPath
    if ($LASTEXITCODE -ne 0) { throw "匯入失敗" }
}
docker exec $NextcloudContainer rm -f $TempPath 2>$null

# 4. List
Write-Host ""
Write-Host "[4/4] 列出外部儲存..."
docker exec -u www-data $NextcloudContainer php occ files_external:list

Write-Host ""
Write-Host "完成。以 chad 登入 http://localhost:8085 → 所有檔案 → 個人雲端硬碟"
