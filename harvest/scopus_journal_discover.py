# -*- coding: utf-8 -*-
"""期刊发现扫描：用搜索 API 采样，把窗口内的期刊名尽量收全。

为什么不能用 facet
    实测网关的 facet 每次只给 7 个桶，count/offset/from/sort 全被忽略，
    /gateway/sources 是 404，Sources 浏览页要 scopus.com 账号（我们只有 CARSI 票）。
    所以枚举只能靠搜索 API 自己采样。

怎么采
    切片 = ORIG-LOAD-DATE 窗口 × PUBYEAR × DOCTYPE（必要时再乘 LANGUAGE）。
    每片取首页 1000 条，把 source 字段全收下来去重。
    切片的作用是把「按 datesort 排前 1000 条」分散到不同年份/类型上，
    否则永远只看到同一批高产刊。

产出
    scopus_journal_seed.json: {"journals": [...], "slices": n, "records": n}
    再合并进 scopus_journal_state.json 供按刊采集使用。
"""
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

os.environ.setdefault("BOOKNOTE_DATA", r"F:\pubmed")
os.environ.setdefault("BOOKNOTE_LOG", r"F:\pubmed\logs")
os.environ.setdefault("SCOPUS_OUT", r"F:\pubmed\scopus")

import scopus_le1000 as L  # noqa: E402
import scopus_pipeline as sc  # noqa: E402

SEED_PATH = os.path.join(sc.OUT_DIR, "scopus_journal_seed.json")
WINDOW_START = os.environ.get("BOOKNOTE_SCOPUS_JOURNAL_START") or "2026-09-01"
BUDGET_MIN = float(os.environ.get("BOOKNOTE_SCOPUS_DISCOVER_BUDGET") or 30)
USE_LANG = os.environ.get("BOOKNOTE_SCOPUS_DISCOVER_LANG", "1") == "1"
YEARS = [int(x) for x in (os.environ.get("BOOKNOTE_SCOPUS_DISCOVER_YEARS")
                          or "2027,2026,2025,2024,2023,2022,2021,2020").split(",")]
LANGS = ["english", "chinese", "japanese", "german", "french", "spanish",
         "russian", "portuguese", "korean", "italian"]

_T0 = [0.0]
_STATS = {"slices": 0, "records": 0, "journals": 0, "empty": 0, "err": 0}


def log(*a):
    sc.log("[discover]", *a)


def budget_left():
    return BUDGET_MIN - (time.time() - _T0[0]) / 60.0


def window_clause():
    d = datetime.date.fromisoformat(WINDOW_START) - datetime.timedelta(days=1)
    return "ORIG-LOAD-DATE AFT %s" % d.strftime("%Y%m%d")


def load_seed():
    try:
        with open(SEED_PATH, encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        d = {}
    return set(d.get("journals") or []), int(d.get("slices") or 0), int(d.get("records") or 0)


def save_seed(js, slices, records):
    tmp = SEED_PATH + ".tmp"
    json.dump({"journals": sorted(js), "slices": slices, "records": records,
               "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
              open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, SEED_PATH)


def harvest_page(pg, q, js):
    """取一页，把 source 收进集合。返回 (新增期刊数, 本页记录数, total)。"""
    recs, tot, err = L.query(pg, q)
    if err:
        _STATS["err"] += 1
        return 0, 0, 0
    tot = int(tot or 0)
    new = 0
    for r in recs or []:
        s = (r.get("source") or "").strip()
        if s and s.lower() not in ("undefined", "null", "none") and s not in js:
            js.add(s)
            new += 1
    _STATS["records"] += len(recs or [])
    return new, len(recs or []), tot


def main():
    _T0[0] = time.time()
    js, slices0, records0 = load_seed()
    log("[seed-load]", "已有期刊", len(js), "slices", slices0, "records", records0)

    cam, br, app = L.land()
    if app is None:
        return 3
    rc = 0
    try:
        base = window_clause()
        plan = []
        for y in YEARS:
            for dt in list(sc.DOCTYPES) + ["__rest__"]:
                plan.append(("%d/%s" % (y, dt), "%s AND PUBYEAR = %d%s" % (
                    base, y,
                    (" AND DOCTYPE(%s)" % dt) if dt != "__rest__"
                    else " AND NOT DOCTYPE(%s)" % " OR ".join(sc.DOCTYPES))))
        if USE_LANG:
            for lg in LANGS:
                plan.append(("lang/" + lg, "%s AND LANGUAGE(%s)" % (base, lg)))
        log("[plan]", len(plan), "slices, budget", BUDGET_MIN, "min")

        for i, (tag, q) in enumerate(plan):
            if budget_left() <= 1.5:
                log("[budget-stop]", "剩余 %.1f 分钟，已完成 %d 片" % (budget_left(), i))
                rc = 5
                break
            try:
                new, page, tot = harvest_page(app, q, js)
            except Exception as e:
                log("[err]", tag, str(e)[:80])
                _STATS["err"] += 1
                continue
            _STATS["slices"] += 1
            if not tot:
                _STATS["empty"] += 1
            log("[slice]", tag, "tot", tot, "page", page, "new-journals", new,
                "| 累计", len(js), "剩 %.1f 分" % budget_left())
            if _STATS["slices"] % 5 == 0:
                save_seed(js, slices0 + _STATS["slices"], records0 + _STATS["records"])
    finally:
        try:
            br.close()
        except Exception:
            pass
        save_seed(js, slices0 + _STATS["slices"], records0 + _STATS["records"])
    _STATS["journals"] = len(js)
    log("[SUMMARY]", json.dumps(_STATS, ensure_ascii=False), "rc", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
