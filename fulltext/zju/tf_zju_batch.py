# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# tf_zju_batch.py — T&F 浙大自有通道批量（绕开混沌/知献平台）
# CAS登录 → tandfonline ssostart → 同 context 循环 /doi/pdf/{doi} 抓 body
# 09-02 实测：ZJU 权益有效（10.1080 包 2/3 命中，Access provided by Zhejiang）
import sys, os, time, re, random, urllib.parse
def _mark(s):
    with open(_os.path.join(LOGS, r"tf_zju_mark.log"), "a", encoding="utf-8") as f:
        f.write(time.strftime("%H:%M:%S ") + s + "\n")
try:
    _mark("start")
    _log = open(_os.path.join(LOGS, r"tf_zju_batch.log"), "ab", buffering=0)
    os.dup2(_log.fileno(), 1)
    os.dup2(_log.fileno(), 2)
    sys.stdout = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    _mark("dup2-ok")
    from camoufox.sync_api import Camoufox
    _mark("camoufox-imported")
    from worldsci_zju_session import cas_login, safe_content
    _mark("session-imported")
except Exception:
    import traceback
    _mark("BOOT-ERR:" + traceback.format_exc()[-300:])

BASE = "https://www.tandfonline.com"
DOIS = _os.path.join(FT, r"tf_dois.txt")
OUT_DIR = _os.path.join(PDFS, r"Taylor & Francis")
DONE_CHAOS = _os.path.join(FT, r"tf_chaos_done.txt")   # 混沌线已收 DOI 列表（纯 DOI 行）
MISS = _os.path.join(FT, r"tf_zju_miss.txt")
MAX_CONSEC_BAD = 5


def safe_url(pg):
    try:
        return pg.url or ""
    except Exception:
        return ""


def fname(doi):
    return doi.replace("/", "_") + ".pdf"


def fetch_pdf(pg, doi):
    """1) /doi/pdf/{doi} 内联 body；2) CF 等待重试；3) ?download=true 附件流"""
    url = "%s/doi/pdf/%s" % (BASE, doi)
    try:
        r = pg.goto(url, wait_until="domcontentloaded", timeout=120000)
        time.sleep(3)
        body = r.body() if r else b""
        if body[:5] == b"%PDF-":
            return body, "ok"
        low = (safe_content(pg) or "").lower()
        if "just a moment" in low:
            for _ in range(24):
                time.sleep(5)
                low = (safe_content(pg) or "").lower()
                if "just a moment" not in low:
                    break
            try:
                r2 = pg.goto(url, wait_until="domcontentloaded", timeout=120000)
                time.sleep(3)
                b2 = r2.body() if r2 else b""
                if b2[:5] == b"%PDF-":
                    return b2, "ok-after-cf"
            except Exception:
                pass
    except Exception:
        pass
    try:
        with pg.expect_download(timeout=180000) as dli:
            try:
                pg.goto(url + "?download=true", wait_until="domcontentloaded", timeout=180000)
            except Exception:
                pass
        dl = dli.value
        tmp = os.path.join(OUT_DIR, "_tmp_%d.pdf" % int(time.time() * 1000))
        dl.save_as(tmp)
        body = open(tmp, "rb").read()
        try:
            os.remove(tmp)
        except Exception:
            pass
        if body[:5] == b"%PDF-":
            return body, "ok(download)"
        return None, "dl-not-pdf-%d" % len(body)
    except Exception as e:
        return None, "dl-err:" + str(e)[:50]


def main():
    dois = [l.strip() for l in open(DOIS, encoding="utf-8") if l.strip()]
    done_chaos = set()
    if os.path.exists(DONE_CHAOS):
        done_chaos |= {l.strip() for l in open(DONE_CHAOS, encoding="utf-8") if l.strip()}
    todo = [d for d in dois if d not in done_chaos]
    print("start total=%d chaos-done=%d todo=%d" % (len(dois), len(done_chaos), len(todo)), flush=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    ok_n = bad_n = consec = 0
    with Camoufox(headless=True, exclude_addons=["ublock-origin"]) as b:
        pg = b.new_page()
        pg.set_default_timeout(120000)
        if not cas_login(pg):
            print("LOGIN_FAIL", flush=True)
            return 2
        print("CAS OK", flush=True)
        try:
            pg.goto(BASE + "/action/ssostart?redirectUri=" + urllib.parse.quote("/", safe="/") +
                    "&idp=" + urllib.parse.quote("https://idp.zju.edu.cn/idp/shibboleth", safe="/"),
                    wait_until="domcontentloaded")
        except Exception as e:
            print("sso-err", str(e)[:80], flush=True)
        for i in range(20):
            time.sleep(5)
            low = (safe_content(pg) or "").lower()
            if "zhejiang" in low or "access provided" in low or "sign out" in low:
                print("[tf] inst OK", flush=True)
                break
            if "just a moment" not in low and i >= 8:
                break
        time.sleep(3)
        for i, doi in enumerate(todo):
            out = os.path.join(OUT_DIR, fname(doi))
            if os.path.exists(out) and os.path.getsize(out) > 10000:
                with open(DONE_CHAOS, "a", encoding="utf-8") as f:
                    f.write(doi + "\n")
                continue
            body, note = fetch_pdf(pg, doi)
            if body:
                open(out, "wb").write(body)
                with open(DONE_CHAOS, "a", encoding="utf-8") as f:
                    f.write(doi + "\n")
                ok_n += 1
                consec = 0
                print("[%d/%d] HIT %s %dKB" % (i, len(todo), doi, len(body) // 1024), flush=True)
            else:
                with open(MISS, "a", encoding="utf-8") as f:
                    f.write("%s\t%s\n" % (doi, note))
                bad_n += 1
                consec += 1
                print("[%d/%d] MISS %s %s" % (i, len(todo), doi, note), flush=True)
                if consec >= MAX_CONSEC_BAD:
                    consec = 0
                    if ok_n == 0 and bad_n >= 15:
                        print("%d bad with 0 ok -> session dead, exit" % bad_n, flush=True)
                        break
            time.sleep(random.uniform(2, 4))
        print("DONE ok=%d bad=%d" % (ok_n, bad_n), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
