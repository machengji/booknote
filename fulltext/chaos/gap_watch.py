# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# gap_watch.py — 缺口库每小时自动尝试下载器（远程 schtasks）
# 每平台：todo>0 且守护未跑 → 刷新会话 → 试下 3 篇 → HIT>=1 起守护批量；否则记 channel-not-ready（下一轮再试）
# 每轮最多 REFRESH_CAP 次会话刷新（控 /auth/ 票据），顺序逐轮轮换。
import sys, os, time, subprocess
BASE = os.path.dirname(os.path.abspath(__file__))
PY = (_os.environ.get("BOOKNOTE_PY") or _sys.executable)
LOG_DIR = LOGS
os.makedirs(LOG_DIR, exist_ok=True)
_logf = open(os.path.join(LOG_DIR, "gap_watch.log"), "ab", buffering=0)
os.dup2(_logf.fileno(), 1)
os.dup2(_logf.fileno(), 2)
sys.stdout = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
REFRESH_CAP = 3
ROTATE = os.path.join(LOG_DIR, "gap_rotate.txt")

def wlog(m):
    print(time.strftime("%H:%M:%S") + " " + m, flush=True)

CONFIGS = [
    # name, card, base, tpl, dois, out, ck, proxy_mode(None=住宅链 proxy=direct)
    # 2026-09-01 精简：桥接型卡（NAT/AIAA/AIP/OPT/ASME/IEEE/EBSCO）session 永远 fail 还烧 /auth/ 票，撤出轮询；
    # 这些库改走 ZJU Shibboleth 通道（ieee_zju_session.py / _bridge_multi.py 拿凭据）。
    ("ACM",  52,  "https://dl.acm.org",         "{base}/doi/pdf/{doi}",               "acm_dois.txt",    _os.path.join(PDFS, r"ACM"),    "acm_chaos_cookies.json", None),
    ("RSC", 136,  "https://rsc.99885.net",     "{base}/en/content/articlepdf/{rsc_year}/{rsc_code}/{doi_suffix_upper}", "rsc_dois.txt", _os.path.join(PDFS, r"RSC"), "rsc_chaos_cookies.json", "direct"),
]
NO_CHANNEL = [("MUSE", 1028), ("Thieme", 1497), ("BioOne", 2462), ("WorldScientific", 556), ("Brill", 3406)]

def done_set(out, done_log):
    s = set()
    if os.path.exists(done_log):
        with open(done_log, encoding="utf-8") as f:
            s |= {l.strip() for l in f if l.strip()}
    if os.path.isdir(out):
        for fn in os.listdir(out):
            if fn.endswith(".pdf"):
                s.add(fn[:-4].replace("_", "/"))
    return s

def guard_running(card):
    try:
        out = subprocess.run(["wmic", "process", "where",
                              "name='python.exe' and commandline like '%%chaos_tf_guard.py %s%%'" % card,
                              "get", "processid"], capture_output=True, text=True, timeout=25).stdout
        return any(p.strip() for p in out.splitlines() if p.strip().isdigit())
    except Exception:
        return False

def remaining(name, dois, out):
    dlog = os.path.join(LOG_DIR, name.lower() + "_done.txt")
    if not os.path.exists(os.path.join(BASE, dois)):
        return -2, 0
    all_ = [l.strip() for l in open(os.path.join(BASE, dois), encoding="utf-8") if l.strip()]
    done = done_set(out, dlog)
    return len([d for d in all_ if d not in done]), len(all_)

def env_for(name, card, base, tpl, dois, out, ck, mode):
    env = dict(os.environ)
    env.update({"CHAOS_BASE": base, "CHAOS_TPL": tpl, "CHAOS_DOIS": dois,
                "CHAOS_DONE": os.path.join(LOG_DIR, name.lower() + "_done.txt"),
                "CHAOS_MISS": os.path.join(LOG_DIR, name.lower() + "_miss.txt"),
                "CHAOS_COOKIE_OUT": ck, "CHAOS_OUT": out, "CHAOS_PROXY": mode or "proxy"})
    return env

def refresh(card, env):
    r = subprocess.run([PY, os.path.join(BASE, "chaos_tf_session.py"), str(card)],
                       cwd=BASE, capture_output=True, timeout=1200, encoding="utf-8", errors="replace", env=env)
    return r.returncode == 0, (r.stdout or "")[-120:].replace("\n", "|")

def probe_batch(name, dois, out, env):
    r = subprocess.run([PY, os.path.join(BASE, "chaos_tf_batch.py"), "3"],
                       cwd=BASE, capture_output=True, timeout=600, encoding="utf-8", errors="replace", env=env)
    o = r.stdout or ""
    hits = o.count("HIT")
    return hits, (o or "")[-150:].replace("\n", "|")

def start_guard(name, card, env):
    log = open(os.path.join(LOG_DIR, name.lower() + "_guard.log"), "ab", 0)
    subprocess.Popen([PY, os.path.join(BASE, "chaos_tf_guard.py"), str(card)],
                     cwd=BASE, env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

def main():
    wlog("==== gap_watch round ====")
    try:
        rot = int(open(ROTATE).read().strip() or "0")
    except Exception:
        rot = 0
    order = CONFIGS[rot % len(CONFIGS):] + CONFIGS[:rot % len(CONFIGS)]
    open(ROTATE, "w").write(str(rot + 1))

    # 失败冷却：session_fail 的平台冷却 2 轮，把额度让给其它库
    FAIL = os.path.join(LOG_DIR, "gap_fail.txt")
    fail = {}
    try:
        for line in open(FAIL, encoding="utf-8"):
            k, v = line.split()
            fail[k] = int(v)
    except Exception:
        pass

    refreshes = 0
    for (name, card, base, tpl, dois, out, ck, mode) in order:
        if fail.get(name, 0) > 0:
            fail[name] = fail[name] - 1
            wlog(f"[{name}] cooldown {fail[name]}, skip"); continue
        try:
            rem, tot = remaining(name, dois, out)
        except Exception as e:
            wlog(f"[{name}] read-err {e}"); continue
        if rem <= 0:
            wlog(f"[{name}] NONE(done {tot})"); continue
        if guard_running(str(card)):
            wlog(f"[{name}] guard-running, skip (rem {rem})"); continue
        if refreshes >= REFRESH_CAP:
            wlog(f"[{name}] defer (cap {REFRESH_CAP}, rem {rem})"); continue
        env = env_for(name, card, base, tpl, dois, out, ck, mode)
        ok, tail = refresh(card, env)
        refreshes += 1
        if not ok:
            fail[name] = 2
            wlog(f"[{name}] TRY rem={rem} session_fail {tail}"); continue
        fail.pop(name, None)
        hits, btail = probe_batch(name, dois, out, env)
        if hits >= 1:
            start_guard(name, card, env)
            wlog(f"[{name}] TRY rem={rem} session_ok probe_hit={hits} -> GUARD STARTED")
        else:
            fail[name] = 2
            wlog(f"[{name}] TRY rem={rem} session_ok but probe 0-hit {btail[:80]} channel-not-ready")
    with open(FAIL, "w", encoding="utf-8") as f:
        for k, v in fail.items():
            f.write(f"{k} {v}\n")
    for n, c in NO_CHANNEL:
        wlog(f"[{n}] NO-CHANNEL({c}) skip")

if __name__ == "__main__":
    main()
