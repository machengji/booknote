# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# ovid_chaos_batch.py — 混沌书苑 Ovid(LWW) 批量下载（requests 三步：检索→PDFLink→中间页→PDF）
# 会话由 ovid_chaos_guard.py 用浏览器引导后写入 _ovid_session.json
import sys, os, time, json, random, re, requests, urllib3
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 10 ** 9
PROXY = os.environ.get("CHAOS_PROXY") or "http://127.0.0.1:10889"
DOIS_FILE = os.environ.get("OVID_DOIS", "ovid_dois.txt")
DONE_LOG = os.environ.get("OVID_DONE", "ovid_chaos_done.txt")
MISS_LOG = os.environ.get("OVID_MISS", "ovid_chaos_miss.txt")
OUTDIR = os.environ.get("OVID_OUT", _os.path.join(PDFS, r"Ovid"))
SESS_FILE = os.path.join(BASE_DIR, "_ovid_session.json")

def _p(f):
    return f if os.path.isabs(f) else os.path.join(BASE_DIR, f)

os.makedirs(OUTDIR, exist_ok=True)
S = json.load(open(SESS_FILE, encoding="utf-8"))
COOKIES = S["cookies"]
OVID = S["base"]  # https://ovidsp-dcX-ovid-com.ptchproxy.flysheet.com.tw:8443/ovid-new-b
SID = S["sid"]    # GUID|main
UA = S.get("ua", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0")

s = requests.Session()
for c in COOKIES:
    s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
proxies = {"http": PROXY, "https": PROXY}
HDRS = {"User-Agent": UA, "Referer": OVID + "/ovidweb.cgi", "Origin": OVID.split("/ovid-new-b")[0]}

done = set()
if os.path.exists(_p(DONE_LOG)):
    done = {l.strip() for l in open(_p(DONE_LOG), encoding="utf-8") if l.strip()}
try:
    for root, _, files in os.walk(OUTDIR):
        for fn in files:
            if fn.endswith(".pdf"):
                done.add(fn[:-4])
except Exception:
    pass

all_dois = [l.strip() for l in open(_p(DOIS_FILE), encoding="utf-8") if l.strip()]
todo = [d for d in all_dois if d not in done]
print(f"todo: {len(todo)} (done {len(done)}/{len(all_dois)})", flush=True)

def search_pdf_url(doi):
    # 1) 检索
    body = {
        "S": SID, "Mode Form": "easy", "SH Select": "", "textBox": doi,
        "submit:Perform Search|1": "", "showSpeedOverride": "1", "LIMIT-PREV": "",
        "Limit Selection List": "0", "LIMIT": "", "SHPage": "Main Search Page",
    }
    r = s.post(OVID + "/ovidweb.cgi", data=body, headers=HDRS, proxies=proxies, timeout=(20, 60), verify=False)
    if r.status_code != 200:
        raise RuntimeError(f"search {r.status_code}")
    h = r.text
    global _dumped
    if not _dumped:
        open(os.path.join(BASE_DIR, "_ovid_search_resp.html"), "w", encoding="utf-8").write(h)
        _dumped = True
        print("[dbg] search resp saved, len", len(h), flush=True)
    # 2) 找 Article as PDF 链接
    m = re.search(r'href="([^"]*PDFLink=[^"]+)"', h)
    if not m:
        return None  # 无匹配/无 PDF
    link = OVID + m.group(1) if m.group(1).startswith("?") else m.group(1)
    if link.startswith("?"):
        link = OVID + "/ovidweb.cgi" + link
    link = link.replace("&amp;", "&")
    # 3) 中间页 → 真实 PDF 地址
    r2 = s.get(link, headers=HDRS, proxies=proxies, timeout=(20, 60), verify=False)
    m2 = re.search(r'https?://[^"\'<>\s]+/ovftpdfs/[^"\'<>\s]+\.pdf', r2.text)
    if not m2:
        return None
    return m2.group(0)

consec_bad = 0
n_ok = 0
_dumped = False
for i, doi in enumerate(todo[:LIMIT], 1):
    safe = doi.replace("/", "_")
    fn = os.path.join(OUTDIR, safe + ".pdf")
    if os.path.exists(fn) and os.path.getsize(fn) > 10000:
        with open(_p(DONE_LOG), "a", encoding="utf-8") as f:
            f.write(doi + "\n")
        continue
    ok = False
    try:
        u = search_pdf_url(doi)
        if u:
            r3 = s.get(u, headers=HDRS, proxies=proxies, timeout=(20, 120), verify=False)
            if r3.content[:5] == b"%PDF-" and len(r3.content) > 8000:
                open(fn, "wb").write(r3.content)
                with open(_p(DONE_LOG), "a", encoding="utf-8") as f:
                    f.write(doi + "\n")
                n_ok += 1
                ok = True
                print(f"[{i}] HIT  {doi} {len(r3.content)//1024}KB", flush=True)
            else:
                print(f"[{i}] MISS {doi} pdf {r3.status_code} {r3.content[:40]!r}", flush=True)
        else:
            print(f"[{i}] MISS {doi} no-pdf-link", flush=True)
    except Exception as e:
        print(f"[{i}] ERR  {doi} {str(e)[:90]}", flush=True)
    if ok:
        consec_bad = 0
    else:
        consec_bad += 1
        with open(_p(MISS_LOG), "a", encoding="utf-8") as f:
            f.write(doi + "\n")
    if consec_bad >= 8:
        print("SESSION DEAD (8 consecutive) — stopping for refresh", flush=True)
        break
    time.sleep(random.uniform(2.0, 4.5))

print(f"batch done: +{n_ok} this run", flush=True)
