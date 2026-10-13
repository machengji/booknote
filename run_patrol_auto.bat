@echo off
chcp 65001 >nul
title WoS/Scopus 自动巡检与自主修复守护程序
cd /d "%~dp0"

echo ========================================================
echo   WoS / Scopus 反重力定时巡检与自主修复守护程序
echo   模式: 全自动守护循环 (每小时自动执行，无需人工干预)
echo ========================================================
echo.

python harvest\antigravity_patrol.py --daemon --interval 60

pause
