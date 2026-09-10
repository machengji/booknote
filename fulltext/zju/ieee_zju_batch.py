# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# ieee_zju_batch.py — IEEE ZJU 单会话批量下载（远程）
# 设计：一次 Camoufox = CAS登录 + 同context批量下载（TSPD 指纹Cookie 不跨浏览器传递）
# 自愈：连续3篇失败 → 关浏览器 → 重开重新登录 → 继续；--probe1 只试1篇并打全响应日志
import sys, os, time, json, re, random, base64
_log = open(_os.path.join(LOGS, r"ieee_zju_batch.log"), "ab", buffering=0)
os.dup2(_log.fileno(), 1)
os.dup2(_log.fileno(), 2)
sys.stdout = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
from camoufox.sync_api import Camoufox

LOG_DIR = LOGS
OUT_DIR = _os.path.join(PDFS, r"IEEE Xplore")
TODO = _os.path.join(FT, r"ieee_map_todo.txt")
DONE = os.path.join(LOG_DIR, "ieee_zju_done.txt")
MISS = os.path.join(LOG_DIR, "ieee_zju_miss.txt")
STATE = _os.path.join(FT, r"ieee_zju_state.json")
SLEEP = (9, 16)
CHUNK_EVERY, CHUNK_S = 30, 120
MAX_CONSEC_BAD = 3
CRED_LIST = zju_creds()
WAYF = ("https://ieeexplore.ieee.org/servlet/wayf.jsp"
        "?entityId=https://idp.zju.edu.cn/idp/shibboleth"
        "&url=https://ieeexplore.ieee.org/Xplore/home.jsp")

_p = lambda m: print(time.strftime("%H:%M:%S") + " " + str(m), flush=True)
DBG = False


def cas_login(pg, user, pwd):
    """同浏览器内 CAS 干净登录（无验证码窗口），成功返回 True"""
    pg.goto(WAYF, wait_until="domcontentloaded", timeout=90000)
    time.sleep(6)
    for _ in range(2):
        c0 = pg.content() or ""
        if "锁定" in c0 or "登录异常" in c0:
            m = re.search(r"(\d{2})时\s*(\d{2})分\s*(\d{2})秒", c0)
            wait = min(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + 45, 700) if m else 320
            _p("[locked] wait %ds" % wait)
            time.sleep(wait)
            try:
                pg.reload(wait_until="domcontentloaded")
            except Exception:
                pass
            time.sleep(5)
        else:
            break
    eu, ep = pg.query_selector("#username"), pg.query_selector("#password")
    if not eu or not ep:
        _p("[cas] fields missing")
        return False
    vis = pg.evaluate("()=>{const g=id=>{const e=document.getElementById(id);return e?!!(e.offsetParent||e.getClientRects().length):false};return {a:g('authcode'),p:g('yzmPic')}}")
    if vis.get("p"):
        _p("[cas] captcha visible -> dirty, abort this round")
        return False
    eu.fill(user)
    ep.fill(pwd)
    pg.evaluate("var b=document.getElementById('dl'); if(b){b.click();} else {var f=document.querySelector('#fm1')||document.querySelector('form'); if(f){f.submit();}}")
    for i in range(15):
        time.sleep(3)
        u = pg.url or ""
        c = pg.content() or ""
        if "zjuam.zju.edu.cn" not in u or "Access provided by" in c:
            ok = "Access provided by" in c or "ieeexplore" in u
            _p("[cas] landed %s ok=%s" % (u[:90], ok))
            return ok
        if "用户名或密码错误" in c:
            _p("[cas] WRONG password for %s" % user)
            return False
        if "锁定" in c:
            _p("[cas] locked after submit")
            return False
    _p("[cas] stuck")
    return False


def arnumber_via_doi(doi):
    try:
        import requests as _rq
        rr = _rq.get("https://doi.org/" + doi, allow_redirects=True, timeout=(20, 60),
                     headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0"})
        m = re.search(r"/document/(\d+)", rr.url or "")
        if m:
            return m.group(1)
    except Exception:
        pass
    m0 = re.search(r"\.(\d{5,10})$", doi.strip())
    return m0.group(1) if m0 else ""


def inpage_fetch_pdf(pg, url):
    """页内同源 fetch getPDF/ielx，返回 PDF bytes 或 None"""
    try:
        b64 = pg.evaluate(
            """async (u) => {
                const r = await fetch(u, {credentials: 'include'});
                const buf = await r.arrayBuffer();
                const arr = new Uint8Array(buf);
                let s = '';
                for (let i = 0; i < arr.length; i += 0x8000) {
                    s += String.fromCharCode.apply(null, arr.subarray(i, i + 0x8000));
                }
                return btoa(s);
            }""", url)
        raw = base64.b64decode(b64) if b64 else b""
        if raw[:5] == b"%PDF-":
            return raw
        if raw:
            open(os.path.join(LOG_DIR, "_ieee_inpage_resp.bin"), "wb").write(raw[:60000])
    except Exception:
        pass
    return None


def fetch_pdf(pg, ar, proven=False):
    """stamp.jsp 停留 + 监听器 + 帧轮询 + 页内fetch + 重掷TSPD 多重抓取"""
    got_url = "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber=%s&ref=" % ar
    got = {"rsp": None}

    def on_resp(rsp):
        try:
            ct = (rsp.headers or {}).get("content-type", "")
            url = rsp.url or ""
            if DBG:
                print("    [resp] %s %s ct=%s" % (rsp.status, url[:110], ct[:30]), flush=True)
            if got["rsp"] is None and ("application/pdf" in ct or "getPDF.jsp" in url
                                       or re.search(r"/ielx?\d+/.*\.pdf", url)):
                got["rsp"] = rsp
        except Exception:
            pass

    body = None
    for attempt in range(2):
        pg.on("response", on_resp)
        try:
            pg.goto("https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=%s" % ar,
                    wait_until="domcontentloaded", timeout=90000)
        except Exception:
            pass
        # 关键：Shape Security 扣住 PDF 响应约30s等行为传感器通过，
        # 等待期间绝不能做任何 CDP 活动（帧轮询/evaluate 都会让传感器判负）
        time.sleep(40 if not proven else 20)
        if got["rsp"] is not None:
            try:
                b = got["rsp"].body()
                if b and b[:5] == b"%PDF-":
                    body = b
            except Exception:
                pass
        try:
            pg.remove_listener("response", on_resp)
        except Exception:
            pass
        if body:
            return body, "ok"
        # 静默等待未放行：页内 fetch 构造好的 getPDF.jsp（此刻传感器已完成，XHR 可能放行）
        raw = inpage_fetch_pdf(pg, got_url)
        if raw:
            return raw, "ok(inpage)"
        if attempt < 1:
            _p("[retry-tspd] ar=%s" % ar)
            time.sleep(6)
    return None, "no-pdf(ar=%s)" % ar


def session_round(todo_left):
    """一个 Camoufox 进程：A-context 登录存 state → B-context 复刻成功路径探针 → 命中则收获。
    返回 (剩余todo, 是否换下一个浏览器)"""
    ok_n = bad_n = 0
    with Camoufox(headless=True, exclude_addons=["ublock-origin"]) as b:
        # A: 登录
        pga = b.new_page()
        pga.set_default_timeout(90000)
        logged = False
        for user, pwd in CRED_LIST:
            try:
                if cas_login(pga, user, pwd):
                    logged = True
                    break
            except Exception as e:
                _p("[login-err] %s" % str(e)[:110])
        if not logged:
            _p("[session] login failed")
            return todo_left, ok_n, bad_n, True
        try:
            st = pga.context.storage_state()
        except Exception:
            st = None
        # B: 全新 context（首个导航=stamp.jsp，行为轨迹干净）
        ctxb = b.new_context(storage_state=st)
        pg = ctxb.new_page()
        pg.set_default_timeout(90000)
        proven = False
        idx = 0
        while idx < len(todo_left):
            doi, tid = todo_left[idx]
            out = os.path.join(OUT_DIR, tid + ".pdf")
            if os.path.exists(out) and os.path.getsize(out) > 10000:
                with open(DONE, "a", encoding="utf-8") as f:
                    f.write(doi + "\n")
                todo_left.pop(idx)
                continue
            ar = arnumber_via_doi(doi)
            if not ar:
                with open(MISS, "a", encoding="utf-8") as f:
                    f.write(doi + "\tno-arnumber\n")
                todo_left.pop(idx)
                continue
            try:
                body, note = fetch_pdf(pg, ar, proven)
            except Exception as e:
                body, note = None, "exc:" + str(e)[:70]
            if body:
                open(out, "wb").write(body)
                with open(DONE, "a", encoding="utf-8") as f:
                    f.write(doi + "\n")
                ok_n += 1
                proven = True
                todo_left.pop(idx)
                _p("[HIT] %s ar=%s %dKB (todo=%d)" % (doi, ar, len(body) // 1024, len(todo_left)))
            else:
                with open(MISS, "a", encoding="utf-8") as f:
                    f.write("%s\t%s\n" % (doi, note))
                bad_n += 1
                idx += 1
                _p("[MISS] %s ar=%s %s (proven=%s)" % (doi, ar, note, proven))
                if not proven:
                    _p("[session] probe article failed -> rotate browser")
                    return todo_left, ok_n, bad_n, True
                if bad_n - ok_n >= 3 and not _recent_hit(ok_n):
                    _p("[session] 3 net misses after proven -> rotate")
                    return todo_left, ok_n, bad_n, True
            time.sleep(random.uniform(*SLEEP))
    return todo_left, ok_n, bad_n, False


def _recent_hit(_):
    return False


def main():
    global DBG
    DBG = "--probe1" in sys.argv
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
    if DBG:
        todo = todo[:1]
    rounds = 0
    while todo and rounds < 20:
        rounds += 1
        _p("== browser round %d ==" % rounds)
        todo, ok_n, bad_n, again = session_round(todo)
        _p("== round %d done ok=%d bad=%d left=%d ==" % (rounds, ok_n, bad_n, len(todo)))
        if not again:
            break
        time.sleep(20)
    _p("BATCH_DONE todo_left=%d" % len(todo))
    return 0


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    sys.exit(main())
