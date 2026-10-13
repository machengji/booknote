@echo off
chcp 65001 >nul
set OPENBLAS_NUM_THREADS=1
set OMP_NUM_THREADS=1
cd /d E:\booknote
set PY=E:\py311\python.exe
echo [%date% %time%] sept-catchup start >> D:\machengji_logs\booknote_wos_sept.log
%PY% -u harvest\wos_sept_catchup.py >> D:\machengji_logs\booknote_wos_sept.log 2>&1
echo [%date% %time%] end rc=%errorlevel% >> D:\machengji_logs\booknote_wos_sept.log
