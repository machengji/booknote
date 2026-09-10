# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# T&F 批量下载（知献 um.ac.id 机构会话 + 住宅链）：仿 ox_zx_batch
# 命中=%PDF 保存；未订阅返回 200 HTML 记 miss（不算 cf）；403/429 记 cf
import re, sys, os, time, random, requests, urllib3, pickle, argparse, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

OUT_DEFAULT = _os.path.join(PDFS, r"Taylor & Francis")
SESSION_PKL = _os.path.join(FT, r"tf_session_zx.pkl")
CHAIN = "http://127.0.0.1:10889"
_lock = threading.Lock()

def load_session():
    st = pickle.load(open(SESSION_PKL, "rb"))
    return st["cookies"], st["ua"]

def out_path(out_dir, d):
    return os.path.join(out_dir, d.replace("/", "_").replace(".", "_").replace(":", "_") + ".pdf")

def build_jar(cookies):
    jar = requests.cookies.RequestsCookieJar()
    for c in cookies:
        for dom in (c.get("domain") or ".tandfonline.com", ".tandfonline.com"):
            try:
                jar.set(c["name"], c["value"], domain=dom, path=(c.get("path") or "/"))
            except Exception:
                pass
    return jar

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dois_file"); ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--sleep", type=float, default=2.0); ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--cf-exit", type=int, default=20)
    args = ap.parse_args()
    out_dir = args.out; os.makedirs(out_dir, exist_ok=True)
    cookies, ua = load_session()
    dois = [l.strip() for l in open(args.dois_file, encoding="utf-8") if l.strip()]
    miss_file = os.path.join(os.path.dirname(SESSION_PKL), "tf_missed_remote.txt")
    missed_set = set()
    if os.path.exists(miss_file):
        missed_set = {l.strip() for l in open(miss_file, encoding="utf-8") if l.strip()}
        print("[tf-batch] skip-miss list: %d" % len(missed_set), flush=True)
    total = len(dois)
    print("[tf-batch] total=%d workers=%d" % (total, args.workers), flush=True)
    proxies = {"http": CHAIN, "https": CHAIN}

    def worker(d):
        out = out_path(out_dir, d)
        if os.path.exists(out) and os.path.getsize(out) > 1024:
            return ("skip", d)
        if d in missed_set:
            return ("skipmiss", d)
        try:
            r = requests.get(f"https://www.tandfonline.com/doi/pdf/{d}?download=true",
                             headers={"User-Agent": ua, "Accept": "application/pdf",
                                      "Referer": f"https://www.tandfonline.com/doi/{d}"},
                             cookies=build_jar(cookies), proxies=proxies, timeout=60, verify=False)
            b = r.content
            if b[:4] == b"%PDF":
                with _lock:
                    open(out, "wb").write(b)
                return ("ok", d)
            if r.status_code in (403, 429) or b"just a moment" in b[:3000].lower():
                return ("cf", d)
            return ("miss", d)
        except Exception as e:
            return ("err", d, str(e)[:30])

    done = 0; counts = {"ok": 0, "skip": 0, "skipmiss": 0, "cf": 0, "miss": 0, "err": 0}; cf_streak = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(worker, d): d for d in dois}
        for fut in as_completed(futs):
            res = fut.result(); done += 1
            if res[0] == "ok":
                counts["ok"] += 1; print("[%d/%d][ok] %s" % (done, total, res[1]), flush=True)
            elif res[0] == "skip":
                counts["skip"] += 1
            elif res[0] == "skipmiss":
                counts["skipmiss"] += 1
            elif res[0] == "cf":
                counts["cf"] += 1; cf_streak += 1; print("[%d/%d][cf] %s" % (done, total, res[1]), flush=True)
            elif res[0] == "miss":
                counts["miss"] += 1
            else:
                counts["err"] += 1; print("[%d/%d][err] %s %s" % (done, total, res[1], res[2]), flush=True)
            if res[0] != "cf":
                cf_streak = 0
            if cf_streak >= args.cf_exit:
                print("[REBUILD_NEEDED] cf_streak=%d -> session dead" % cf_streak, flush=True)
                break
            if args.sleep and done % args.workers == 0:
                time.sleep(args.sleep)
    print("[DONE] ok=%d skip=%d cf=%d miss=%d err=%d" % (counts["ok"], counts["skip"], counts["cf"], counts["miss"], counts["err"]), flush=True)

if __name__ == "__main__":
    main()