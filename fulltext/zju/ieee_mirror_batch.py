# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# ieee_mirror_batch.py — IEEE 镜像站纯 requests 批量（远程）
# 链路：doi.org 解析 arnumber → 镜像 stamp.jsp 取 getPDF(ref) → %PDF 落盘 <tid>.pdf
import sys, os, time, re, random
import requests
_log = open(_os.path.join(LOGS, r"ieee_mirror_batch.log"), "ab", buffering=0)
os.dup2(_log.fileno(), 1)
os.dup2(_log.fileno(), 2)
sys.stdout = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")

LOG_DIR = LOGS
OUT_DIR = _os.path.join(PDFS, r"IEEE Xplore")
TODO = _os.path.join(FT, r"ieee_map_todo.txt")
DONE = os.path.join(LOG_DIR, "ieee_zju_done.txt")
MISS = os.path.join(LOG_DIR, "ieee_zju_miss.txt")
BASE = os.environ.get("IEEE_MIRROR", "https://ieeexplore.66557.net")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0"}
SLEEP = (6, 13)
CHUNK_EVERY, CHUNK_S = 40, 90
MAX_CONSEC_BAD = 8

_p = lambda m: print(time.strftime("%H:%M:%S") + " " + str(m), flush=True)


def arnumber_via_doi(s, doi):
    try:
        rr = s.get("https://doi.org/" + doi, allow_redirects=True, timeout=(15, 40))
        m = re.search(r"/document/(\d+)", rr.url or "")
        if m:
            return m.group(1)
    except Exception:
        pass
    m0 = re.search(r"\.(\d{5,10})$", doi.strip())
    return m0.group(1) if m0 else ""


def fetch_pdf(s, ar):
    try:
        r = s.get("%s/stamp/stamp.jsp?tp=&arnumber=%s" % (BASE, ar), timeout=60)
        if r.status_code != 200:
            return None, "stamp-st%s" % r.status_code
        m = re.search(r'src="([^"]*getPDF\.jsp[^"]*)"', r.text) or \
            re.search(r'((?:https?://[^\s"\']*|/)stampPDF/getPDF\.jsp\?[^\s"\']+)', r.text)
        if not m:
            return None, "no-getpdf-link"
        gp = m.group(1).replace("&amp;", "&")
        if gp.startswith("/"):
            gp = BASE + gp
        rp = s.get(gp, timeout=(20, 180), headers={"Referer": "%s/stamp/stamp.jsp?tp=&arnumber=%s" % (BASE, ar)})
        body = rp.content or b""
        if body[:5] == b"%PDF-":
            return body, "ok"
        return None, "getpdf-st%s-len%d" % (rp.status_code, len(body))
    except Exception as e:
        return None, "err:" + str(e)[:60]


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
    _p("start todo=%d total=%d mirror=%s" % (len(todo), len(items), BASE))
    if not todo:
        _p("ALL_DONE")
        return 0
    os.makedirs(OUT_DIR, exist_ok=True)
    s = requests.Session()
    s.headers.update(UA)
    ok_n = bad_n = consec = 0
    for i, (doi, tid) in enumerate(todo):
        out = os.path.join(OUT_DIR, tid + ".pdf")
        if os.path.exists(out) and os.path.getsize(out) > 10000:
            with open(DONE, "a", encoding="utf-8") as f:
                f.write(doi + "\n")
            continue
        ar = arnumber_via_doi(s, doi)
        if not ar:
            with open(MISS, "a", encoding="utf-8") as f:
                f.write(doi + "\tno-arnumber\n")
            continue
        body, note = fetch_pdf(s, ar)
        if body:
            open(out, "wb").write(body)
            with open(DONE, "a", encoding="utf-8") as f:
                f.write(doi + "\n")
            ok_n += 1
            consec = 0
            _p("[%d/%d] HIT %s ar=%s %dKB" % (i, len(todo), doi, ar, len(body) // 1024))
        else:
            with open(MISS, "a", encoding="utf-8") as f:
                f.write("%s\t%s\n" % (doi, note))
            bad_n += 1
            consec += 1
            _p("[%d/%d] MISS %s ar=%s %s" % (i, len(todo), doi, ar, note))
            if consec >= MAX_CONSEC_BAD:
                _p("%d consecutive bad -> pause 300s" % consec)
                time.sleep(300)
                consec = 0
        time.sleep(random.uniform(*SLEEP))
        if (i + 1) % CHUNK_EVERY == 0:
            _p("chunk pause %ds (ok=%d bad=%d)" % (CHUNK_S, ok_n, bad_n))
            time.sleep(CHUNK_S)
    _p("BATCH_DONE ok=%d bad=%d" % (ok_n, bad_n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
