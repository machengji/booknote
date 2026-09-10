# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# chaos_tf_guard.py — 混沌书苑 T&F 收割守护：会话刷新 + 批量循环直到清空
# 本地版: 需 SSH 隧道 10990; 远程版(默认): 直接用 127.0.0.1:10889
import sys, os, time, subprocess, socket
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
PY = _os.environ.get("BOOKNOTE_PY") or sys.executable
if os.name != "nt":
    PY = sys.executable
BASE = os.path.dirname(os.path.abspath(__file__))
REMOTE_MODE = os.environ.get("CHAOS_REMOTE") == "1"
PROXY_PORT = int(os.environ.get("CHAOS_PROXY_PORT") or "10889")

def tunnel_ok():
    try:
        s = socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=3)
        s.close()
        return True
    except Exception:
        return False

def ensure_tunnel():
    if os.environ.get("CHAOS_PROXY", "").strip().lower() in ("direct", "", "none"):
        return  # 国内直连模式，无需隧道
    if tunnel_ok():
        return
    helper = "chain_proxy.py" if REMOTE_MODE else "resi_tunnel.py"
    print("[guard] proxy %d down, starting %s" % (PROXY_PORT, helper), flush=True)
    subprocess.Popen([PY, os.path.join(BASE, helper)],
                     stdout=open(os.path.join(BASE, "_chain.log"), "ab"),
                     stderr=subprocess.STDOUT, cwd=BASE)
    for _ in range(20):
        time.sleep(3)
        if tunnel_ok():
            print("[guard] proxy back", flush=True)
            return
    raise RuntimeError("proxy cannot start")

def todo_probe():
    # 用批量脚本自身探测剩余量（limit=0 只打印 todo 行即退出）
    try:
        r = subprocess.run([PY, os.path.join(BASE, "chaos_tf_batch.py"), "0"],
                           cwd=BASE, capture_output=True, timeout=300,
                           encoding="utf-8", errors="replace")
        for line in (r.stdout or "").splitlines():
            if line.startswith("todo:"):
                return int(line.split()[1])
    except Exception as e:
        print("[guard] probe err", str(e)[:120], flush=True)
    return -1

DATA_ID = sys.argv[1] if len(sys.argv) > 1 else "41"
# 每卡默认配置（bat 环境变量优先；无 bat 时直接 schtasks 指 python 也能跑）
CARD_CFG = {
    "45": dict(base="https://acstest.99885.net", tpl="{base}/doi/pdf/{doi}?download=true",
               dois="acs_dois.txt", done=_os.path.join(LOGS, r"acs_chaos_done.txt"),
               miss=_os.path.join(LOGS, r"acs_chaos_miss.txt"),
               ck=_os.path.join(LOGS, r"acs_chaos_cookies.json"), out=_os.path.join(PDFS, r"ACS")),
    "52": dict(base="https://dl.acm.org", tpl="{base}/doi/pdf/{doi}",
               dois="acm_dois.txt", done=_os.path.join(LOGS, r"acm_chaos_done.txt"),
               miss=_os.path.join(LOGS, r"acm_chaos_miss.txt"),
               ck=_os.path.join(LOGS, r"acm_chaos_cookies.json"), out=_os.path.join(PDFS, r"ACM")),
    "47": dict(base="https://www.nature.com", tpl="{base}/articles/{doi_suffix}.pdf",
               dois="nature_dois.txt", done=_os.path.join(LOGS, r"nature_chaos_done.txt"),
               miss=_os.path.join(LOGS, r"nature_chaos_miss.txt"),
               ck=_os.path.join(LOGS, r"nature_chaos_cookies.json"), out=_os.path.join(PDFS, r"Nature")),
    "53": dict(base="https://arc.aiaa.org", tpl="{base}/doi/pdf/{doi}?download=true",
               dois="aiaa_dois.txt", done=_os.path.join(LOGS, r"aiaa_chaos_done.txt"),
               miss=_os.path.join(LOGS, r"aiaa_chaos_miss.txt"),
               ck=_os.path.join(LOGS, r"aiaa_chaos_cookies.json"), out=_os.path.join(PDFS, r"AIAA")),
}
_cfg = CARD_CFG.get(DATA_ID)
if _cfg:
    _m = {"base": "CHAOS_BASE", "tpl": "CHAOS_TPL", "dois": "CHAOS_DOIS", "done": "CHAOS_DONE",
          "miss": "CHAOS_MISS", "ck": "CHAOS_COOKIE_OUT", "out": "CHAOS_OUT"}
    for _k, _env in _m.items():
        os.environ.setdefault(_env, _cfg[_k])
CK = os.environ.get("CHAOS_COOKIE_OUT") or {"41": "tf_chaos_cookies.json"}.get(DATA_ID, f"chaos_{DATA_ID}_cookies.json")

refresh_budget = 30
while True:
    ensure_tunnel()
    left = todo_probe()
    print(f"[guard] remaining: {left}", flush=True)
    if left == 0:
        print("[guard] ALL DONE", flush=True)
        break
    ck = os.path.join(BASE, CK)
    need_refresh = True
    if os.path.exists(ck) and time.time() - os.path.getmtime(ck) < 3600:
        need_refresh = False  # 1 小时内的会话直接用
    if need_refresh and refresh_budget > 0:
        print("[guard] refreshing session...", flush=True)
        r = subprocess.run([PY, os.path.join(BASE, "chaos_tf_session.py"), DATA_ID],
                           cwd=BASE, capture_output=True, timeout=1200,
                           encoding="utf-8", errors="replace")
        tail = (r.stdout or "")[-200:].replace("\n", " | ")
        print("[guard] refresh:", r.returncode, tail, flush=True)
        if r.returncode != 0:
            refresh_budget -= 1
            time.sleep(120)
            continue
    print("[guard] launching batch", flush=True)
    r = subprocess.run([PY, os.path.join(BASE, "chaos_tf_batch.py")],
                       cwd=BASE, capture_output=True, timeout=360000,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "")
    tail = out[-300:].replace("\n", " | ")
    print("[guard] batch exit:", r.returncode, tail, flush=True)
    if "SESSION DEAD" in out:
        try:
            os.remove(ck)
        except Exception:
            pass
        continue
    left = todo_probe()
    if left == 0:
        print("[guard] ALL DONE", flush=True)
        break
    refresh_budget -= 1
    time.sleep(180)
print("[guard] exit", flush=True)
