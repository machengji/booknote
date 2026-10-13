# -*- coding: utf-8 -*-
"""Catch up WoS 题录 from 1 Sep through 7 Sep, then keep last_done at the later of that and current."""
import datetime
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wos_pipeline as wos


def main():
    start = datetime.date(2026, 9, 1)
    end = datetime.date(2026, 9, 7)
    days = []
    d = start
    while d <= end:
        days.append(d.isoformat())
        d += datetime.timedelta(days=1)
    prev = {}
    if os.path.exists(wos.STATE):
        try:
            prev = json.load(open(wos.STATE, encoding="utf-8"))
        except Exception:
            prev = {}
    wos.log("=== wos sept catchup", days, "prev last_done", prev.get("last_done"), "===")
    rc = wos.run_days(days, wait_login=False, auto_login=False, headed=True)
    st = {}
    if os.path.exists(wos.STATE):
        try:
            st = json.load(open(wos.STATE, encoding="utf-8"))
        except Exception:
            st = {}
    old = prev.get("last_done") or ""
    new = st.get("last_done") or ""
    keep = max([x for x in (old, new) if x] or [""])
    if keep:
        st["last_done"] = keep
        json.dump(st, open(wos.STATE, "w"), ensure_ascii=False, indent=1)
        wos.log("[keep last_done]", keep)
    wos.log("CATCHUP", "ok" if rc == 0 else "rc=%d" % rc)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(3)
