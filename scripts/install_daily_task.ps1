# ==============================================================================
# 注册/修复每日 08:00 [Scopus + WoS] 自动采集 Windows 计划任务
# 用法: powershell -ExecutionPolicy Bypass -File scripts\install_daily_task.ps1
# ==============================================================================

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$BatPath = Join-Path $ProjectRoot "scripts\run_daily_harvest.bat"
$TaskName = "Daily_8AM_Scopus_WoS_Goal_Harvester"

Write-Host ">>> 正在注册每日 08:00 自动采集计划任务: $TaskName ..." -ForegroundColor Cyan
Write-Host "    执行入口: $BatPath"

schtasks /Create /TN "$TaskName" /TR "`"$BatPath`"" /SC DAILY /ST 08:00 /RL HIGHEST /F
if ($LASTEXITCODE -ne 0) {
    throw "计划任务注册失败，退出码: $LASTEXITCODE"
}

Write-Host "`n>>> 计划任务注册成功！当前任务状态核验：" -ForegroundColor Green
schtasks /Query /TN "$TaskName" /FO LIST /V
