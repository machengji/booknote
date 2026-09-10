# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# Oxford 批量下载（知献网关会话 + 住宅链）：仿 wiley_parallel.py
# 用法: python ox_zx_batch.py <dois_file> [--out DIR] [--workers N] [--sleep S] [--cf-exit N]
import re, sys, os, time, random, requests, urllib3, pickle, argparse, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

OUT_DEFAULT = _os.path.join(PDFS, r"Oxford Academic")
SESSION_PKL = _os.path.join(FT, r"oxford_session_zx.pkl")
CHAIN = "http://127.0.0.1:10889"
_lock = threading.Lock()
# 直连模式：ox_direct.txt 存在 = 用远程本机出口 + oxford_session_direct.pkl（住宅链被 OUP 封时的替代）
DIRECT = os.path.exists(_os.path.join(FT, r"ox_direct.txt"))
if DIRECT:
    SESSION_PKL = _os.path.join(FT, r"oxford_session_direct.pkl")

def load_session():
    st = pickle.load(open(SESSION_PKL, "rb"))
    return st["cookies"], st["ua"]

def out_path(out_dir, d):
    return os.path.join(out_dir, d.replace("/", "_").replace(".", "_").replace(":", "_") + ".pdf")

def url_for(doi):
    p = doi.split("/")
    return f"https://academic.oup.com/{p[1]}/article-pdf/doi/{doi}/0/{p[-1]}.pdf"

def build_jar(cookies):
    jar = requests.cookies.RequestsCookieJar()
    for c in cookies:
        for dom in (c.get("domain") or ".academic.oup.com", ".academic.oup.com", ".oup.com"):
            try:
                jar.set(c["name"], c["value"], domain=dom, path=(c.get("path") or "/"))
            except Exception:
                pass
    return jar

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dois_file"); ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--sleep", type=float, default=2.0); ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--cf-exit", type=int, default=25)
    ap.add_argument("--max-try", type=int, default=0, help=">0 = 最多尝试 N 个 PDF 端点请求后退出（会话配额保护）")
    args = ap.parse_args()
    out_dir = args.out; os.makedirs(out_dir, exist_ok=True)
    cookies, ua = load_session()
    dois = [l.strip() for l in open(args.dois_file, encoding="utf-8") if l.strip()]
    total = len(dois)
    print("[ox-batch] total=%d workers=%d mode=%s" % (total, args.workers, "direct" if DIRECT else "chain"), flush=True)
    proxies = None if DIRECT else {"http": CHAIN, "https": CHAIN}

    def worker(d):
        out = out_path(out_dir, d)
        if os.path.exists(out) and os.path.getsize(out) > 1024:
            return ("skip", d)
        try:
            r = requests.get(url_for(d), headers={"User-Agent": ua, "Accept": "application/pdf"},
                             cookies=build_jar(cookies), proxies=proxies, timeout=60, verify=False)
            b = r.content
            if b[:4] == b"%PDF":
                with _lock:
                    open(out, "wb").write(b)
                return ("ok", d)
            if r.status_code in (403, 429) or b"just a moment" in b[:3000].lower():
                return ("cf", d)
            return ("html", d)
        except Exception as e:
            return ("err", d, str(e)[:30])

    done = 0; counts = {"ok": 0, "skip": 0, "cf": 0, "html": 0, "err": 0}; cf_streak = 0
    tried = 0; html_seen = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(worker, d): d for d in dois}
        for fut in as_completed(futs):
            res = fut.result(); done += 1
            if res[0] != "skip":
                tried += 1
            if res[0] == "html" and len(html_seen) < 3:
                html_seen.append(res[1])
            if args.max_try and tried >= args.max_try:
                print("[SESSION_SPENT] tried=%d ok=%d html=%d cf=%d -> stop, next window rebuilds" % (tried, counts["ok"], counts["html"], counts["cf"]), flush=True)
                break
            if res[0] == "ok":
                counts["ok"] += 1; print("[%d/%d][ok] %s" % (done, total, res[1]), flush=True)
            elif res[0] == "skip":
                counts["skip"] += 1
            elif res[0] == "cf":
                counts["cf"] += 1; cf_streak += 1; print("[%d/%d][cf] %s" % (done, total, res[1]), flush=True)
            elif res[0] == "html":
                counts["html"] += 1
            else:
                counts["err"] += 1; print("[%d/%d][err] %s %s" % (done, total, res[1], res[2]), flush=True)
            if done % 25 == 0:
                print("[hb] %d/%d ok=%d skip=%d cf=%d html=%d err=%d" % (done, total, counts["ok"], counts["skip"], counts["cf"], counts["html"], counts["err"]), flush=True)
            if res[0] != "cf":
                cf_streak = 0
            if cf_streak >= 3:
                print("[cooldown] cf_streak=%d -> pause 600s at %d" % (cf_streak, done), flush=True)
                time.sleep(600); cf_streak = 0
            if cf_streak >= args.cf_exit:
                print("[REBUILD_NEEDED] cf_streak=%d after %d done -> session dead, exiting" % (cf_streak, done), flush=True)
                break
            if args.sleep and res[0] != "skip" and done % args.workers == 0:
                time.sleep(max(args.sleep, 10.0))
    print("[DONE] ok=%d skip=%d cf=%d html=%d err=%d html_sample=%s" % (counts["ok"], counts["skip"], counts["cf"], counts["html"], counts["err"], html_seen[:3]), flush=True)

if __name__ == "__main__":
    main()