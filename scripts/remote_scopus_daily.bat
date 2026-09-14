@echo off
chcp 65001 >nul
set OPENBLAS_NUM_THREADS=1
set OMP_NUM_THREADS=1
set BOOKNOTE_DATA=F:\pubmed
set BOOKNOTE_LOG=F:\pubmed\logs
set SCOPUS_OUT=F:\pubmed\scopus
set WOS_OUT=F:\pubmed\wos
set BOOKNOTE_SCOPUS_GAP=3.5
set BOOKNOTE_SCOPUS_GAP_JITTER=1.8
set BOOKNOTE_SCOPUS_BURST_EVERY=18
set BOOKNOTE_SCOPUS_BURST_PAUSE=16
set BOOKNOTE_SCOPUS_DAY_GAP=25
cd /d E:\booknote
set PY=E:\py311\python.exe
if not exist F:\pubmed\logs mkdir F:\pubmed\logs
echo [%date% %time%] start %* >> F:\pubmed\logs\booknote_scopus_daily.log
%PY% -u harvest\scopus_pipeline.py %* >> F:\pubmed\logs\booknote_scopus_daily.log 2>&1
set RC=%errorlevel%
echo [%date% %time%] end rc=%RC% >> F:\pubmed\logs\booknote_scopus_daily.log
if not %RC%==0 echo [%date% %time%] BAT_FAIL rc=%RC% >> F:\pubmed\logs\scopus_problems.log
