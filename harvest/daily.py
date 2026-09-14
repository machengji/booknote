# -*- coding: utf-8 -*-
"""每天补采昨天的 WoS / Scopus 新题录。复用 cookie，不自动登录 scihuber。

  python harvest/daily.py              # 有头，补 last_done+1 .. 昨天
  python harvest/daily.py 2026-09-08   # 指定一天
  python harvest/daily.py --headless   # 仅 cookie 仍有效时可用
"""
import datetime
import json
import os
import re
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import wos_pipeline as wos
import scopus_pipeline as scopus


def yesterday():
    return (datetime.date.today() - datetime.timedelta(days=1)).isoformat()


def days_from_state(state_path):
    st = {}
    if os.path.exists(state_path):
        try:
            st = json.load(open(state_path, encoding="utf-8"))
        except Exception:
            pass
    end = datetime.date.fromisoformat(yesterday())
    last = st.get("last_done") or ""
    if last:
        try:
            start = datetime.date.fromisoformat(last) + datetime.timedelta(days=1)
        except Exception:
            start = end
    else:
        start = end
    out = []
    d = start
    while d <= end:
        out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def main():
    args = sys.argv[1:]
    headed = "--headless" not in args
    rest = [a for a in args if not a.startswith("--")]
    if rest and not re.match(r"\d{4}-\d{2}-\d{2}$", rest[0]):
        wos.log("usage: python harvest/daily.py [YYYY-MM-DD] [--headless]")
        return 2
    if rest:
        wos_days = [rest[0]]
        scp_days = [rest[0]]
    else:
        wos_days = days_from_state(wos.STATE)
        scp_days = days_from_state(scopus.STATE)
    wos.log("=== booknote daily", time_now(), "wos", wos_days, "scopus", scp_days,
            "headed", headed, "===")
    if not wos.load_pw_cookies() and not (wos.USER and wos.PWD):
        wos.log("NO_SESSION: 先设 SCIDOWNLOAD_USER/PWD（或仓库根 .env），再跑")
        wos.log("  python harvest/wos_pipeline.py --wait-login")
        return 2
    rc = 0
    if wos_days:
        r = wos.run_days(wos_days, wait_login=False, auto_login=False, headed=headed)
        wos.log("[daily-wos]", "ok" if r == 0 else "rc=%d" % r)
        if r:
            rc = r
    else:
        wos.log("[daily-wos] nothing to do")
    for i, day in enumerate(scp_days):
        r = scopus.run_day(day, headed=headed)
        wos.log("[daily-scopus]", day, "ok" if r == 0 else "rc=%d" % r)
        if r:
            rc = r
            break
        if i + 1 < len(scp_days):
            gap = scopus.SCOPUS_DAY_GAP
            wos.log("[daily-scopus-gap]", "%.0fs" % gap)
            time.sleep(gap)
    if not scp_days:
        wos.log("[daily-scopus] nothing to do")
    return rc


def time_now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(3)
