# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# 知献网关通用会话构建器：
#   用法: python zx_gateway_camou.py <classid> <id> <session_out.pkl> <test_pdf_url>
#   入口页两种模式:
#     jump: window.location.href=URL (token 在 URL 里) -> 浏览器直接 goto
#     form: <form id=asder> 自动提交 -> route 注入解码后的 HTML 再提交
import sys, re, time, pickle, requests, urllib3
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from camoufox.sync_api import Camoufox

ZX_BASE = "http://lib.zxsju.com"
CHAIN = "http://127.0.0.1:10889"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

CID, IID, OUT_PKL, TEST_URL = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
ENTRY = f"{ZX_BASE}/e/action/ShowInfo.php?classid={CID}&id={IID}"

def decode(t):
    cands = re.findall(r"'([0-9A-Za-z\n\r+]{500,})'", t)
    if not cands:
        return None
    raw = max(cands, key=len); out = []; num = ''
    for ch in raw:
        if ch.isdigit(): num += ch
        else:
            if num: out.append(chr(int(num))); num=''
    if num: out.append(chr(int(num)))
    return ''.join(out)

with open("_zx_login.pkl", "rb") as f:
    zx = pickle.load(f)
zx.headers["User-Agent"] = UA
r = zx.get(ENTRY, timeout=25, headers={"Referer": ZX_BASE + "/e/action/ListInfo/?classid=63"})
t = r.text

jump = re.search(r'window\.location\.href="([^"]+)"', t)
html_dec = decode(t)
if html_dec and "<form" in html_dec:
    MODE = "form"; PAGE = html_dec
elif "<form" in t and "asder" in t:
    MODE = "form"; PAGE = t
elif jump:
    MODE = "jump"; PAGE = jump.group(1)
else:
    print("UNKNOWN ENTRY MODE len", len(t)); print(t[:300]); sys.exit(1)
print("MODE:", MODE, flush=True)

def _content(pg):
    try:
        return pg.content() or ""
    except Exception:
        return ""

target_domain = None
if MODE == "form":
    m = re.search(r'action=["\']?\s*(https?://[^"\'\s]+)', PAGE)
    target_domain = re.sub(r'^https?://(www\.)?', '', m.group(1)).split('/')[0] if m else None

cookies = None; ua = None
with Camoufox(headless=False, proxy={"server": CHAIN}) as b:
    pg = b.new_page()
    if MODE == "form":
        def _fake_entry(route):
            route.fulfill(status=200, content_type="text/html", body=PAGE)
        pg.route(ENTRY, _fake_entry)
        pg.goto(ENTRY, timeout=45000, wait_until="domcontentloaded",
                referer=ZX_BASE + "/e/action/ListInfo/?classid=63")
        time.sleep(2)
        try:
            pg.evaluate("()=>{const f=document.getElementById('asder')||document.querySelector('form'); if(f) f.submit();}")
        except Exception:
            pass
    else:
        pg.goto(PAGE, timeout=45000, wait_until="domcontentloaded",
                referer=ZX_BASE + "/e/action/ListInfo/?classid=63")
    landed = False
    for i in range(40):
        time.sleep(3)
        u = pg.url or ""
        names = sorted({c["name"] for c in pg.context.cookies()})
        dom_hit = (target_domain and target_domain in u) or (target_domain is None)
        if target_domain and target_domain in u:
            print(f"  landed url={u[:90]} ck={names[:10]}", flush=True)
            landed = True
            for j in range(50):
                time.sleep(3)
                c = _content(pg)
                cur = {c["name"] for c in pg.context.cookies()}
                if "just a moment" not in c.lower() and len(c) > 3000 and cur:
                    print(f"  >>> CF pass t={j*3}s url={(pg.url or '')[:80]}", flush=True)
                    break
            break
        if MODE == "jump" and i == 0:
            landed = True  # jump 模式第一跳即落地目标
            print(f"  jump landed url={u[:90]}", flush=True)
            for j in range(50):
                time.sleep(3)
                c = _content(pg)
                cur = {c["name"] for c in pg.context.cookies()}
                if "just a moment" not in c.lower() and len(c) > 3000 and cur:
                    print(f"  >>> CF pass t={j*3}s url={(pg.url or '')[:80]}", flush=True)
                    break
            break
        if i % 3 == 0:
            print(f"  t={(i+1)*3}s url={u[:75]}", flush=True)
    if not landed:
        print("NOT landed", flush=True)
        b.close(); sys.exit(1)
    time.sleep(2)
    cookies = [{"name": c["name"], "value": c["value"], "domain": c.get("domain", ""), "path": c.get("path", "/")} for c in pg.context.cookies()]
    ua = pg.evaluate("()=>navigator.userAgent")
    b.close()

names = [c["name"] for c in cookies]
print("final cookies:", len(cookies), names, flush=True)
if names:
    pickle.dump({"cookies": cookies, "ua": ua}, open(OUT_PKL, "wb"))
    print("SESSION SAVED", OUT_PKL, flush=True)
else:
    print("NO cookies", flush=True); sys.exit(1)

# 验证
jar = requests.cookies.RequestsCookieJar()
for c in cookies:
    for dom in (c.get("domain") or "", ".wiley.com", ".tandfonline.com", ".sagepub.com", ".iopscience.iop.org"):
        try:
            jar.set(c["name"], c["value"], domain=dom, path=(c["path"] or "/"))
        except Exception:
            pass
try:
    rr = requests.get(TEST_URL, headers={"User-Agent": ua, "Accept": "application/pdf"},
                      cookies=jar, proxies={"http": CHAIN, "https": CHAIN}, timeout=60, verify=False)
    b = rr.content
    print(f"TEST st={rr.status_code} len={len(b)} pdf@={b.find(b'%PDF')}", flush=True)
except Exception as e:
    print("TEST ERR", str(e)[:60], flush=True)