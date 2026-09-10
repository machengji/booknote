# Register a Windows daily task at 07:00. Headed Camoufox needs an interactive logon.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $root) { $root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$bat = Join-Path $root "scripts\run_daily.bat"
if (-not (Test-Path $bat)) { throw "missing $bat" }
$tr = "cmd /c `"$bat`""
schtasks /Create /TN "booknote-daily" /SC DAILY /ST 07:00 /RL LIMITED /IT /F /TR $tr
Write-Host "ok: booknote-daily -> $bat (07:00, interactive)"
Write-Host "cookie 失效时窗口会停在登录页；先跑: python harvest/wos_pipeline.py --wait-login"
