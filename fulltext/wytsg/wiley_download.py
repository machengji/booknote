# -*- coding: utf-8 -*-
"""
无忧下载 - Wiley 单篇/批量下载器（wytsg 文渊图书馆 WYTSG_USER）。

用法:
    python wiley_download.py            # 交互下载测试 DOI(10.1111/ablj.70014)
    python wiley_download.py --doi 10.1111/xxx.xxxx
    python wiley_download.py --bulk dois.txt   # 用已保存会话批量拉取

流程(到"人工点 Turnstile"之前全自动):
    wytsg 登录(a0j挑战+ddddocr验证码) -> /xm/xg/wiley.php?doi=.. -> docapi.xyz
    -> 自动提交 sharedSPMessage 到 authenticateSharedSP -> 停在 Cloudflare
    "Verify you are human"。请在浏览器窗口**手动勾选一次**。
    勾选后脚本自动检测放行 -> 校验全文访问 -> 抓 cf_clearance+授权cookie
    -> 下载测试PDF(校验 %PDF 头) -> 保存会话 wiley_session.pkl。

注意:
  - Camoufox 默认继承系统代理(FlClash)。--direct 用 network.proxy.type=0 走
    本地直连住宅 IP。两者都会出 Turnstile(人工验证门)，与 IP 无关。
  - 需已安装: pip install camoufox ddddocr playwright requests
"""
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
import argparse, os, re, sys, time, pickle, json
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from camoufox.sync_api import Camoufox

BASE = "http://www.wytsg.com"
CRED = {"username": _os.environ.get("WYTSG_USER") or "", "password": _os.environ.get("WYTSG_PWD") or ""}
OUT_DIR = _os.path.join(PDFS, r"Wiley Online Library")
SESSION = _os.path.join(FT, r"wiley_session.pkl")


def log(*a):
    print("[*]", *a, flush=True)


def is_challenge(page):
    try:
        c = page.content()
        return ("Just a moment" in c) or ("challenges.cloudflare" in c) or ("turnstile" in c.lower())
    except Exception:
        return True


def solve_a0j(page):
    for _ in range(25):
        try:
            title = page.title() or ""
            if "登录" in title or "无忧" in title:
                if "无忧图书馆登录" in title or "login" in page.url:
                    return True
            if "login" in page.url and len(page.content()) > 3000:
                return True
        except Exception:
            pass
        try:
            page.reload(wait_until="domcontentloaded")
        except Exception:
            pass
        page.wait_for_timeout(1500)
    return "login" in page.url


def wytsg_login(page, ocr):
    page.goto(BASE + "/e/member/login/", timeout=45000, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    solve_a0j(page)
    for attempt in range(8):
        try:
            if page.url.rstrip("/") != BASE + "/e/member/login/":
                page.goto(BASE + "/e/member/login/", wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                solve_a0j(page)
            cap = ocr.classification(page.context.request.get(BASE + "/e/ShowKey/?v=login").body()).strip()
            log(f"login attempt {attempt+1}: captcha={cap!r}")
            page.fill("input[name=username]", CRED["username"])
            page.fill("input[name=password]", CRED["password"])
            page.fill("input[name=key]", cap)
            page.click("input[type=submit][name=ok]")
            page.wait_for_timeout(4000)
            cookies = {c["name"]: c["value"] for c in page.context.cookies()}
            if "ujgpvmlauth" in cookies:
                log("wytsg LOGIN OK")
                return True
            log("login retry...")
        except Exception as e:
            log(f"login err {e}")
            page.wait_for_timeout(2000)
    return False


def grab_session_pickle():
    rs = requests.Session()
    return rs


# 不固定 UA：用 Camoufox 默认指纹解 Turnstile（评分更高、能通过）；解成功后
# 实时抓取浏览器实际 UA 连同 cookie 一起保存，供后续批量复用（cf_clearance 绑定 IP+UA）。
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doi", default="10.1111/ablj.70014")
    ap.add_argument("--direct", action="store_true", help="走本地直连住宅IP(绕过系统代理)")
    args = ap.parse_args()
    DOI = args.doi
    PDF_URL = "https://onlinelibrary.wiley.com/doi/pdf/" + DOI
    os.makedirs(OUT_DIR, exist_ok=True)

    import ddddocr
    ocr = ddddocr.DdddOcr()
    prefs = {"network.proxy.type": 0} if args.direct else {}

    with Camoufox(headless=False, firefox_user_prefs=prefs) as b:
        page = b.new_page()
        log("step 0: wytsg 登录...")
        if not wytsg_login(page, ocr):
            log("WYTSG LOGIN FAILED"); return

        log("step 1: 取 docapi sharedSP token...")
        rs = requests.Session()
        for c in page.context.cookies():
            rs.cookies.set(c["name"], c["value"])
        rs.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        xg = BASE + "/xm/xg/wiley.php?doi=" + DOI
        r = rs.get(xg, headers={"Referer": BASE + "/e/action/ListInfo/?classid=239"}, timeout=20, verify=False)
        m = re.search(r'href=["\']?(https?://[^"\']*docapi[^"\']*)["\']?', r.text)
        docapi_url = m.group(1) if m else None
        if not docapi_url:
            log("  no docapi url; body:", r.text[:200])
            return
        log("  docapi_url:", docapi_url[:120])

        page.goto(docapi_url, timeout=45000, wait_until="domcontentloaded",
                  referer=BASE + "/xm/xg/wiley.php?doi=" + DOI)
        page.wait_for_timeout(4000)
        log("  url:", page.url[:120])
        if "docapi" in page.url:
            if page.query_selector('form[action*="authenticateSharedSP"], form#asder'):
                page.evaluate("() => {const f=document.querySelector('form'); if(f) f.submit();}")
                log("  submitted sharedSP form to Wiley")
            page.wait_for_timeout(6000)
        log("  url:", page.url[:120])

        def on_wiley():
            return "onlinelibrary.wiley.com" in page.url
        if not on_wiley():
            for _ in range(12):
                page.wait_for_timeout(2000)
                if on_wiley():
                    break
        log("  wiley url:", page.url[:140])

        # Wait for human to click Turnstile
        log("=" * 60)
        log(">>> 请在弹出的浏览器窗口勾选 'Verify you are human' <<<")
        log(">>> 勾选后脚本会自动继续并下载 PDF <<<")
        log("=" * 60)
        attempt = 0
        while on_wiley() and is_challenge(page) and attempt < 200:
            if attempt % 10 == 0:
                log(f"  waiting for manual Turnstile click... ({attempt*3}s)")
            page.wait_for_timeout(3000)
            attempt += 1
        if on_wiley() and is_challenge(page):
            log("CHALLENGE NOT CLEARED (超时). 请重跑并尽快勾选。")
            page.screenshot(path=_os.path.join(FT, r"wiley_challenge_fail.png"))
            return
        if not on_wiley():
            log("未到达 Wiley 页面")
            return

        log("CHALLENGE CLEARED! url:", page.url[:140])
        page.wait_for_timeout(6000)
        cookies = {c["name"]: c["value"] for c in page.context.cookies()}
        log("  cookies:", list(cookies.keys()))
        log("  has cf_clearance:", "cf_clearance" in cookies)
        log("  title:", page.title())

        # Download test PDF through the same browser context (carries auth+cookies).
        log("step 2: 下载测试 PDF...")

        def save_pdf(body):
            out = os.path.join(OUT_DIR, DOI.replace("/", "_").replace(".", "_").replace(":", "_") + ".pdf")
            open(out, "wb").write(body)
            log("PDF SAVED:", out, len(body), "bytes")
            return out

        def look_pdf():
            # try direct pdf with Accept application/pdf (content-negotiation), then pdfdirect/epdf
            arms = {"Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8"}
            for u in [
                PDF_URL,
                "https://onlinelibrary.wiley.com/doi/pdfdirect/" + DOI,
                "https://onlinelibrary.wiley.com/doi/epdf/" + DOI,
            ]:
                try:
                    r = page.context.request.get(u, headers=arms)
                    b = r.body()
                    if b[:4] == b"%PDF":
                        return save_pdf(b)
                    log(f"  {u.rsplit('/',1)[-1]} -> status={r.status} len={len(b)} head={b[:4]!r} url={r.url[:100]}")
                except Exception as e:
                    log(f"  {u} err {e}")
            return None

        got = look_pdf()
        if got:
            pass
        else:
            # fallback: real browser navigation to PDF URL; capture native download or pdf content
            log("  trying real browser navigation + expect_download...")
            try:
                with page.expect_download(timeout=30000) as dl:
                    page.goto(PDF_URL, timeout=45000, wait_until="domcontentloaded")
                d = dl.value
                out = os.path.join(OUT_DIR, DOI.replace("/", "_").replace(".", "_") + ".pdf")
                d.save_as(out)
                log("DOWNLOAD via expect_download saved:", out)
            except Exception as e:
                log(f"  expect_download failed: {e}")
                try:
                    page.goto(PDF_URL, timeout=45000, wait_until="domcontentloaded")
                    page.wait_for_timeout(4000)
                    body = page.content().encode("utf-8")
                    if body[:4] == b"%PDF":
                        save_pdf(body)
                    else:
                        log("  final nav not PDF; url:", page.url[:120])
                        page.screenshot(path=_os.path.join(FT, r"wiley_pdf_fail.png"))
                except Exception as e2:
                    log(f"  final nav err {e2}")

        # Persist session for bulk use (保存真实 UA 以匹配 cf_clearance 的 IP+UA 绑定)
        try:
            real_ua = page.evaluate("() => navigator.userAgent")
        except Exception:
            real_ua = ""
        rs2 = requests.Session()
        for c in page.context.cookies():
            rs2.cookies.set(c["name"], c["value"])
        if real_ua:
            rs2.headers["User-Agent"] = real_ua
        with open(SESSION, "wb") as f:
            pickle.dump(rs2, f)
        log("session saved:", SESSION, "ua:", real_ua[:60])
    b.close()


if __name__ == "__main__":
    main()
