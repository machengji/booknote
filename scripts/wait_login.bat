@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set PY=python
if exist "E:\py311\python.exe" set PY=E:\py311\python.exe
echo 弹出浏览器：账号密码从环境变量/.env 填写，你只填验证码。
"%PY%" -u harvest\wos_pipeline.py --wait-login %*
echo 结束码 %errorlevel%
pause
