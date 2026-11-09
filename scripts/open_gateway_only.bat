@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
echo [Gateway-Only] 仅打开并登录 8090 专属通道网页链接 (Scopus goto=2350 / WoS id=5157)，不启动采集...
python -u -m gateway.channel_8090 --open both %*
pause
