<?php
// DB 與密鑰等由 nextcloud-env Secret 的 NC_* 覆寫（見 lib/private/Config.php），此檔可不含敏感資料
$CONFIG = array (
  'htaccess.RewriteBase' => '/',
  'memcache.local' => '\\OC\\Memcache\\APCu',
  'apps_paths' =>
  array (
    0 =>
    array (
      'path' => '/var/www/html/apps',
      'url' => '/apps',
      'writable' => false,
    ),
    1 =>
    array (
      'path' => '/var/www/html/custom_apps',
      'url' => '/custom_apps',
      'writable' => true,
    ),
  ),
  'upgrade.disable-web' => true,
  'trusted_domains' =>
  array (
    0 => 'localhost',
    1 => '*',
  ),
  'datadirectory' => '/var/www/html/data',
  'dbtype' => 'mysql',
  'version' => '32.0.3.2',
  'overwrite.cli.url' => 'http://localhost',
  'dbtableprefix' => 'oc_',
  'mysql.utf8mb4' => true,
  'installed' => true,
);
