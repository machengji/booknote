# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# chaos_tf_session.py — 混沌书苑 T&F 会话刷新器
# 流程: (可选复用chaoslib登录) -> 英文库 -> 点 T&F 卡 -> POST /auth/ 票据 -> /api/redirect/ 跳转 -> 导出 tandfonline cookies
# 用法: python chaos_tf_session.py [data_id]   (默认 41=T&F; IOP=161 SD=152 OVID=93 Karger=98)
import sys, time, json, re, os
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
from camoufox.sync_api import Camoufox

CARD = _os.environ.get("CHAOS_CARD") or ""
PWD = _os.environ.get("CHAOS_PWD") or _os.environ.get("ZX_PWD") or ""
HOME = "https://www.chaoslib.com/"
PROXY = os.environ.get("CHAOS_PROXY") or "http://127.0.0.1:10889"
DIRECT = PROXY.strip().lower() in ("direct", "", "none")
DATA_ID = sys.argv[1] if len(sys.argv) > 1 else "41"
OUT = os.environ.get("CHAOS_COOKIE_OUT", "tf_chaos_cookies.json")

def content(pg):
    try:
        return pg.content() or ""
    except Exception:
        return ""

def mclick(pg, el):
    el.scroll_into_view_if_needed()
    r = el.bounding_box()
    if not r or r["width"] == 0:
        return False
    pg.mouse.click(r["x"] + r["width"] / 2, r["y"] + r["height"] / 2)
    return True

state = {"resp_body": None, "post_seen": False}

def req_watch(r):
    if r.method == "POST" and r.url.rstrip("/").endswith("/auth"):
        state["post_seen"] = True

def resp_watch(r):
    if r.request.method == "POST" and r.url.rstrip("/").endswith("/auth"):
        try:
            state["resp_body"] = r.text()
            print("[auth-resp]", r.status, str(state["resp_body"])[:180], flush=True)
        except Exception:
            pass

def try_cf_click(pg):
    try:
        for fr in pg.frames:
            if "challenges.cloudflare" in (fr.url or ""):
                box = fr.query_selector("input[type=checkbox]")
                if box:
                    bb = box.bounding_box()
                    if bb:
                        pg.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
                        return True
    except Exception:
        pass
    return False

_kw = {} if DIRECT else {"proxy": {"server": PROXY}}
with Camoufox(headless=True, exclude_addons=["ublock-origin"], **_kw) as b:
    pg = b.new_page()
    pg.on("request", req_watch)
    pg.on("response", resp_watch)

    pg.goto(HOME, timeout=60000, wait_until="domcontentloaded")
    time.sleep(6)
    pg.fill("#username", CARD)
    pg.fill("#password", PWD)
    try:
        pg.check("#login-checkbox", force=True, timeout=3000)
    except Exception:
        pass
    pg.click("#login-button")
    logged = False
    for i in range(15):
        time.sleep(2)
        c = content(pg)
        if "退出" in c or "个人" in c:
            logged = True
            break
        try:
            for btn in pg.query_selector_all("a,button"):
                try:
                    if (btn.inner_text() or "").strip() == "强制剔除":
                        btn.click()
                        break
                except Exception:
                    continue
        except Exception:
            continue
    if not logged:
        print("[FAIL] chaoslib login", flush=True)
        pg.screenshot(path="_chaos_sess_loginfail.png")
        sys.exit(2)
    print("[login] OK", flush=True)
    time.sleep(3)

    # 确保目标卡片可见（必要时反复点 英文数据库 tab）
    def ensure_card_visible(did):
        for attempt in range(5):
            card = pg.query_selector(f"a.block-group__item[data-id='{did}']")
            if card:
                try:
                    if card.evaluate("e=>!!e.offsetParent && e.getBoundingClientRect().width>0"):
                        return card
                except Exception:
                    pass
            for el in pg.query_selector_all("a,li,span,div"):
                try:
                    if (el.inner_text() or "").strip() == "英文数据库" and el.is_visible():
                        mclick(pg, el)
                        break
                except Exception:
                    continue
            time.sleep(4)
        return pg.query_selector(f"a.block-group__item[data-id='{did}']")

    for cycle in range(3):
        card = ensure_card_visible(DATA_ID)
        if not card:
            print("[FAIL] card data-id not found/invisible:", DATA_ID, flush=True)
            sys.exit(3)
        state["resp_body"] = None
        state["post_seen"] = False
        mclick(pg, card)
        print(f"[cycle {cycle}] card clicked", flush=True)
        for i in range(60):
            if state["resp_body"] is not None:
                break
            time.sleep(3)
            if i in (25, 50) and not state["post_seen"]:
                try:
                    mclick(pg, card)
                except Exception:
                    pass
        m = re.search(r'"url"\s*:\s*"([^"]+)"', state["resp_body"] or "")
        if not m:
            print(f"[cycle {cycle}] no ticket, retry", flush=True)
            time.sleep(5)
            continue
        target = HOME.rstrip("/") + m.group(1) if m.group(1).startswith("/") else m.group(1)
        try:
            pg.goto(target, timeout=90000, wait_until="domcontentloaded")
        except Exception as e:
            print("[goto]", str(e)[:120], flush=True)
        # 等跳转/CF 完成
        for i in range(30):
            time.sleep(3)
            try:
                t = (pg.title() or "").lower()
            except Exception:
                t = "moment"
            if "moment" in t:
                try_cf_click(pg)
                continue
            if i >= 6:
                break
        time.sleep(5)
        # 验证目标域 cookies（need 为空集=该域任意 cookie 存在即可）
        VALID = {"41": ("https://www.tandfonline.com", {"JSESSIONID", "cf_clearance"}),
                 "161": ("https://iop.66557.net", {"JSESSIONID", "IOP_session_live"}),
                 "45": ("https://acstest.99885.net", set()),
                 "52": ("https://dl.acm.org", set()),
                 "47": ("https://www.nature.com", set()),
                 "53": ("https://arc.aiaa.org", set()),
                 "130": ("https://epubs.siam.org", set()),
                 "165": ("https://www.pnas.org", set()),
                 "136": ("https://rsc.99885.net", set()),
                 "57": ("https://pubs.aip.org", set()),
                 "55": ("https://opg.optica.org", set()),
                 "56": ("https://asmedigitalcollection.asme.org", set()),
                 "32": ("https://ieeexplore.ieee.org", set()),
                 "100": ("https://research.ebsco.com", set())}
        dom, need = VALID.get(DATA_ID, ("https://www.tandfonline.com", {"JSESSIONID", "cf_clearance"}))
        cks = pg.context.cookies(dom)
        names = [c["name"] for c in cks]
        if (not need and names) or need.issubset(set(names)):
            json.dump(cks, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
            # 同存 UA 供批量线匹配 cf_clearance
            ua = pg.evaluate("navigator.userAgent")
            open(OUT + ".ua", "w", encoding="utf-8").write(ua)
            print("[OK] cookies saved:", OUT, len(cks), "ua:", ua[:60], flush=True)
            sys.exit(0)
        print(f"[cycle {cycle}] cookies incomplete: {names}", flush=True)
    print("[FAIL] all cycles", flush=True)
    sys.exit(4)
