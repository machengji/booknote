@echo off
chcp 65001 >nul
set OPENBLAS_NUM_THREADS=1
set OMP_NUM_THREADS=1
set BOOKNOTE_DATA=F:\pubmed
set BOOKNOTE_LOG=F:\pubmed\logs
set WOS_OUT=F:\pubmed\wos
cd /d E:\booknote
set PY=E:\py311\python.exe
if not exist F:\pubmed\logs mkdir F:\pubmed\logs
echo [%date% %time%] start %* >> F:\pubmed\logs\booknote_wos_daily.log
%PY% -u harvest\wos_pipeline.py %* >> F:\pubmed\logs\booknote_wos_daily.log 2>&1
set RC=%errorlevel%
echo [%date% %time%] end rc=%RC% >> F:\pubmed\logs\booknote_wos_daily.log
if not %RC%==0 echo [%date% %time%] BAT_FAIL rc=%RC% >> F:\pubmed\logs\wos_problems.log
