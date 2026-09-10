# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# chaos_tf_batch.py — 混沌书苑订阅批量下载（通用模板，按环境变量适配不同库）
# T&F 默认；IOP: CHAOS_BASE=https://iop.66557.net CHAOS_TPL="{base}/article/{doi}/pdf"
#           CHAOS_DOIS=iop_chaos_dois.txt CHAOS_DONE=iop_chaos_done.txt CHAOS_COOKIE_OUT=iop_chaos_cookies.json
# 用法: python chaos_tf_batch.py [limit]
import sys, os, time, json, random, requests, urllib3
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 10 ** 9
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROXY = os.environ.get("CHAOS_PROXY") or "http://127.0.0.1:10889"
BASE = os.environ.get("CHAOS_BASE", "https://www.tandfonline.com").strip()
TPL = os.environ.get("CHAOS_TPL", "{base}/doi/pdf/{doi}?download=true").strip()
DOIS_FILE = os.environ.get("CHAOS_DOIS", "tf_dois.txt")
DONE_LOG = os.environ.get("CHAOS_DONE", "tf_chaos_done.txt")
MISS_LOG = os.environ.get("CHAOS_MISS", "tf_chaos_miss.txt")
COOKIE_FILE = os.environ.get("CHAOS_COOKIE_OUT", "tf_chaos_cookies.json")
OUTDIR = os.environ.get("CHAOS_OUT") or os.path.join(BASE_DIR, "tf_chaos_pdfs")

os.makedirs(OUTDIR, exist_ok=True)
def _p(f):
    return f if os.path.isabs(f) else os.path.join(BASE_DIR, f)

done = set()
if os.path.exists(_p(DONE_LOG)):
    done = {l.strip() for l in open(_p(DONE_LOG), encoding="utf-8") if l.strip()}

# 本地已有 PDF 视为完成（点号命名: 10.1080_xxx.pdf）
try:
    for fn in os.listdir(OUTDIR):
        if fn.endswith(".pdf"):
            done.add(fn[:-4].replace("_", "/"))
except Exception:
    pass

all_dois = [l.strip() for l in open(_p(DOIS_FILE), encoding="utf-8") if l.strip()]
todo = [d for d in all_dois if d not in done]
print(f"todo: {len(todo)} (done {len(done)}/{len(all_dois)})", flush=True)

try:
    UA = open(_p(COOKIE_FILE) + ".ua", encoding="utf-8").read().strip()
except Exception:
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"

def _rsc_year(doi):
    # 10.1039/{X}{Y}{code}{seq} -> year = 2000 + (ord(X)-ord('a')-1)*10 + int(Y)
    try:
        s = doi.split("/", 1)[1]
        x = s[0].lower(); y = int(s[1])
        return 2000 + (ord(x) - ord("a") - 1) * 10 + y
    except Exception:
        return 2020

def _rsc_code(doi):
    try:
        return doi.split("/", 1)[1][2:4].upper()
    except Exception:
        return "CC"

def build_session():
    s = requests.Session()
    for c in json.load(open(_p(COOKIE_FILE), encoding="utf-8")):
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    return s

s = build_session()
proxies = None if PROXY.strip().lower() in ("direct", "", "none") else {"http": PROXY, "https": PROXY}
consec_bad = 0
n_ok = 0

for i, doi in enumerate(todo[:LIMIT], 1):
    fn = os.path.join(OUTDIR, doi.replace("/", "_") + ".pdf")
    if os.path.exists(fn) and os.path.getsize(fn) > 10000:
        with open(_p(DONE_LOG), "a", encoding="utf-8") as f:
            f.write(doi + "\n")
        continue
    url = TPL.format(base=BASE, doi=doi, doi_suffix=doi.split("/", 1)[1] if "/" in doi else doi,
                     doi_suffix_upper=(doi.split("/", 1)[1] if "/" in doi else doi).upper(),
                     rsc_year=_rsc_year(doi), rsc_code=_rsc_code(doi))
    ok = False
    try:
        r = s.get(url, headers={"User-Agent": UA, "Accept": "application/pdf,*/*", "Referer": BASE + "/", "Connection": "close"},
                  proxies=proxies, timeout=(20, 90), verify=False)
        if r.content[:5] == b"%PDF-" and len(r.content) > 8000:
            open(fn, "wb").write(r.content)
            with open(_p(DONE_LOG), "a", encoding="utf-8") as f:
                f.write(doi + "\n")
            n_ok += 1
            ok = True
            print(f"[{i}] HIT  {doi} {len(r.content)//1024}KB", flush=True)
        else:
            print(f"[{i}] MISS {doi} {r.status_code} {r.content[:50]!r}", flush=True)
    except Exception as e:
        print(f"[{i}] ERR  {doi} {str(e)[:90]}", flush=True)
    if ok:
        consec_bad = 0
    else:
        consec_bad += 1
        with open(_p(MISS_LOG), "a", encoding="utf-8") as f:
            f.write(doi + "\n")
    if consec_bad >= 8:
        print("SESSION DEAD (8 consecutive misses) — stopping for refresh", flush=True)
        break
    time.sleep(random.uniform(2.5, 5.5))

print(f"batch done: +{n_ok} this run", flush=True)
