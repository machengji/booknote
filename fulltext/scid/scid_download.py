# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# scidownload 通用下载器（机构 SSO 网关，机制与 wytsg/Wiley 同构）
#
# 用法：
#   scid_login.py        -> 生成 scid_session.pkl（卡号 SCID_USER）
#   python scid_download.py <库名> session   # 有头 Camoufox：进库入口，等你点一次 Turnstile/「第一步」
#   python scid_download.py <库名> bulk      # 无头 Camoufox：复用该库会话批量拉（可续传）
#
# 库名对应 {LIB_PREFIX}_dois.txt（每行一个 DOI）。PDF 落在 E:/pdfs/<库名>/
#
# 关键（与 Wiley 相同）：
#   1) 发布商域名是 Cloudflare，requests 复用会 403，必须用 Camoufox 建会话后 headless 复用
#   2) 每库每会话只需一次人工 Turnstile 点选（或接付费验证码服务）
#   3) 不要 pin UA 去解挑战（降低指纹导致卡死）；解完抓真实 navigator.userAgent 存会话
import re, sys, os, time, json, pickle, requests, urllib3
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from camoufox.sync_api import Camoufox

BASE = "https://www.scidownload.com"
SCID = _os.path.join(FT, r"scid_session.pkl")
OUT_ROOT = _os.path.join(PDFS, r"scidownload")

# 库配置：entry = (classid, ShowInfo id)；hosts = 判定「已登录发布商」的域名正则
PUBLISHERS = {
    "sage":   {"entry": (197, 2834), "hosts": r"journals\.sagepub\.com",
               "pdf": lambda d: ["https://journals.sagepub.com/doi/pdf/" + d]},
    "tf":     {"entry": (204, 5462), "hosts": r"tandfonline\.com",
               "pdf": lambda d: ["https://www.tandfonline.com/doi/pdf/" + d]},
    "iop":    {"entry": (193, 3844), "hosts": r"iopscience\.iop\.org",
               "pdf": lambda d: ["https://iopscience.iop.org/article/" + d + "/pdf",
                                 "https://iopscience.iop.org/article/" + d + "/pdf"]},
    "oxford": {"entry": (200, 5336), "hosts": r"academic\.oup\.com",
               "pdf": lambda d: ["https://academic.oup.com/doi/pdf/" + d]},
    "aip":    {"entry": (213, 5347), "hosts": r"pubs\.aip\.org|scitation\.aip\.org",
               "pdf": lambda d: ["https://pubs.aip.org/doi/pdf/" + d]},
    "bioone": {"entry": (239, 5422), "hosts": r"bioone\.org|meridian\.allenpress\.com",
               "pdf": lambda d: ["https://bioone.org/journals/" + d.replace("/", "_") + "/article-pdf/"]},
    "muse":   {"entry": (239, 5585), "hosts": r"muse\.jhu\.edu",
               "pdf": lambda d: ["https://muse.jhu.edu/article/" + d + "/pdf"]},
}

def doi_filename(d):
    return d.replace("/", "_").replace(".", "_").replace(":", "_")

def cookie_list(ses, domain=".scidownload.com"):
    out = []
    for c in ses.cookies:
        out.append({"name": c.name, "value": c.value, "domain": domain, "path": "/"})
    return out

def phase_session(lib):
    cfg = PUBLISHERS[lib]
    cid, eid = cfg["entry"]
    entry = BASE + f"/e/action/ShowInfo.php?classid={cid}&id={eid}"
    sess_file = f_os.path.join(FT, r"{lib}_session.pkl")
    if os.path.exists(sess_file):
        os.remove(sess_file)
    scid = pickle.load(open("scid_session.pkl", "rb"))
    os.makedirs(OUT_ROOT, exist_ok=True)
    with Camoufox(headless=False) as browser:
        pg = browser.new_page()
        try:
            pg.context.add_cookies(cookie_list(scid))
        except Exception as e:
            print("add_cookies err", e)
        print(f"[session:{lib}] goto entry {entry}")
        pg.goto(entry, timeout=45000, wait_until="domcontentloaded", referer=BASE + "/yingwenku/")
        print("url:", pg.url[:90])
        # 等待用户完成 SSO（可能点「第一步」，过 Turnstile），直到进入发布商域名
        import re as _re
        arrived = False
        for i in range(240):  # 最多 12 分钟
            try:
                u = pg.url or ""
            except Exception:
                u = ""
            if _re.search(cfg["hosts"], u):
                arrived = True
                break
            if i in (0, 30, 60, 120, 180):
                print(f"  等待进入 {cfg['hosts']} ... 当前 url={u[:80]} （请在弹出的窗口完成登录/勾选 human 验证）")
            time.sleep(3)
        if not arrived:
            print("未进入发布商域名，可能需人工点击或入口失效。抓图存证。")
            try: pg.screenshot(path=f_os.path.join(FT, r"{lib}_session_fail.png"))
            except Exception: pass
            browser.close(); return
        print("已进入发布商:", pg.url[:100])
        time.sleep(5)
        cookies = {c["name"]: c["value"] for c in pg.context.cookies()}
        real_ua = pg.evaluate("() => navigator.userAgent")
        print("cookies:", list(cookies.keys()))
        print("UA:", real_ua[:70])
        ses = requests.Session()
        for c in pg.context.cookies():
            ses.cookies.set(c["name"], c["value"])
        ses.headers.update({"User-Agent": real_ua})
        with open(sess_file, "wb") as f:
            pickle.dump({"session": ses, "ua": real_ua, "hosts": cfg["hosts"]}, f)
        print("会话已保存 ->", sess_file)
        browser.close()

def phase_bulk(lib):
    cfg = PUBLISHERS[lib]
    sess_file = f_os.path.join(FT, r"{lib}_session.pkl")
    dois_file = f_os.path.join(FT, r"{lib}_dois.txt")
    if not os.path.exists(sess_file):
        print("无会话缓存，先运行 session 阶段"); return
    if not os.path.exists(dois_file):
        print("缺 DOI 文件", dois_file); return
    st = pickle.load(open(sess_file, "rb"))
    ses, ua = st["session"], st["ua"]
    out_dir = os.path.join(OUT_ROOT, lib)
    os.makedirs(out_dir, exist_ok=True)
    dois = [l.strip() for l in open(dois_file, encoding="utf-8") if l.strip()]
    log = open(f_os.path.join(FT, r"{lib}_bulk.log"), "a", encoding="utf-8")
    ok = skip = cf = html = err = 0
    cf_streak = 0
    probes = dois[:3] if len(dois) >= 3 else dois
    with Camoufox(headless=True) as browser:
        pg = browser.new_page()
        for c in ['cf_clearance', 'iam', 'JSESSIONID', 'SID', '_ga', 'sessionid']:
            if c in ses.cookies:
                try:
                    pg.context.add_cookies([{"name": c, "value": ses.cookies[c], "domain": ".scidownload.com", "path": "/"}])
                except Exception: pass
        # 注入会话 cookie（发布商域）
        for c in ses.cookies:
            for dom in ['.sagepub.com', '.tandfonline.com', '.iopscience.iop.org', '.oup.com',
                        '.aip.org', '.bioone.org', '.muse.jhu.edu', '.academic.oup.com']:
                try:
                    pg.context.add_cookies([{"name": c.name, "value": c.value, "domain": dom, "path": "/"}])
                except Exception: pass
        for idx, doi in enumerate(dois, 1):
            out = os.path.join(out_dir, doi_filename(doi) + ".pdf")
            if os.path.exists(out) and os.path.getsize(out) > 1000:
                ok += 1; continue
            got = None
            for cand in cfg["pdf"](doi):
                try:
                    r = pg.context.request.get(cand, headers={"Accept": "application/pdf"})
                except Exception as e:
                    err += 1; break
                b = r.body()
                st_ = r.status
                hdr = b[:5]
                if hdr == b"%PDF-":
                    got = b; break
                if st_ in (403, 429) or b"Just a moment" in b[:2000] or b"turnstile" in b[:2000].lower():
                    cf += 1; cf_streak += 1; break
                if hdr == b"%PDF" or b"%PDF" in b[:100]:
                    got = b; break
                # 其它：HTML
                cf_streak = 0
                html += 1; break
            if got is not None:
                with open(out, "wb") as f:
                    f.write(got)
                ok += 1; cf_streak = 0
                print(f"[{idx}/{len(dois)}][ok] {doi} {len(got)}", flush=True)
            if cf_streak >= 8:
                # 健康探测：若探针也失败则判定会话死
                alive = False
                for pr in probes:
                    for cand in cfg["pdf"](pr):
                        try:
                            rr = pg.context.request.get(cand, headers={"Accept": "application/pdf"})
                            if rr.body()[:5] == b"%PDF-":
                                alive = True; break
                        except Exception: pass
                    if alive: break
                if not alive:
                    print("!!! 会话已死（探测失败），请重跑 session 阶段再点一次。stop=True", flush=True)
                    log.write("DONE: stop=True\n"); log.close(); browser.close(); return
                cf_streak = 0
            log.write(f"[{idx}/{len(dois)}][{'ok' if got is not None else 'cf' if cf else 'html'}] {doi}\n")
            log.flush()
    log.write(f"DONE: ok={ok} cf={cf} html={html} err={err}\n")
    log.close()
    print(f"[bulk:{lib}] DONE ok={ok} cf={cf} html={html} err={err}")

if __name__ == "__main__":
    lib = sys.argv[1] if len(sys.argv) > 1 else "sage"
    phase = sys.argv[2] if len(sys.argv) > 2 else "session"
    if lib not in PUBLISHERS:
        print("可用库:", list(PUBLISHERS.keys())); sys.exit(1)
    if phase == "session":
        phase_session(lib)
    elif phase == "bulk":
        phase_bulk(lib)
    else:
        print("phase 应为 session|bulk")
