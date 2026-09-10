# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# ovid_chaos_guard.py — Ovid 线守护：浏览器引导 Ovid 会话 -> requests 批量 -> 死了重建
import sys, os, time, json, re, subprocess, socket
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
PY = _os.environ.get("BOOKNOTE_PY") or sys.executable
BASE = os.path.dirname(os.path.abspath(__file__))
PROXY_PORT = int(os.environ.get("CHAOS_PROXY_PORT") or "10889")
SESS_FILE = os.path.join(BASE, "_ovid_session.json")

def tunnel_ok():
    try:
        s = socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=3)
        s.close()
        return True
    except Exception:
        return False

def ensure_tunnel():
    if tunnel_ok():
        return
    helper = "chain_proxy.py" if PROXY_PORT == 10889 else "resi_tunnel.py"
    print("[guard] proxy down, starting", helper, flush=True)
    subprocess.Popen([PY, os.path.join(BASE, helper)], stdout=open(os.path.join(BASE, "_chain.log"), "ab"), stderr=subprocess.STDOUT, cwd=BASE)
    for _ in range(20):
        time.sleep(3)
        if tunnel_ok():
            print("[guard] proxy back", flush=True)
            return
    raise RuntimeError("proxy cannot start")

BOOT = r'''
import sys, time, json, re
sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
from camoufox.sync_api import Camoufox
def mclick(pg, el):
    el.scroll_into_view_if_needed(); r = el.bounding_box()
    pg.mouse.click(r["x"]+r["width"]/2, r["y"]+r["height"]/2)
state = {"body": None}
def resp_watch(r):
    if r.request.method == "POST" and r.url.rstrip("/").endswith("/auth"):
        try: state["body"] = r.text()
        except Exception: pass
with Camoufox(headless=True, proxy={"server": "http://127.0.0.1:10889"}, exclude_addons=["ublock-origin"]) as b:
    pg = b.new_page()
    pg.on("response", resp_watch)
    pg.goto("https://www.chaoslib.com/", timeout=60000, wait_until="domcontentloaded")
    time.sleep(6)
    pg.fill("#username", _os.environ.get("CHAOS_CARD") or ""); pg.fill("#password", _os.environ.get("CHAOS_PWD") or "")
    try: pg.check("#login-checkbox", force=True, timeout=3000)
    except Exception: pass
    pg.click("#login-button")
    for i in range(15):
        time.sleep(2)
        c = pg.content()
        if "退出" in c or "个人" in c: break
        try:
            for btn in pg.query_selector_all("a,button"):
                try:
                    if (btn.inner_text() or "").strip() == "强制剔除": btn.click(); break
                except Exception: continue
        except Exception: continue
    time.sleep(3)
    TABS = ["医学数据库", "英文数据库"]
    card = None
    for attempt in range(10):
        card = pg.query_selector("a.block-group__item[data-id='93']")
        vis = False
        if card:
            try: vis = card.evaluate("e=>!!e.offsetParent && e.getBoundingClientRect().width>0")
            except Exception: pass
        if vis: break
        tab = TABS[attempt % 2]
        for el in pg.query_selector_all("a,li,span,div"):
            try:
                if (el.inner_text() or "").strip() == tab and el.is_visible():
                    mclick(pg, el); break
            except Exception: continue
        time.sleep(4)
    if not card: print("[FAIL] no card"); sys.exit(1)
    mclick(pg, card)
    for i in range(100):
        if state["body"]: break
        time.sleep(3)
        if i in (25, 50, 75):
            try: mclick(pg, card)
            except Exception: pass
    m = re.search(r'"url"\s*:\s*"([^"]+)"', state["body"] or "")
    if not m: print("[FAIL] no ticket"); sys.exit(1)
    try:
        pg.goto(m.group(1), timeout=60000, wait_until="domcontentloaded")
    except Exception as e:
        print("[goto]", str(e)[:80]); time.sleep(5)
    for i in range(20):
        time.sleep(3)
        if "ovidweb.cgi" in (pg.url or ""): break
    print("[land]", pg.url[:100])
    st = pg.evaluate("""() => { const c = document.querySelector('input#ovft'); if(!c) return 'noinput'; const l = document.querySelector('label[for=ovft]') || c.closest('li') || c; if(l) l.click(); return c.checked; }""")
    print("[db]", st); time.sleep(1)
    pg.evaluate("""() => { const b = document.querySelector('#database-submit'); if(b){ b.disabled = false; b.click(); } }""")
    for i in range(15):
        time.sleep(3)
        if "Search Form" not in (pg.title() or ""): break
    time.sleep(6)
    h = pg.content()
    m2 = re.search(r'S=([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})%7[cC]main', h)
    if not m2:
        m2 = re.search(r'S=([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', h)
    if not m2: print("[FAIL] no sid"); sys.exit(1)
    base = (pg.url or "").split("/ovid-new-b")[0] + "/ovid-new-b"
    sess = {"cookies": pg.context.cookies(), "sid": m2.group(1) + "|main", "base": base,
            "ua": pg.evaluate("navigator.userAgent")}
    json.dump(sess, open(_os.path.join(FT, r"_ovid_session.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print("[OK] session saved:", base[:60], sess["sid"][:20])
'''

def bootstrap():
    p = os.path.join(BASE, "_ovid_boot.py")
    open(p, "w", encoding="utf-8").write(BOOT)
    r = subprocess.run([PY, p], cwd=BASE, capture_output=True, timeout=900, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + " || " + (r.stderr or "")
    print("[boot]", r.returncode, out[-260:].replace("\n", " | "), flush=True)
    return r.returncode == 0 and os.path.exists(SESS_FILE)

refresh_budget = 30
while True:
    ensure_tunnel()
    need_boot = True
    if os.path.exists(SESS_FILE) and time.time() - os.path.getmtime(SESS_FILE) < 1200:
        need_boot = False  # 20 分钟内的会话直接用
    if need_boot:
        if refresh_budget <= 0:
            print("[guard] no refresh budget", flush=True)
            break
        refresh_budget -= 1
        if not bootstrap():
            time.sleep(120)
            continue
    print("[guard] launching batch", flush=True)
    r = subprocess.run([PY, os.path.join(BASE, "ovid_chaos_browser.py")], cwd=BASE,
                       capture_output=True, timeout=360000, encoding="utf-8", errors="replace")
    out = r.stdout or ""
    print("[guard] batch exit:", r.returncode, out[-200:].replace("\n", " | "), flush=True)
    try:
        os.remove(SESS_FILE)
    except Exception:
        pass
    if "SESSION DEAD" not in out and "todo: 0" in out:
        print("[guard] ALL DONE", flush=True)
        break
    time.sleep(60)
print("[guard] exit", flush=True)
