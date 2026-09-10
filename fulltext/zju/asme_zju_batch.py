# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# worldsci_zju_batch.py — World Scientific 浏览器单会话批量（远程）
# CAS登录 → SSO → 同context循环 /doi/pdf/{doi}?download=true 抓 body（CF 直接过）
import sys, os, time, re, random, urllib.parse
_log = open(_os.path.join(LOGS, r"asme_zju_batch.log"), "ab", buffering=0)
os.dup2(_log.fileno(), 1)
os.dup2(_log.fileno(), 2)
sys.stdout = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
from camoufox.sync_api import Camoufox

def safe_url(pg):
    try:
        return pg.url or ""
    except Exception:
        return ""


def safe_content(pg):
    try:
        return pg.content() or ""
    except Exception:
        time.sleep(2)
        try:
            return pg.content() or ""
        except Exception:
            return ""


def cas_login(pg):
    """先经 IEEE WAYF 建 CAS 会话（probe2 实证路径），再静默 SSO 到 WorldSci"""
    wayf = ("https://ieeexplore.ieee.org/servlet/wayf.jsp?entityId=" +
            urllib.parse.quote("https://idp.zju.edu.cn/idp/shibboleth", safe="") +
            "&url=https://ieeexplore.ieee.org/Xplore/home.jsp")
    for user, pwd in CRED_LIST:
        try:
            pg.goto(wayf, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            print("[wayf-err]", str(e)[:80], flush=True)
            continue
        time.sleep(6)
        for _ in range(2):
            c0 = safe_content(pg)
            if "锁定" in c0 or "登录异常" in c0:
                m = re.search(r"(\d{2})时\s*(\d{2})分\s*(\d{2})秒", c0)
                w = min(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + 45, 700) if m else 320
                print("[locked] wait %ds" % w, flush=True)
                time.sleep(w)
                try:
                    pg.reload(wait_until="domcontentloaded")
                except Exception:
                    pass
                time.sleep(5)
            else:
                break
        time.sleep(2)
        eu, ep = pg.query_selector("#username"), pg.query_selector("#password")
        if not eu or not ep:
            print("[cas] no fields for %s" % user, flush=True)
            continue
        vis = pg.evaluate("()=>{const g=id=>{const e=document.getElementById(id);return e?!!(e.offsetParent||e.getClientRects().length):false};return {p:g('yzmPic')}}")
        if vis.get("p"):
            print("[cas] captcha up for %s, skip" % user, flush=True)
            continue
        eu.fill(user); ep.fill(pwd)
        pg.evaluate("var b=document.getElementById('dl'); if(b){b.click();}")
        ok = False
        for i in range(15):
            time.sleep(3)
            u = safe_url(pg)
            c = safe_content(pg)
            if "zjuam" not in u and ("Access provided by" in c or "ieeexplore" in u):
                ok = True
                print("[cas] OK via %s" % user, flush=True)
                break
            if "用户名或密码错误" in c:
                print("[cas] wrong pwd %s" % user, flush=True)
                break
        if ok:
            return True
    return False

LOG_DIR = LOGS
OUT_DIR = _os.path.join(PDFS, r"ASME")
TODO = _os.path.join(FT, r"asme_map_todo.txt")
DONE = os.path.join(LOG_DIR, "asme_zju_done.txt")
MISS = os.path.join(LOG_DIR, "asme_zju_miss.txt")
BASE = "https://asmedigitalcollection.asme.org"
SLEEP = (6, 12)
CHUNK_EVERY, CHUNK_S = 30, 90
MAX_CONSEC_BAD = 5

CRED_LIST = zju_creds()

REQ = __import__("requests").Session()
REQ.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0"})

_p = lambda m: print(time.strftime("%H:%M:%S") + " " + str(m), flush=True)


def fetch_pdf(pg, doi):
    """doi.org 规范URL → 等 citation_pdf_url meta → download-first 捕获 PDF"""
    final = ""
    try:
        rr = REQ.get("https://doi.org/" + doi, allow_redirects=True, timeout=(15, 40))
        final = rr.url or ""
    except Exception:
        pass
    if not final or "asme" not in final:
        final = BASE + "/doi/" + doi
    try:
        pg.goto(final, wait_until="domcontentloaded", timeout=90000)
    except Exception:
        pass
    # 等文章页就绪（citation_pdf_url meta 出现）
    link = ""
    for _ in range(10):
        time.sleep(2)
        h = safe_content(pg) or ""
        m = re.search(r'(https?://[^"\s]*article-pdf[^"\s]*\.pdf)', h)
        if not m:
            m = re.search(r'citation_pdf_url"\s+content="([^"]+)"', h)
        if m:
            link = m.group(1).replace("&amp;", "&")
            break
    if not link:
        return None, "no-pdf-link"
    if link.startswith("/"):
        link = BASE + link
    # download-first：Silverchair article-pdf 是附件流，第一发 goto 就要包住事件
    try:
        with pg.expect_download(timeout=150000) as dli:
            try:
                pg.goto(link, wait_until="domcontentloaded", timeout=150000)
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
    except Exception:
        pass
    # 没有下载事件 → 监听器抓内联 PDF（静默等待，不做 CDP 活动）
    got = {"rsp": None}

    def on_resp(rsp):
        try:
            ct = (rsp.headers or {}).get("content-type", "")
            if got["rsp"] is None and "application/pdf" in ct:
                got["rsp"] = rsp
        except Exception:
            pass

    pg.on("response", on_resp)
    try:
        pg.goto(link, wait_until="domcontentloaded", timeout=120000)
    except Exception:
        pass
    for _ in range(20):
        if got["rsp"] is not None:
            break
        time.sleep(3)
    try:
        pg.remove_listener("response", on_resp)
    except Exception:
        pass
    if got["rsp"] is not None:
        try:
            body = got["rsp"].body()
            if body[:5] == b"%PDF-":
                return body, "ok"
        except Exception:
            pass
    return None, "no-pdf-both"

def _fetch_pdf_old(pg, doi):
    url = "%s/doi/pdf/%s?download=true" % (BASE, doi)
    for attempt in range(2):
        try:
            r = pg.goto(url, wait_until="domcontentloaded", timeout=120000)
            time.sleep(3)
            body = r.body() if r else b""
            ct = (r.headers or {}).get("content-type", "") if r else ""
            if body[:5] == b"%PDF-":
                return body, "ok"
            low = (pg.content() or "").lower()
            if "just a moment" in low or "security verification" in low:
                # CF 放行需耐心（会话脚本实测 ~60-90s），绝不 reload 打断
                for _ in range(24):
                    time.sleep(5)
                    low = (safe_content(pg) or "").lower()
                    if "just a moment" not in low and "security verification" not in low:
                        break
                try:
                    r2 = pg.goto(url, wait_until="domcontentloaded", timeout=120000)
                    time.sleep(3)
                    b2 = r2.body() if r2 else b""
                    if b2[:5] == b"%PDF-":
                        return b2, "ok-after-cf"
                except Exception:
                    pass
                continue
            return None, "st%s ct=%s len=%d" % (r.status if r else "?", ct[:20], len(body))
        except Exception as e:
            if attempt == 1:
                return None, "err:" + str(e)[:60]
            time.sleep(10)
    return None, "cf-loop"


def main():
    items = []
    for line in open(TODO, encoding="utf-8"):
        line = line.strip()
        if line:
            parts = line.split("\t")
            if len(parts) >= 2:
                items.append((parts[0], parts[1]))
    done = set()
    if os.path.exists(DONE):
        done |= {l.split("\t")[0].strip() for l in open(DONE, encoding="utf-8") if l.strip()}
    todo = [(d, t) for (d, t) in items if d not in done]
    _p("start todo=%d total=%d" % (len(todo), len(items)))
    if not todo:
        _p("ALL_DONE")
        return 0
    os.makedirs(OUT_DIR, exist_ok=True)
    ok_n = bad_n = consec = 0
    with Camoufox(headless=True, exclude_addons=["ublock-origin"]) as b:
        pg = b.new_page()
        pg.set_default_timeout(120000)
        if not cas_login(pg):
            _p("LOGIN_FAIL")
            return 2
        pg.goto(BASE + "/Shibboleth.sso/Login?entityID=" + urllib.parse.quote("https://idp.zju.edu.cn/idp/shibboleth", safe="") +
                "&target=" + urllib.parse.quote(BASE + "/"), wait_until="domcontentloaded")
        for i in range(20):
            time.sleep(5)
            low = (safe_content(pg) or "").lower()
            if "zhejiang" in low or "浙江大学" in (safe_content(pg) or "") or "access provided" in low or "sign out" in low:
                _p("[ws] inst OK")
                break
            if "just a moment" not in low and i >= 8:
                break
        time.sleep(3)
        for i, (doi, tid) in enumerate(todo):
            out = os.path.join(OUT_DIR, tid + ".pdf")
            if os.path.exists(out) and os.path.getsize(out) > 10000:
                with open(DONE, "a", encoding="utf-8") as f:
                    f.write(doi + "\n")
                continue
            body, note = fetch_pdf(pg, doi)
            if body:
                open(out, "wb").write(body)
                with open(DONE, "a", encoding="utf-8") as f:
                    f.write(doi + "\n")
                ok_n += 1
                consec = 0
                _p("[%d/%d] HIT %s %dKB" % (i, len(todo), doi, len(body) // 1024))
            else:
                with open(MISS, "a", encoding="utf-8") as f:
                    f.write("%s\t%s\n" % (doi, note))
                bad_n += 1
                consec += 1
                _p("[%d/%d] MISS %s %s" % (i, len(todo), doi, note))
                if consec >= MAX_CONSEC_BAD:
                    # 跳过坏项继续；仅当开局全坏（会话死）才退出
                    consec = 0
                    if ok_n == 0 and bad_n >= 15:
                        _p("%d bad with 0 ok -> session dead, exit" % bad_n)
                        break
            time.sleep(random.uniform(*SLEEP))
            if (i + 1) % CHUNK_EVERY == 0:
                _p("chunk pause %ds (ok=%d bad=%d)" % (CHUNK_S, ok_n, bad_n))
                time.sleep(CHUNK_S)
    _p("BATCH_DONE ok=%d bad=%d" % (ok_n, bad_n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
