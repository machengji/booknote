@echo off
chcp 65001 >nul
set OPENBLAS_NUM_THREADS=1
cd /d E:\booknote
set PY=E:\py311\python.exe
echo [%date% %time%] shu-test start >> D:\machengji_logs\booknote_wos_shu_test.log
%PY% -u harvest\wos_shu_test.py >> D:\machengji_logs\booknote_wos_shu_test.log 2>&1
echo [%date% %time%] end rc=%errorlevel% >> D:\machengji_logs\booknote_wos_shu_test.log
