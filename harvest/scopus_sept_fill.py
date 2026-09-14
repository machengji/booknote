# -*- coding: utf-8 -*-
"""Fill incomplete September Scopus days then keep last_done for daily."""
import datetime
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from camoufox.sync_api import Camoufox
import scopus_pipeline as sc
from wos_pipeline import load_pw_cookies, cookies_still_vip, inject_cookie_list

DAYS = [
    "2026-09-01", "2026-09-02", "2026-09-05", "2026-09-07",
    "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13",
]
KEEP_DONE = "2026-09-13"


def main():
    sc.log("=== scopus sept fill", DAYS, "===")
    saved = load_pw_cookies()
    ck = sc.sci_cookies(saved)
    vip = bool(saved and cookies_still_vip(saved))
    sc.log("[reuse]", "vip", vip, "ck", len(ck or []))
    if not ck:
        sc.problem("NO_SESSION", "sept fill")
        return 2
    prev = {}
    if os.path.exists(sc.STATE):
        try:
            prev = json.load(open(sc.STATE, encoding="utf-8"))
        except Exception:
            prev = {}
    box = {}
    rc = 0
    with Camoufox(headless=False, exclude_addons=["ublock-origin"]) as b:
        pg = b.new_page()
        pg.set_default_timeout(90000)
        try:
            pg.context.add_init_script(sc.ANTI_DEBUG)
        except Exception:
            pass
        pg.on("dialog", lambda d: d.accept())
        inject_cookie_list(pg.context, ck)
        try:
            pg.goto(sc.SITE + "e/member/cp/", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            sc.log("[cp]", str(e)[:80])
        time.sleep(2)
        if not sc.is_logged(pg):
            t0 = sc.page_text(pg) or ""
            if "只能一人在线" in t0 or "请点击这里" in t0:
                sc.takeover_single_session(pg)
        sc.only_one_tab(pg.context, pg)
        sc.attach(pg, box)
        pg.context.on("page", lambda p: sc.attach(p, box))
        app = None
        for eid, name in sc.ENTRIES:
            try:
                app = sc.open_entry(pg, eid, name)
            except Exception as e:
                sc.log("[open-ex]", name, str(e)[:80])
                app = None
            if app is not None and app is not sc.NEED_LOGIN:
                sc.log("[using]", name)
                sc.save_sci_only(app.context)
                break
        if app is None or app is sc.NEED_LOGIN:
            sc.problem("FAIL_LAND", "sept fill")
            return 3
        for day in DAYS:
            box.pop("docs", None)
            box.pop("search_post", None)
            try:
                r = sc.harvest(app, day, box)
            except Exception as e:
                sc.problem("EXCEPTION", day, str(e)[:120])
                r = 3
            sc.log("[fill-day]", day, "rc", r)
            if r:
                rc = r
            gap = sc.SCOPUS_DAY_GAP
            sc.log("[day-gap]", "%.0fs" % gap)
            time.sleep(gap)
    st = {}
    if os.path.exists(sc.STATE):
        try:
            st = json.load(open(sc.STATE, encoding="utf-8"))
        except Exception:
            st = {}
    keep = max(x for x in (prev.get("last_done") or "", st.get("last_done") or "", KEEP_DONE) if x)
    st["last_done"] = keep
    json.dump(st, open(sc.STATE, "w"), ensure_ascii=False, indent=1)
    sc.log("[keep last_done]", keep)
    sc.log("FILL_SEPT", "ok" if rc == 0 else "rc=%d" % rc)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sc.problem("EXCEPTION", traceback.format_exc()[-300:].replace("\n", " | "))
        sys.exit(3)
