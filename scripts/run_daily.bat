@echo off
chcp 65001 >nul
set OPENBLAS_NUM_THREADS=1
set OMP_NUM_THREADS=1
cd /d "%~dp0.."
set PY=python
if exist "E:\py311\python.exe" set PY=E:\py311\python.exe
"%PY%" -u harvest\daily.py %*
echo 结束码 %errorlevel%
