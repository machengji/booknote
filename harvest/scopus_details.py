# -*- coding: utf-8 -*-
"""Scopus 详情：列表某天采全之后，用同一落地会话 GET doc-details。

接口（09-17 打通）
    GET {gateway}/gateway/doc-details/documents/{eid}
    同一 scihuber 会话、不打开 record 页。

规则
    1. 天从**正式列表月档自己算**（`verify_from_list`），不再读 scopus_day_verify.json
       —— 那是按天线的产物，按天线 2026-09-28 已整段退役、该文件冻结不再更新；
    2. 小天先齐（按当天 unique eid 升序），所以薄的几天一两个 cap 就能跑满；
    3. 按 eid 断点续跑，已有详情的跳过；
    4. 401/403 立刻停手；429/503 交给 pace 退避，连续失败则收手。
    5. 生产入口不是本文件，而是 `scopus_journal.py` 复用同一个浏览器会话调用
       （两次 land() 相隔 1~2 分钟就会把站点顶成「同一帐号同一时刻只能一人在线」）。
"""
import datetime
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ.setdefault("BOOKNOTE_DATA", r"F:\pubmed")
os.environ.setdefault("BOOKNOTE_LOG", r"F:\pubmed\logs")
os.environ.setdefault("SCOPUS_OUT", r"F:\pubmed\scopus")

import scopus_pipeline as sc  # noqa: E402

MONTH_FILE_RE = re.compile(r"^scopus_(\d{4}-\d{2})\.jsonl$")
DETAIL_MAX = int(os.environ.get("BOOKNOTE_SCOPUS_DETAIL_MAX") or 400)
KEEP_RAW = os.environ.get("BOOKNOTE_SCOPUS_DETAIL_RAW", "").strip() in ("1", "true", "yes")


def log(*a):
    sc.log("[detail]", *a)


def out_dir():
    return sc.OUT_DIR


def list_path(day):
    return os.path.join(out_dir(), "scopus_%s.jsonl" % day[:7])


def detail_path(day):
    return os.path.join(out_dir(), "scopus_detail_%s.jsonl" % day[:7])


def _txt(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        parts = [_txt(x) for x in v]
        return " ".join(x for x in parts if x)
    if isinstance(v, dict):
        return v.get("text") or v.get("value") or v.get("name") or ""
    return str(v)


def pick_detail(d, day):
    """从网关 JSON 抽出交付字段。默认不落整包 raw（一天两万条会到几百 MB）。"""
    titles = d.get("titles") or []
    title = titles[0] if titles else d.get("title")
    src = d.get("source") if isinstance(d.get("source"), dict) else {}
    rec = {
        "eid": (d.get("eid") or "").strip(),
        "load_date": day,
        "doi": _txt(d.get("doi")).replace("https://doi.org/", ""),
        "title": _txt(title),
        "abstract": _txt(d.get("abstract")),
        "type": _txt(d.get("type")),
        "language": _txt(d.get("language")),
        "publicationYear": _txt(d.get("publicationYear") or (src.get("firstPublication") or {}).get("year")),
        "publicationStage": _txt(d.get("publicationStage")),
        "openAccess": d.get("openAccess"),
        "citedBy": d.get("citedBy"),
        "pubmedInfo": d.get("pubmedInfo"),
        "source": src,
        "issn": _txt(src.get("issn")),
        "volume": _txt(src.get("volume")),
        "issue": _txt(src.get("issue")),
        "pages": _txt(src.get("pages")),
        "articleNumber": _txt(src.get("articleNumber")),
        "publisher": _txt(src.get("publisher")),
        "authors": d.get("authors") or [],
        "affiliations": d.get("affiliations") or [],
        "authorKeywords": d.get("authorKeywords") or [],
        "indexedKeywords": d.get("indexedKeywords") or {},
        "fundingDetails": d.get("fundingDetails") or {},
        "correspondences": d.get("correspondences") or [],
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if KEEP_RAW:
        rec["raw"] = d
    return rec


def load_list_eids(day):
    """当天列表档里的 eid，保序去重。"""
    path = list_path(day)
    out, seen = [], set()
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                v = json.loads(line)
            except Exception:
                continue
            if (v.get("load_date") or "")[:10] != day:
                continue
            k = (v.get("eid") or "").strip()
            if k and k not in seen:
                seen.add(k)
                out.append(k)
    return out


def load_have_details(months=None):
    """已有详情的 eid 集合。只读正式月档 scopus_detail_YYYY-MM.jsonl。"""
    have = set()
    try:
        names = os.listdir(out_dir())
    except Exception:
        return have
    for name in names:
        m = re.match(r"^scopus_detail_(\d{4}-\d{2})\.jsonl$", name)
        if not m:
            continue
        if months and m.group(1) not in months:
            continue
        path = os.path.join(out_dir(), name)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        v = json.loads(line)
                    except Exception:
                        continue
                    k = (v.get("eid") or "").strip()
                    if k:
                        have.add(k)
        except Exception:
            continue
    return have


def append_detail(rec):
    day = (rec.get("load_date") or "")[:10]
    if not day or not rec.get("eid"):
        return False
    path = detail_path(day)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return True


def harvest_day(pg, day, have, budget_left_fn, max_n=DETAIL_MAX):
    """拉这一天还缺的详情。返回 {need, have, new, fail, left, stop}。

    stop: '' | 'auth' | 'budget' | 'http'
    """
    eids = load_list_eids(day)
    todo = [e for e in eids if e not in have]
    st = {"need": len(eids), "have": len(eids) - len(todo),
          "new": 0, "fail": 0, "left": len(todo), "stop": ""}
    if not todo:
        log("[day-full]", day, "need", st["need"], "already", st["have"])
        return st
    log("[day-start]", day, "need", st["need"], "todo", len(todo),
        "cap", max_n)
    n_http = 0
    for i, eid in enumerate(todo):
        if i >= max_n:
            st["stop"] = "cap"
            break
        if budget_left_fn and budget_left_fn() <= 2:
            st["stop"] = "budget"
            log("[budget-stop]", day, "已拉", st["new"])
            break
        d, http = sc.fetch_doc_details(pg, eid)
        if http in (401, 403):
            st["stop"] = "auth"
            log("[auth-stop]", day, eid, http)
            break
        if http == 404 or (http == 200 and not d):
            rec = {"eid": eid, "load_date": day, "missing": True,
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            if append_detail(rec):
                have.add(eid)
            st["fail"] += 1
            n_http = 0
            continue
        if http and http != 200:
            n_http += 1
            st["fail"] += 1
            if n_http >= 6:
                st["stop"] = "http"
                log("[http-stop]", day, "连续非200", n_http)
                break
            continue
        n_http = 0
        if not d:
            st["fail"] += 1
            continue
        rec = pick_detail(d, day)
        if not rec.get("eid"):
            rec["eid"] = eid
        if append_detail(rec):
            have.add(eid)
            st["new"] += 1
        if st["new"] and st["new"] % 20 == 0:
            log("[pace-n]", day, "new", st["new"], "todo", len(todo) - i - 1)
    st["left"] = sum(1 for e in eids if e not in have)
    log("[day-done]", day, "new", st["new"], "fail", st["fail"],
        "left", st["left"], "stop", st["stop"] or "ok")
    return st


def complete_days(verify):
    """列表已采全的天（ok=True）。upstream-empty 没有列表，不拉详情。"""
    out = []
    for day, v in (verify or {}).items():
        if not isinstance(v, dict):
            continue
        if not v.get("ok"):
            continue
        if (v.get("note") or "") == "upstream-empty":
            continue
        if int(v.get("total") or 0) <= 0 and int(v.get("uniq") or 0) <= 0:
            continue
        out.append(day)
    # 小天先齐：09-03 只有 6 条，一轮就能拉完；09-05 五万条按 400/轮慢慢补。
    out.sort(key=lambda d: int((verify.get(d) or {}).get("uniq") or 0))
    return out


def run(pg, verify, budget_left_fn, just_completed=None, max_n=DETAIL_MAX):
    """本轮详情入口。先拉刚采全的天，再拉历史上已齐、详情还缺的天。"""
    days = complete_days(verify)
    if not days:
        log("[skip]", "没有列表已齐的天")
        return {}
    prefer = [d for d in (just_completed or []) if d in days]
    rest = [d for d in days if d not in prefer]
    order = prefer + rest
    months = set(d[:7] for d in order)
    have = load_have_details(months)
    log("[have]", len(have), "order", order[:8], "n", len(order))
    out = {}
    for day in order:
        if budget_left_fn and budget_left_fn() <= 2:
            log("[budget-stop]", "详情未开", day)
            break
        st = harvest_day(pg, day, have, budget_left_fn, max_n=max_n)
        out[day] = st
        if st.get("stop") in ("auth", "http"):
            break
        # 一天没拉完就换下一天？不 —— **只有真的被 cap/budget 截断才留下轮**。
        # 原条件把 stop=="" 也算进来，于是一条 429 残渣就能废掉整轮预算：
        # 09-28 21:08 实测 [day-done] 2026-09-27 new 6 fail 1 left 1 stop ok，
        # 然后 [detail-summary] 剩 115.3 分钟直接收工（120 分钟的详情段只用了 1.4 分钟）。
        # 正常走完 todo 的失败条目不会被记进 have，下轮会自然重试，不需要在这里卡住。
        if st.get("left", 0) > 0 and st.get("stop") in ("cap", "budget"):
            break
    return out


def verify_from_list(months=None):
    """自己从正式列表月档算出「有哪些天、每天多少 unique eid」。

    为什么不让它继续读 scopus_day_verify.json：那是**按天线的产物**。按天线
    2026-09-28 已整段退役，那个文件冻结在 09-24 12:16 再也不会更新，照它跑
    只会永远重复同样几天、新采的天一条都排不进队。详情线的输入必须自足。
    """
    per_day = {}
    try:
        names = os.listdir(out_dir())
    except Exception:
        return {}
    for name in names:
        m = MONTH_FILE_RE.match(name)
        if not m:
            continue
        if months and m.group(1) not in months:
            continue
        path = os.path.join(out_dir(), name)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        v = json.loads(line)
                    except Exception:
                        continue
                    d = (v.get("load_date") or "")[:10]
                    e = (v.get("eid") or "").strip()
                    if d and e:
                        per_day.setdefault(d, set()).add(e)
        except Exception:
            continue
    return {d: {"ok": True, "total": len(s), "uniq": len(s), "note": ""}
            for d, s in per_day.items() if s}


def main():
    """独立跑：python scopus_details.py [YYYY-MM-DD ...]  （自己落地会话）

    注意：生产里**不用**这个入口 —— 详情由 scopus_journal.py 在同一个浏览器
    会话里调用。单独运行会另开一个 land()，而实测两次 land() 相隔 1~2 分钟
    就会把站点顶成「同一帐号同一时刻只能一人在线」。
    """
    from camoufox.sync_api import Camoufox
    from wos_pipeline import load_pw_cookies, cookies_still_vip, inject_cookie_list
    import scopus_le1000 as le

    args = [a for a in sys.argv[1:] if re.match(r"\d{4}-\d{2}-\d{2}$", a)]
    cam, br, app = le.land()
    if app is None:
        return 3
    t0 = time.time()
    budget = float(os.environ.get("BOOKNOTE_SCOPUS_DETAIL_BUDGET") or 40)

    def left():
        return budget - (time.time() - t0) / 60.0

    verify = {}
    if args:
        for d in args:
            verify[d] = {"ok": True, "total": 1, "uniq": 1, "note": ""}
    else:
        verify = verify_from_list()
        log("[days-from-list]", len(verify), sorted(verify)[:5])
    try:
        run(app, verify, left, just_completed=args)
    finally:
        le._close(br)
    return 0


if __name__ == "__main__":
    sys.exit(main())
