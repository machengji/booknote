# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# 远端 SAGE 接管守护：不重复spawn已存在的 sage_selfheal，只监控PDF增量，停滞/死亡则kill+重启
import subprocess, time, os, sys, re
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
PY = (_os.environ.get("BOOKNOTE_PY") or _sys.executable)
SG = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "sage_selfheal.py")
OUT = _os.path.join(PDFS, r"scidownload/sage")
WLOG = _os.path.join(LOGS, r"sage_watch.log")
SLOG = _os.path.join(LOGS, r"sage_selfheal.log")
def count():
    try: return len([f for f in os.listdir(OUT) if f.lower().endswith('.pdf')])
    except Exception: return 0
def find_pids():
    try:
        out = subprocess.check_output(
            ['wmic', 'process', 'where', "name='python.exe' and commandline like '%sage_selfheal%'",
             'get', 'ProcessId', '/format:list'],
            text=True, errors='replace', creationflags=0x08000000)
        return re.findall(r'ProcessId=(\d+)', out)
    except Exception as e:
        return []
def w(s):
    print(s, flush=True)
    try:
        open(WLOG, "a", encoding="utf-8").write(time.strftime("%H:%M:%S ") + s + "\n")
    except Exception: pass
w("watchdog(接管) start")
last = count(); stall = 0; spawned = False
while True:
    pids = find_pids()
    if pids:
        spawned = True
        n = count()
        w("selfheal running pids=%s pdfs=%d last=%d" % (pids, n, last))
        if n > last:
            last = n; stall = 0
        else:
            stall += 60
            if stall >= 720:  # 12min无新pdf
                w("STALL 12min no new pdf -> kill pids=%s" % pids)
                for pp in pids:
                    try: subprocess.run(['taskkill', '/PID', pp, '/F'], capture_output=True)
                    except Exception: pass
                stall = 0; last = count(); time.sleep(30)
    else:
        w("no selfheal proc; spawning (last pdfs=%d)" % last)
        try:
            subprocess.Popen([PY, SG], stdout=open(SLOG, "a", encoding="utf-8"), stderr=subprocess.STDOUT, cwd=FT)
            spawned = True
            time.sleep(45)
        except Exception as e:
            w("spawn err " + str(e)[:60])
        last = count(); stall = 0
    time.sleep(60)
