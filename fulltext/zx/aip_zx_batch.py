# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# aip_zx_batch.py — AIP 知献网关批量（自含式）
# 流程: requests 取网关表单(zx会话) -> Camoufox 过 AIP CF -> 页面内提交登录表单 -> context.request 逐篇 /doi/pdf/
# cf_streak>=8 或登录失效 -> 进程内重铸（网关+登录）继续
import re, sys, os, time, random, pickle, requests, urllib3
_log = open(_os.path.join(LOGS, r"aip_batch.log"), "ab", buffering=0)
os.dup2(_log.fileno(), 1)
os.dup2(_log.fileno(), 2)
sys.stdout = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
urllib3.disable_warnings()
from camoufox.sync_api import Camoufox

ZX_BASE = "https://lib.zxsju.com"
OUT = _os.path.join(PDFS, r"AIP")
DOIS_FILE = _os.path.join(FT, r"aip_dois.txt")
ZX_PKL = _os.path.join(FT, r"_zx_login.pkl")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
MAX_CONSEC_BAD = 12


def fn(d):
    return d.replace("/", "_") + ".pdf"


def gw_form():
    """requests 取网关自动提交表单 -> (action, fields)"""
    s = pickle.load(open(ZX_PKL, "rb"))
    s.headers.update({"User-Agent": UA})
    SHOW = ZX_BASE + "/e/action/ShowInfo.php?classid=198&id=1914"
    r0 = s.get(SHOW, timeout=30, verify=False, headers={"Referer": ZX_BASE + "/e/action/ListInfo/?classid=63"})
    if "aip.php" not in r0.text:
        return None, "showinfo-no-aip(len=%d)" % len(r0.text)
    r = s.get("https://api.zhixianlib.cn/faner/AIP/aip.php", timeout=30, verify=False,
              headers={"Referer": SHOW})
    form = re.search(r'<form[^>]*action="([^"]+)"[^>]*>(.*?)</form>', r.text, re.S)
    if not form:
        return None, "no-form(len=%d)" % len(r.text)
    action = form.group(1)
    fields = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form.group(2)))
    return (action, fields), None


def build_session(b):
    """在给定 browser 内完成 CF 等待 + 登录表单提交，返回 (page, ok)"""
    (action, fields), err = gw_form()
    if err:
        print("[gw-fail]", err, flush=True)
        return None, False
    pg = b.new_page()
    pg.set_default_timeout(90000)
    try:
        pg.goto("https://pubs.aip.org/", timeout=90000, wait_until="domcontentloaded")
    except Exception as e:
        print("home-goto:", str(e)[:80], flush=True)
    ready = False
    for i in range(30):
        time.sleep(3)
        try:
            c = pg.content() or ""
        except Exception:
            c = ""
        if "just a moment" not in c.lower() and len(c) > 5000:
            ready = True
            print(f"[cf passed] t={i*3}s", flush=True)
            break
    if not ready:
        print("[cf] NEVER PASSED", flush=True)
        return pg, False
    js = ("async (flds) => { const f = document.createElement('form');"
          " f.method='POST'; f.action=" + repr(action) + ";"
          " for (const [k,v] of Object.entries(flds)) { const i=document.createElement('input');"
          " i.type='hidden'; i.name=k; i.value=v; f.appendChild(i);} document.body.appendChild(f);"
          " f.submit(); return 'ok'; }")
    try:
        pg.evaluate(js, fields)
    except Exception as e:
        print("submit-err", str(e)[:100], flush=True)
        return pg, False
    time.sleep(8)
    for i in range(10):
        try:
            u = pg.url or ""
        except Exception:
            u = ""
        if "aip.org" in u and "loginhandler" not in u.lower():
            print("[logged-in]", u[:80], flush=True)
            return pg, True
        time.sleep(3)
    print("[login] stuck at", (pg.url or "")[:80], flush=True)
    return pg, True  # 仍试下载，由结果判断


def build_good_session(b, test_doi, tries=6):
    """会话轮盘：铸会话后立即用已知可下 DOI 验证，坏会话立即丢弃"""
    for k in range(tries):
        pg, ok = build_session(b)
        if not ok:
            try:
                pg.close()
            except Exception:
                pass
            time.sleep(3)
            continue
        try:
            r = pg.context.request.get("https://pubs.aip.org/doi/pdf/" + test_doi,
                                       headers={"Accept": "application/pdf,*/*"}, timeout=90000)
            bdy = r.body()
            if bdy[:4] == b"%PDF-":
                open(os.path.join(OUT, fn(test_doi)), "wb").write(bdy)
                print("[roulette] GOOD session at try %d" % (k + 1), flush=True)
                return pg
            print("[roulette] try %d -> %s %d (governor)" % (k + 1, r.status, len(bdy)), flush=True)
        except Exception as e:
            print("[roulette] err", str(e)[:60], flush=True)
        try:
            pg.close()
        except Exception:
            pass
        time.sleep(4)
    return None


def main():
    dois = [l.strip() for l in open(DOIS_FILE, encoding="utf-8") if l.strip().startswith("10.1063/")]
    os.makedirs(OUT, exist_ok=True)
    total = len(dois)
    print("[aip-batch] total=%d" % total, flush=True)
    ok_n = bad_n = 0
    with Camoufox(headless=True, exclude_addons=["ublock-origin"]) as b:
        pg = build_good_session(b, dois[0])
        consec_bad = 0
        i = 0
        if pg is None:
            print("[roulette] no good session, exit", flush=True)
            print("[aip-batch] DONE ok=%d bad=%d" % (ok_n, bad_n), flush=True)
            return 0
        while i < total:
            d = dois[i]
            i += 1
            out = os.path.join(OUT, fn(d))
            if os.path.exists(out) and os.path.getsize(out) > 1024:
                continue
            got = None
            try:
                r = pg.context.request.get("https://pubs.aip.org/doi/pdf/" + d,
                                           headers={"Accept": "application/pdf,*/*"}, timeout=90000)
                bdy = r.body()
                if bdy[:4] == b"%PDF-":
                    open(out, "wb").write(bdy)
                    ok_n += 1
                    consec_bad = 0
                    print("[%d/%d] HIT %s %dKB" % (i, total, d, len(bdy) // 1024), flush=True)
                    continue
                if r.status in (403, 429) or b"just a moment" in bdy[:3000].lower():
                    got = "cf-%d" % r.status
                else:
                    got = "html-%d-%d" % (r.status, len(bdy))
            except Exception as e:
                got = "err:" + str(e)[:40]
            bad_n += 1
            consec_bad += 1
            print("[%d/%d] MISS %s %s ok=%d" % (i, total, d, got, ok_n), flush=True)
            if consec_bad >= MAX_CONSEC_BAD:
                print("[%d] rebuild session (consec_bad=%d)" % (i, consec_bad), flush=True)
                try:
                    pg.close()
                except Exception:
                    pass
                pg.close()
                pg = build_good_session(b, dois[0])
                consec_bad = 0
                if pg is None:
                    print("[rebuild-failed] exit", flush=True)
                    break
                continue
            time.sleep(random.uniform(1.0, 2.5))
        print("[aip-batch] DONE ok=%d bad=%d" % (ok_n, bad_n), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
