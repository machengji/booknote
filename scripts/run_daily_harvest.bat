@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
echo [Daily-Runner] 启动 8090 专属通道 (Scopus + WoS) 每日自动采集总控...
python -u -m scheduler.daily_runner %*
