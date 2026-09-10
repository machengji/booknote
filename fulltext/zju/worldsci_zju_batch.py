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
_log = open(_os.path.join(LOGS, r"worldsci_zju_batch.log"), "ab", buffering=0)
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
OUT_DIR = _os.path.join(PDFS, r"WorldScientific")
TODO = _os.path.join(FT, r"worldsci_map_todo.txt")
DONE = os.path.join(LOG_DIR, "worldsci_zju_done.txt")
MISS = os.path.join(LOG_DIR, "worldsci_zju_miss.txt")
BASE = "https://www.worldscientific.com"
SLEEP = (6, 12)
CHUNK_EVERY, CHUNK_S = 30, 90
MAX_CONSEC_BAD = 5

CRED_LIST = zju_creds()

_p = lambda m: print(time.strftime("%H:%M:%S") + " " + str(m), flush=True)


def fetch_pdf(pg, doi):
    url = "%s/doi/pdf/%s" % (BASE, doi)
    # 1) 内联尝试（无 download=true）
    try:
        r = pg.goto(url, wait_until="domcontentloaded", timeout=120000)
        time.sleep(3)
        body = r.body() if r else b""
        if body[:5] == b"%PDF-":
            return body, "ok"
    except Exception:
        pass
    # 2) download 事件接住（?download=true 是附件流）
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
    # ws_direct.txt 不存在 = 走住宅链 10889（直连 IP 被站点限流时换出口）
    use_chain = not os.path.exists(_os.path.join(FT, r"ws_direct.txt"))
    _p("proxy=%s" % ("chain-10889" if use_chain else "direct"))
    with Camoufox(headless=True, proxy={"server": "http://127.0.0.1:10889"} if use_chain else None,
                  exclude_addons=["ublock-origin"]) as b:
        pg = b.new_page()
        pg.set_default_timeout(120000)
        if not cas_login(pg):
            _p("LOGIN_FAIL")
            return 2
        pg.goto(BASE + "/action/ssostart?redirectUri=" + urllib.parse.quote("/", safe="/") +
                "&idp=" + urllib.parse.quote("https://idp.zju.edu.cn/idp/shibboleth", safe=""),
                wait_until="domcontentloaded")
        for i in range(20):
            time.sleep(5)
            low = (safe_content(pg) or "").lower()
            if "zhejiang" in low or "access provided" in low or "sign out" in low:
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
