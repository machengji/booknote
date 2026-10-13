@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo 正在注册 Windows 计划任务: Antigravity_Patrol_Hourly ...
schtasks /create /tn "Antigravity_Patrol_Hourly" /sc hourly /mo 1 /tr "python \"%~dp0harvest\antigravity_patrol.py\"" /f

if %ERRORLEVEL% equ 0 (
    echo [成功] 计划任务已注册为每小时自动后台运行，无需人工干预！
) else (
    echo [提示] 如需注册系统级任务，请右键选择「以管理员身份运行」。
)
pause
