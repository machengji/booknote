# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# jstage_batch.py — J-STAGE 遗留池批量（10.1248 等，经 mihomo HK 7890）
# 来源: _leftover_all.tsv 前缀 10.1248；输出 E:\pdfs\J-STAGE\{tid}.pdf（与早期爬虫命名一致）
# 断点续传：文件存在即跳过；PDF 链接从 doi.org 落地页提取
import re, sys, os, time, random, requests, urllib3
_log = open(_os.path.join(LOGS, r"jstage_batch.log"), "ab", buffering=0)
os.dup2(_log.fileno(), 1)
os.dup2(_log.fileno(), 2)
sys.stdout = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
urllib3.disable_warnings()

PROXIES = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
OUT = _os.path.join(PDFS, r"J-STAGE")
SRC = _os.path.join(FT, r"_leftover_all.tsv")
PREFIX = "10.1248"
MAX_CONSEC_BAD = 15


def fetch(sess, url, **kw):
    for a in range(3):
        try:
            return sess.get(url, timeout=(20, 300), **kw)
        except Exception as e:
            if a == 2:
                raise
            time.sleep(2 + a * 2)


def get_pdf_url(sess, doi):
    r = fetch(sess, "https://doi.org/" + doi, allow_redirects=True)
    if r.status_code != 200:
        return None, "doi-%d" % r.status_code
    m = re.search(r'href="([^"]*_pdf[^"]*)"', r.text, re.I)
    if not m:
        return None, "no-pdf-link"
    u = m.group(1).replace("&amp;", "&")
    if u.startswith("/"):
        base = re.match(r"(https://[^/]+)", r.url).group(1)
        u = base + u
    return u, None


def main():
    items = []
    for line in open(SRC, encoding="utf-8"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 3 and parts[0] == PREFIX:
            items.append((parts[1], parts[2]))
    os.makedirs(OUT, exist_ok=True)
    todo = [(tid, doi) for (tid, doi) in items
            if not os.path.exists(os.path.join(OUT, tid + ".pdf"))]
    print("[jstage] total=%d todo=%d" % (len(items), len(todo)), flush=True)
    if not todo:
        print("[jstage] ALL_DONE", flush=True)
        return
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://www.jstage.jst.go.jp/"})
    s.proxies.update(PROXIES)
    ok = bad = consec = 0
    for i, (tid, doi) in enumerate(todo, 1):
        out = os.path.join(OUT, tid + ".pdf")
        try:
            u, err = get_pdf_url(s, doi)
            if not u:
                bad += 1
                consec += 1
                print("[%d/%d] MISS %s %s" % (i, len(todo), doi, err), flush=True)
            else:
                r = fetch(s, u)
                if r.content[:4] == b"%PDF":
                    open(out, "wb").write(r.content)
                    ok += 1
                    consec = 0
                    print("[%d/%d] HIT %s %dKB" % (i, len(todo), doi, len(r.content) // 1024), flush=True)
                else:
                    bad += 1
                    consec += 1
                    print("[%d/%d] MISS %s not-pdf-%d-%d" % (i, len(todo), doi, r.status_code, len(r.content)), flush=True)
        except Exception as e:
            bad += 1
            consec += 1
            print("[%d/%d] ERR %s %s" % (i, len(todo), doi, str(e)[:50]), flush=True)
        if consec >= MAX_CONSEC_BAD:
            print("[%d] %d consec bad -> pause 300s" % (i, consec), flush=True)
            time.sleep(300)
            consec = 0
        time.sleep(random.uniform(0.5, 1.5))
    print("[jstage] DONE ok=%d bad=%d" % (ok, bad), flush=True)


if __name__ == "__main__":
    main()
