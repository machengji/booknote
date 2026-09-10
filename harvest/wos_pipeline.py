# -*- coding: utf-8 -*-
"""WoS 题录：scihuber 半自动采集（先本机跑通，再拷到远程）。

本机用法（推荐）：
  python harvest/wos_pipeline.py --wait-login [YYYY-MM-DD]
    弹出浏览器：账号密码已填，你只填验证码并点登录。
    登录后不要关窗口。脚本会打开 WoS；若弹出人机验证（选图片），你点完即可。
    Cookie 存 BOOKNOTE_DATA/wos/scihuber_session.json（默认 E:\\pubmed\\wos），当天不必再登。

  python harvest/wos_pipeline.py [--headed] [YYYY-MM-DD]
    复用已保存的 VIP cookie，不再登录 scihuber。默认有头，方便点 WoS 验证。

不要加 --auto-login（OCR 刷登录会封号）。远程机器等本机写出 JSONL 再拷。
"""
import sys, os, io, json, time, re, datetime, base64, traceback

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
for _k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
    os.environ.pop(_k, None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if not getattr(sys.stdout, "_wos_wrap", False):
    try:
        sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
        sys.stdout._wos_wrap = True
    except Exception:
        pass


def log(*a):
    print(" ".join(str(x) for x in a), flush=True)


def load_dotenv():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, ".env")
    if not os.path.isfile(path):
        return
    for line in open(path, encoding="utf-8"):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()
USER = os.environ.get("SCIDOWNLOAD_USER") or os.environ.get("SCI_USER") or ""
PWD = os.environ.get("SCIDOWNLOAD_PWD") or os.environ.get("SCI_PWD") or ""
SITE = "https://www.scidownload.com/"
DATA = os.environ.get("BOOKNOTE_DATA", r"E:\pubmed")
OUT_DIR = os.environ.get("WOS_OUT", os.path.join(DATA, "wos"))
RAW = os.path.join(OUT_DIR, "raw_scihuber")
STATE = os.path.join(OUT_DIR, "scihuber_state.json")
LOGIN_BUDGET = os.path.join(OUT_DIR, "scihuber_login_budget.json")
SESSION_FILE = os.path.join(OUT_DIR, "scihuber_session.json")
MAX_LOGINS_PER_DAY = 2
MIN_LOGIN_INTERVAL_SEC = 8 * 3600
SNAP = os.environ.get("BOOKNOTE_LOG", os.path.join(DATA, "logs"))
BATCH = 1000
WOS_ENTRIES = [
    (5954, "WOS(shu)"),
    (3876, "WOS(gw3)"),
    (6119, "WOS(nju)"),
    (6081, "WOS(usst)"),
    (6035, "WOS(cqu)"),
    (4321, "WOS(wy)"),
]
os.makedirs(RAW, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(SNAP, exist_ok=True)
CURRENT_SID = ""


def surl(pg):
    try:
        return pg.url or ""
    except Exception:
        return ""


def scontent(pg):
    try:
        return pg.content() or ""
    except Exception:
        return ""


def snap(pg, name):
    path = os.path.join(SNAP, "scihuber_%s.png" % name)
    try:
        pg.screenshot(path=path, full_page=False)
        log("[snap]", path)
    except Exception as e:
        log("[snap-err]", name, str(e)[:80])


def fix_cap(s):
    return re.sub(r"[^0-9A-Za-z]", "", (s or "").strip()).lower()[:4]


def hint(html):
    m = re.search(r"<b>([^<]+)</b>", html or "")
    return m.group(1).strip() if m else ""


def _load_login_budget():
    try:
        return json.load(open(LOGIN_BUDGET, encoding="utf-8"))
    except Exception:
        return {"day": "", "ok": 0, "tries": 0, "last_ts": 0}


def _save_login_budget(b):
    try:
        json.dump(b, open(LOGIN_BUDGET, "w"), ensure_ascii=False, indent=1)
    except Exception as e:
        log("[budget-save-err]", str(e)[:80])


def allow_login():
    """Refuse extra logins so the card is not banned for 高频登录."""
    today = datetime.date.today().isoformat()
    b = _load_login_budget()
    if b.get("day") != today:
        b = {"day": today, "ok": 0, "tries": 0, "last_ts": b.get("last_ts") or 0}
    now = time.time()
    if b.get("ok", 0) >= MAX_LOGINS_PER_DAY:
        log("[login-blocked] already %d successful logins today (max %d)" % (b["ok"], MAX_LOGINS_PER_DAY))
        return False, b
    if b.get("last_ts") and now - float(b["last_ts"]) < MIN_LOGIN_INTERVAL_SEC:
        wait_h = (MIN_LOGIN_INTERVAL_SEC - (now - float(b["last_ts"]))) / 3600.0
        log("[login-blocked] cooldown %.1fh remaining" % wait_h)
        return False, b
    return True, b


def http_login():
    ok, b = allow_login()
    if not ok:
        return None
    b["tries"] = int(b.get("tries") or 0) + 1
    b["last_ts"] = time.time()
    _save_login_budget(b)
    import requests, urllib3
    urllib3.disable_warnings()
    from ddddocr import DdddOcr
    from PIL import Image
    ocr = DdddOcr(show_ad=False)
    s = requests.Session()
    s.verify = False
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": SITE + "e/member/login/",
    })
    s.get(SITE + "e/member/login/", timeout=25)
    for i in range(3):
        im = s.get(SITE + "e/ShowKey/?v=login&t=%s" % time.time(), timeout=20).content
        try:
            img = Image.open(io.BytesIO(im)).convert("L")
            img = img.resize((max(img.width * 3, 8), max(img.height * 3, 8)), Image.LANCZOS)
            buf = io.BytesIO(); img.save(buf, format="PNG")
            cap = fix_cap(ocr.classification(buf.getvalue()))
        except Exception as e:
            log("[ocr-err]", str(e)[:80])
            cap = ""
        log("[http-ocr]", cap, "bytes", len(im))
        if len(cap) < 4:
            continue
        r = s.post(SITE + "e/member/doaction.php", data={
            "fmdo": "login", "dopost": "login_old", "enews": "login",
            "ecmsfrom": "/", "tobind": "0",
            "username": USER, "password": PWD, "key": cap, "ok": "登 录",
        }, timeout=30, allow_redirects=True)
        msg = hint(r.text)
        log("[http-login]", msg, list(s.cookies.get_dict())[:8])
        if "jtqetmlauth" in s.cookies.get_dict() or "登录成功" in (r.text or ""):
            b["ok"] = int(b.get("ok") or 0) + 1
            b["last_ts"] = time.time()
            _save_login_budget(b)
            return s
        if msg and "验证码" not in msg:
            log("[http-stop]", msg)
            return None
        time.sleep(1)
    return None


def save_pw_cookies(context):
    ck = []
    try:
        for x in context.cookies():
            ck.append({
                "name": x.get("name"), "value": x.get("value"),
                "domain": x.get("domain") or "www.scidownload.com",
                "path": x.get("path") or "/",
            })
    except Exception as e:
        log("[save-ck-err]", str(e)[:80])
        return
    json.dump({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "cookies": ck},
              open(SESSION_FILE, "w"), ensure_ascii=False, indent=1)
    log("[session-saved]", SESSION_FILE, "n", len(ck))


def load_pw_cookies():
    try:
        d = json.load(open(SESSION_FILE, encoding="utf-8"))
        return d.get("cookies") or []
    except Exception:
        return []


def cookies_still_vip(cookies):
    """Check saved cookies without a new login."""
    if not cookies:
        return False
    import requests, urllib3
    urllib3.disable_warnings()
    s = requests.Session()
    s.verify = False
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    for c in cookies:
        try:
            s.cookies.set(c["name"], c["value"], domain=str(c.get("domain") or "www.scidownload.com").lstrip("."),
                          path=c.get("path") or "/")
        except Exception:
            s.cookies.set(c["name"], c["value"])
    try:
        r = s.get(SITE + "e/member/cp/", timeout=25, allow_redirects=True)
    except Exception as e:
        log("[vip-check-err]", str(e)[:80])
        return False
    ok = "高级VIP" in (r.text or "") or ("退出" in (r.text or "") and "login" not in r.url)
    log("[vip-check]", ok, r.url[:80])
    return ok


HUMAN_HINTS = (
    "verify you are human",
    "unusual activity coming from your institution",
    "please verify you are human",
    "select everything mainly made of",
    "are you a robot",
)


def page_text(pg, n=6000):
    try:
        return pg.evaluate("() => (document.body && document.body.innerText) ? document.body.innerText : ''") or ""
    except Exception:
        try:
            return (scontent(pg) or "")[:n]
        except Exception:
            return ""


def has_human_challenge(pg):
    t = (page_text(pg) or "").lower()
    html = ""
    try:
        html = (scontent(pg) or "").lower()
    except Exception:
        html = ""
    blob = t + " " + html[:4000]
    if any(h in blob for h in HUMAN_HINTS):
        return True
    try:
        ifr = pg.evaluate("""() => Array.from(document.querySelectorAll('iframe')).map(
            e => ((e.src||'')+' '+(e.title||'')+' '+(e.id||''))).join(' ')""") or ""
        if re.search(r"arkose|funcaptcha|challenges\.cloudflare", ifr, re.I):
            return True
    except Exception:
        pass
    return False


def wait_human_challenge(pg, timeout_sec=600):
    """Clarivate 人机验证必须人点。有头窗口里等用户做完。"""
    if not has_human_challenge(pg):
        return True
    snap(pg, "human_challenge")
    log("WAIT_HUMAN: WoS 弹出了人机验证（选图片）。请在窗口里点完，点完后不要关窗口。")
    n = max(12, int(timeout_sec / 5))
    for i in range(n):
        time.sleep(5)
        if not has_human_challenge(pg):
            log("[human-ok]", "t", i, surl(pg)[:100])
            time.sleep(2)
            return True
        if i % 6 == 0:
            log("[waiting-human]", i, surl(pg)[:90])
    log("WAIT_HUMAN_TIMEOUT")
    snap(pg, "human_timeout")
    return False


def dismiss_cookies(pg):
    try:
        click_label(pg, r"^Accept all$")
    except Exception:
        pass


def wait_user_login(pg):
    """Headed: 账号密码代填，用户只填验证码。等到会员中心可见。"""
    if not USER or not PWD:
        log("NO_CREDS: 请设置环境变量 SCIDOWNLOAD_USER / SCIDOWNLOAD_PWD，或在仓库根目录写 .env（不要提交）")
        return False
    log("WAIT_LOGIN: 账号密码已填，请只输入验证码并点登录。登录成功后不要关窗口。")
    try:
        pg.goto(SITE + "e/member/login/", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("[wait-login-goto]", str(e)[:80])
    time.sleep(2)
    u0 = surl(pg)
    c0 = scontent(pg)
    if "高级VIP" in c0 or (("退出" in c0 or "会员中心" in c0) and "e/member/login" not in u0):
        log("[already-logged]", u0[:90])
        save_pw_cookies(pg.context)
        return True
    try:
        loc_u = pg.locator("input[name=username]").first
        if loc_u.count():
            loc_u.fill(USER)
        loc_p = pg.locator("input[name=password]").first
        if loc_p.count():
            loc_p.fill(PWD)
        loc_k = pg.locator("input[name=key]").first
        if loc_k.count():
            loc_k.click(timeout=3000)
        log("[prefill] username/password filled, captcha focused")
    except Exception as e:
        log("[prefill-err]", str(e)[:80])
    for i in range(180):
        time.sleep(5)
        u = surl(pg)
        c = scontent(pg)
        if "高级VIP" in c or (("退出" in c or "会员中心" in c) and "e/member/login" not in u):
            log("[user-logged]", u[:90], "t", i)
            save_pw_cookies(pg.context)
            return True
        if i % 6 == 0:
            log("[waiting-login]", i, u[:80])
    log("WAIT_LOGIN_TIMEOUT")
    return False


def inject_cookies(context, sess):
    cookies = []
    for c in sess.cookies:
        domain = (c.domain or "www.scidownload.com").lstrip(".")
        if not domain:
            domain = "www.scidownload.com"
        cookies.append({
            "name": c.name, "value": c.value,
            "domain": domain if domain.startswith(".") or "." in domain else "www.scidownload.com",
            "path": c.path or "/",
            "httpOnly": False, "secure": True,
        })
    # also host-only copies
    extra = []
    for c in cookies:
        extra.append(dict(c, domain="www.scidownload.com"))
        extra.append(dict(c, domain=".scidownload.com"))
    try:
        context.add_cookies(cookies + extra)
        log("[cookies]", len(cookies))
    except Exception as e:
        log("[cookie-err]", str(e)[:120])
        context.add_cookies([{**c, "domain": "www.scidownload.com", "secure": False} for c in cookies])


def inject_cookie_list(context, cookies):
    extra = []
    for c in cookies:
        domain = (c.get("domain") or "www.scidownload.com").lstrip(".")
        if not domain:
            domain = "www.scidownload.com"
        item = {
            "name": c.get("name"), "value": c.get("value"),
            "domain": domain if "." in domain else "www.scidownload.com",
            "path": c.get("path") or "/",
            "httpOnly": False, "secure": False,
        }
        extra.append(item)
        if "scidownload" in domain:
            extra.append(dict(item, domain="www.scidownload.com"))
            extra.append(dict(item, domain=".scidownload.com"))
    try:
        context.add_cookies(extra)
        log("[cookies-file]", len(cookies))
    except Exception as e:
        log("[cookie-file-err]", str(e)[:120])
        sci = [x for x in extra if "scidownload" in (x.get("domain") or "")]
        if sci:
            context.add_cookies(sci)


def install_hooks(pg):
    pg.evaluate("""() => {
        window.__sresp = '';
        window.__reqs = [];
        if (window.__hooked) return;
        window.__hooked = 1;
        const of = window.fetch;
        window.fetch = async (...a) => {
            const r = await of(...a);
            try {
                const u = (a[0] && a[0].url) ? a[0].url : String(a[0] || '');
                window.__reqs.push({u: String(u).slice(0, 180), st: r.status});
                if (/runQuerySearch|saveToFile|wosnx|QuerySearch/i.test(u)) {
                    window.__sresp = await r.clone().text();
                    window.__slast = String(u).slice(0, 180);
                }
            } catch (e) {}
            return r;
        };
        const oo = XMLHttpRequest.prototype.open;
        const osend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function(m, u) { this.__u = u; return oo.apply(this, arguments); };
        XMLHttpRequest.prototype.send = function() {
            const xhr = this;
            xhr.addEventListener('load', function() {
                try {
                    const u = String(xhr.__u || '');
                    if (/runQuerySearch|saveToFile|wosnx/i.test(u)) {
                        window.__sresp = xhr.responseText;
                        window.__slast = u.slice(0, 180);
                    }
                } catch (e) {}
            });
            return osend.apply(this, arguments);
        };
    }""")


def is_wos_ui(u, c=""):
    """True only when Smart/Advanced Search UI is actually there. Init=Yes is not ready."""
    low_u = (u or "").lower()
    low_c = (c or "")[:8000].lower()
    if "access.clarivate.com/login" in low_u:
        return False
    if "sign in to continue" in low_c and "smart search" not in low_c:
        return False
    if "select institution" in low_c and "smart search" not in low_c:
        return False
    if any(k in low_u for k in ["smart-search", "advanced-search", "basic-search"]):
        return True
    if "composequerysmartsearch" in low_c or "advancedsearchinputarea" in low_c:
        return True
    if "search documents, researchers" in low_c or "your trusted path to discovery" in low_c:
        return True
    return False


def is_wos_session(u, c=""):
    low_u = (u or "").lower()
    if "access.clarivate.com/login" in low_u:
        return False
    if "webofscience" in low_u or "webofknowledge" in low_u or "clarivate.cn" in low_u:
        return True
    return False


def is_wos_proxy(u):
    low = (u or "").lower()
    if "access.clarivate.com/login" in low:
        return False
    return any(k in low for k in (
        "scihub.top", "shutong", "wenxian2", "downsci.top", "lunwen.one",
    ))


def is_list_page(u):
    low = (u or "").lower()
    return "scidownload.com" in low and "showinfo" not in low and "webofscience" not in low


def dead_reason(pg):
    """Gateway is down / bounced to Clarivate personal login / Firefox neterror."""
    u = (surl(pg) or "")
    if is_list_page(u):
        return ""
    t = ""
    try:
        t = page_text(pg) or ""
    except Exception:
        t = ""
    html = ""
    try:
        html = scontent(pg) or ""
    except Exception:
        html = ""
    blob = (t + " " + html[:2500])
    low = blob.lower()
    if "入口维护" in blob or "入口暂停" in blob or "正在维护" in blob:
        return "maint"
    if "corrupted content error" in low or "problem loading page" in low:
        return "corrupt"
    if "access.clarivate.com/login" in u.lower():
        return "clarivate-login"
    return ""


def grab_sid(pg):
    global CURRENT_SID
    u = surl(pg) or ""
    m = re.search(r"SID=([A-Za-z0-9]{12,48})", u)
    if m:
        CURRENT_SID = m.group(1)
        return CURRENT_SID
    try:
        sid = pg.evaluate("""() => {
            const c = document.cookie || '';
            const m = c.match(/(?:WOSSID|SID)=([A-Za-z0-9]{12,48})/);
            if (m) return m[1];
            for (const k of Object.keys(localStorage||{})) {
                const v = String(localStorage.getItem(k)||'');
                const m2 = v.match(/SID=([A-Za-z0-9]{12,48})/);
                if (m2) return m2[1];
            }
            return '';
        }""") or ""
        if sid:
            CURRENT_SID = sid
    except Exception:
        pass
    return CURRENT_SID


def close_junk_tabs(context, keep=None):
    """Drop leftover Clarivate login / neterror tabs. Never close a live WoS tab."""
    for p in list(context.pages):
        if keep is not None and p is keep:
            continue
        u = surl(p)
        why = dead_reason(p)
        if not why:
            continue
        if is_wos_ui(u) or is_wos_session(u):
            continue
        try:
            p.close()
            log("[close-junk]", why, u[:80])
        except Exception:
            pass


def is_wos_ready(u, c=""):
    return is_wos_ui(u, c)


def _pick_wos_page(pages, name, i):
    """Pick the best WoS tab. Never close pages here (that used to kill the WoS tab)."""
    best, best_score = None, -1
    seen = set()
    for p in pages:
        try:
            if id(p) in seen:
                continue
            seen.add(id(p))
        except Exception:
            pass
        u = surl(p)
        why = dead_reason(p)
        c = ""
        try:
            c = scontent(p)
        except Exception:
            c = ""
        score = 0
        if why:
            score = -1
        elif is_wos_ui(u, c):
            score = 100
        elif has_human_challenge(p):
            score = 90
        elif is_wos_session(u, c):
            score = 50
        elif is_wos_proxy(u):
            score = 40
        log("[wait %s %d]" % (name, i), "s=%d" % score, (why or ""), u[:110])
        if score > best_score:
            best, best_score = p, score
    return best, best_score


def open_entry(pg, eid, name):
    """Click the list-page link (user gesture) and wait for Smart Search UI."""
    holder = {"page": None}

    def on_page(p):
        holder["page"] = p
    try:
        pg.context.on("page", on_page)
    except Exception:
        pass
    close_junk_tabs(pg.context, keep=pg)
    list_url = SITE + "e/action/ListInfo/?classid=186"
    log("[open]", name, eid)
    try:
        pg.goto(list_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("[list-err]", str(e)[:80])
    time.sleep(4)
    clicked = False
    try:
        loc = pg.locator("a[href*='id=%d']" % eid).first
        if loc.count():
            loc.click(timeout=8000)
            clicked = True
            log("[clicked-list]", name)
    except Exception as e:
        log("[click-err]", str(e)[:80])
    if not clicked:
        try:
            pg.goto(SITE + "e/action/ShowInfo.php?classid=186&id=%d" % eid,
                    wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            log("[showinfo-err]", str(e)[:80])
    sess_page = None
    dead_hits = 0
    for i in range(24):
        time.sleep(4)
        pages = []
        try:
            pages = list(pg.context.pages)
        except Exception:
            pages = [pg]
        if holder["page"] is not None:
            pages.append(holder["page"])
        best, score = _pick_wos_page(pages, name, i)
        why = ""
        why_url = ""
        for p in pages:
            r = dead_reason(p)
            if r:
                why, why_url = r, surl(p)
                break
        if score >= 40:
            dead_hits = 0
        elif why:
            dead_hits += 1
            log("[dead-gw]", name, why, dead_hits, why_url[:100])
            if dead_hits >= 2:
                log("[skip-entry]", name, why)
                snap(pg, "dead_%s" % eid)
                return None
        else:
            dead_hits = 0
        if best is None:
            continue
        if score >= 90:
            grab_sid(best)
            log("[landed-ui]", name, surl(best)[:140], "sid", CURRENT_SID[:18])
            wait_human_challenge(best)
            dismiss_cookies(best)
            return best
        if score >= 50:
            sess_page = best
            if i == 0 or i % 5 == 4:
                log("[init-wait]", surl(best)[:120])
        elif score >= 40:
            log("[proxy-wait]", surl(best)[:120])
            if i >= 5:
                log("[skip-entry]", name, "proxy-stuck")
                return None
    if sess_page is not None:
        grab_sid(sess_page)
        log("[landed-session-only]", name, surl(sess_page)[:140], "sid", CURRENT_SID[:18])
        wait_human_challenge(sess_page)
        time.sleep(8)
        if is_wos_ui(surl(sess_page), scontent(sess_page)) or has_human_challenge(sess_page):
            return sess_page
        return sess_page
    snap(pg, "noload_%s" % eid)
    log("[no-land]", name, surl(pg)[:130])
    return None


def find_search_box(pg):
    return pg.evaluate("""() => {
        const vis = (e) => !!(e.offsetParent || (e.getClientRects && e.getClientRects().length));
        const cands = [];
        document.querySelectorAll('textarea, input[type=text], input[type=search], input:not([type]), [contenteditable=true], [role=searchbox]').forEach(e => {
            if (!vis(e)) return;
            const r = e.getBoundingClientRect();
            if (r.width < 120 || r.height < 12) return;
            cands.push({id: e.id||'', ph:(e.placeholder||'').slice(0,80), tag:e.tagName,
                        w:Math.round(r.width), h:Math.round(r.height),
                        x:r.x+r.width/2, y:r.y+r.height/2});
        });
        cands.sort((a,b)=>b.w-a.w);
        return cands.slice(0,8);
    }""")


def parse_qid(body):
    if not body:
        return None, None
    if "passiveVerificationRequired" in body:
        return None, "passiveVerificationRequired"
    if "sessionNotFound" in body:
        return None, "sessionNotFound"
    m2 = re.search(r'"QueryID"\s*:\s*"([^"]+)"', body)
    m3 = re.search(r'"RecordsFound"\s*:\s*(\d+)', body)
    qid = m2.group(1) if m2 else None
    total = int(m3.group(1)) if m3 else None
    if "CountryChange" in body or "Server.authorization" in body:
        return None, body[:400]
    return (qid, total) if qid else (None, total)


def click_label(pg, pattern):
    try:
        hit = pg.evaluate("""(pat) => {
        const re = new RegExp(pat, 'i');
        const els = Array.from(document.querySelectorAll('button, a, span, div'));
        const a = els.find(e => re.test((e.innerText||'').trim()) && (e.offsetParent || e.getClientRects().length)
            && (e.innerText||'').trim().length < 40);
        if (!a) return null;
        const r = a.getBoundingClientRect();
        return {x:r.x+r.width/2, y:r.y+r.height/2, t:(a.innerText||'').trim().slice(0,40)};
    }""", pattern)
    except Exception as e:
        log("[click-label-err]", str(e)[:80])
        return False
    if hit:
        pg.mouse.click(hit["x"], hit["y"])
        log("[click]", hit)
        return True
    return False


def submit_search(pg):
    """Click the magnifier on the Smart Search box. Never hit Research Assistant."""
    btn = pg.evaluate("""() => {
        const vis = (e) => !!(e.offsetParent || (e.getClientRects && e.getClientRects().length));
        const bad = (e) => /question|assistant|submit your|register|sign in|accept|explore a topic|get started/i.test(
            ((e.innerText||'')+' '+(e.getAttribute('aria-label')||'')+' '+(e.getAttribute('title')||'')+' '+(e.id||'')));
        const inp = document.querySelector(
            '#composeQuerySmartSearch, #advancedSearchInputArea, input[placeholder*="Search documents"]');
        if (inp) {
            let n = inp.nextElementSibling;
            for (let i = 0; i < 6 && n; i++, n = n.nextElementSibling) {
                if (!vis(n) || bad(n)) continue;
                const r = n.getBoundingClientRect();
                if (r.width > 8 && r.height > 8)
                    return {x:r.x+r.width/2, y:r.y+r.height/2, t:'next-sib'};
            }
            const box = inp.parentElement;
            if (box) {
                const hits = Array.from(box.querySelectorAll('button, [role=button], svg')).filter(e => vis(e) && !bad(e));
                if (hits.length) {
                    const e = hits[hits.length-1];
                    const r = e.getBoundingClientRect();
                    return {x:r.x+r.width/2, y:r.y+r.height/2, t:'box-icon'};
                }
            }
            const r = inp.getBoundingClientRect();
            return {x: r.right - 18, y: r.y + r.height/2, t:'input-right'};
        }
        const els = Array.from(document.querySelectorAll('button')).filter(e =>
            vis(e) && /^(search|检索)$/i.test((e.innerText||'').trim()) && !bad(e));
        if (!els.length) return null;
        const r = els[0].getBoundingClientRect();
        return {x:r.x+r.width/2, y:r.y+r.height/2, t:(els[0].innerText||'').slice(0,20)};
    }""")
    log("[search-btn]", btn)
    if btn:
        pg.mouse.click(btn["x"], btn["y"])
        return True
    pg.keyboard.press("Enter")
    return False


_QID_HOOKED = set()


def attach_qid_listener(pg, box):
    k = id(pg)
    if k in _QID_HOOKED:
        return

    def on_resp(r):
        try:
            u = r.url or ""
            if "runQuerySearch" not in u and "saveToFile" not in u:
                return
            t = r.text()
            box["body"] = t
            qid, total = parse_qid(t)
            if qid:
                box["qid"] = qid
                box["total"] = total
            elif isinstance(total, str):
                box["err"] = total
            log("[net]", r.status, u[:90], "qid", qid, "total", str(total)[:80])
        except Exception:
            pass
    try:
        pg.on("response", on_resp)
        _QID_HOOKED.add(k)
    except Exception as e:
        log("[qid-hook-err]", str(e)[:80])


def wait_qid(pg, n=16, box=None):
    qid = total = None
    for i in range(n):
        time.sleep(5)
        if has_human_challenge(pg):
            log("[poll %d] human challenge during search" % i)
            if not wait_human_challenge(pg):
                return None, None
            continue
        body = ""
        if box and box.get("qid") and isinstance(box.get("total"), int):
            log("[poll %d]" % i, "from-net", box.get("qid"), box.get("total"))
            return box["qid"], box["total"]
        try:
            body = pg.evaluate("() => (window.__sresp || '')") or ""
        except Exception:
            body = ""
        if not body and box:
            body = box.get("body") or ""
        qid, total = parse_qid(body)
        u = surl(pg)
        log("[poll %d]" % i, u[:110], "qid", qid, "total", total, re.sub(r"\s+", " ", body)[:200])
        if qid and isinstance(total, int):
            return qid, total
        if total == "passiveVerificationRequired":
            log("[need-human] API still wants verification")
            wait_human_challenge(pg, timeout_sec=300)
            continue
        if isinstance(total, str) and ("CountryChange" in total or "authorization" in total):
            log("[api-denied]", total[:300])
            snap(pg, "denied")
            return None, None
        if "/summary/" in (u or "") or "/woscc/summary" in (u or ""):
            if qid:
                return qid, total if isinstance(total, int) else None
    return qid, total if isinstance(total, int) else None


def api_search(pg, day):
    """runQuerySearch on the current origin, SID from land URL / cookie."""
    install_hooks(pg)
    sid0 = CURRENT_SID
    try:
        r = pg.evaluate("""async (arg) => {
            const day = arg.day, sid0 = arg.sid0;
            const sid = (location.href.match(/SID=([A-Za-z0-9]{12,48})/)||[])[1]
                || (document.cookie.match(/(?:WOSSID|SID)=([A-Za-z0-9]{12,48})/)||[])[1]
                || sid0 || '';
            const cn = location.hostname.indexOf('clarivate.cn') >= 0;
            const product = cn ? 'ALLDB' : 'WOSCC';
            const dop = day + '/' + day;
            const body = {"product":"WOSCC","searchMode":"general","viewType":"search","serviceMode":"summary",
              "search":{"mode":"general","database":"WOSCC","query":
                [{"rowField":"DOP","rowText":dop},{"rowBoolean":"AND","rowField":"AU","rowText":""}]},
              "retrieve":{"count":20,"sort":"relevance","history":true,"jcr":true,
                "analyzes":["TP.Value.6","REVIEW.Value.6","EARLY ACCESS.Value.6","OA.Value.6","DR.Value.6",
                  "ECR.Value.6","PY.Field_D.6","FPY.Field_D.6","DT.Value.6","AU.Value.6","DX2NG.Value.6",
                  "PEERREVIEW.Value.6","STK.Value.10"],"locale":"en"},"eventMode":null};
            const url = location.origin + '/api/wosnx/core/runQuerySearch' + (sid ? ('?SID='+sid) : '');
            const rr = await fetch(url, {method:'POST',
              headers:{'Content-Type':'text/plain;charset=UTF-8','Accept':'application/x-ndjson',
                       'x-1p-wos-sid': sid},
              body: JSON.stringify(body)});
            return {code: rr.status, text: (await rr.text()).slice(0, 8000), url, sid};
        }""", {"day": day, "sid0": sid0})
    except Exception as e:
        log("[api-search-err]", str(e)[:120])
        return None, None
    log("[api-search]", r.get("code"), "sid", str(r.get("sid"))[:18], str(r.get("text"))[:350])
    qid, total = parse_qid(r.get("text") or "")
    if qid and isinstance(total, int):
        return qid, total
    return None, total


def goto_advanced(pg):
    """Click the in-page Advanced Search tab. Do not goto /advanced-search (CountryChange)."""
    if "advanced-search" in (surl(pg) or "").lower():
        return True
    hit = pg.evaluate("""() => {
        const vis = (e) => !!(e.offsetParent || (e.getClientRects && e.getClientRects().length));
        const nodes = Array.from(document.querySelectorAll('a, button, [role=tab], span, div'));
        const a = nodes.find(e => vis(e) && /^advanced search$/i.test((e.innerText||'').trim())
            && (e.innerText||'').trim().length < 24);
        if (!a) {
            const href = nodes.find(e => vis(e) && /advanced-search/i.test(e.getAttribute('href')||''));
            if (!href) return null;
            const r = href.getBoundingClientRect();
            return {x:r.x+r.width/2, y:r.y+r.height/2, t:'href'};
        }
        const r = a.getBoundingClientRect();
        return {x:r.x+r.width/2, y:r.y+r.height/2, t:(a.innerText||'').trim()};
    }""")
    log("[adv-hit]", hit)
    if hit:
        pg.mouse.click(hit["x"], hit["y"])
    time.sleep(4)
    for i in range(12):
        try:
            if pg.locator("#advancedSearchInputArea").count():
                log("[adv-ui]", surl(pg)[:100])
                return True
        except Exception:
            pass
        if "advanced-search" in (surl(pg) or "").lower():
            log("[adv-url]", surl(pg)[:100])
            return True
        time.sleep(1)
    log("[adv-miss]", surl(pg)[:100])
    return False


def fill_query(pg, day, advanced=False):
    q = "DOP=%s/%s" % (day, day)
    filled = False
    sels = ["#advancedSearchInputArea", "textarea"] if advanced else [
        "#advancedSearchInputArea", "#composeQuerySmartSearch",
        "input[placeholder*='Search documents']", "textarea"]
    for sel in sels:
        try:
            loc = pg.locator(sel).first
            if loc.count():
                loc.click(timeout=5000)
                loc.fill(q)
                filled = True
                log("[filled]", sel)
                break
        except Exception as e:
            log("[fill-err]", sel, str(e)[:60])
    if filled:
        return True
    boxes = find_search_box(pg)
    log("[boxes]", json.dumps(boxes, ensure_ascii=False)[:1000])
    if not boxes:
        return False
    box = boxes[0]
    pg.mouse.click(box["x"], box["y"])
    time.sleep(0.3)
    try:
        pg.keyboard.press("Control+A")
        pg.keyboard.press("Backspace")
    except Exception:
        pass
    pg.keyboard.type(q, delay=20)
    return True


def smart_search(pg, day):
    """Java WosExportDownloader path: page-origin runQuerySearch, not Topic UI."""
    box = {}
    attach_qid_listener(pg, box)
    install_hooks(pg)
    grab_sid(pg)
    wait_human_challenge(pg)
    dismiss_cookies(pg)
    snap(pg, "after_land")
    qid, total = api_search(pg, day)
    if qid and isinstance(total, int) and total > 0:
        return qid, total
    if total == "passiveVerificationRequired" or has_human_challenge(pg):
        wait_human_challenge(pg)
        qid, total = api_search(pg, day)
        if qid and isinstance(total, int) and total > 0:
            return qid, total
    snap(pg, "after_search")
    return qid, total if isinstance(total, int) else None


def page_export(pg, qid, a, b, raw):
    grab_sid(pg)
    sid = CURRENT_SID or ""
    body = json.dumps({
        "parentQid": qid, "sortBy": "recently-added", "displayTimesCited": "true",
        "displayCitedRefs": "true", "product": "UA", "colName": "WOS",
        "displayUsageInfo": "true", "fileOpt": "xls", "action": "saveToExcel",
        "markFrom": str(a), "markTo": str(b), "view": "summary",
        "isRefQuery": "false", "locale": "zh_CN", "filters": "fullRecord"
    }, separators=(",", ":"))
    try:
        r = pg.evaluate("""async (payload) => {
            const sid = payload.sid || '';
            const url = location.origin + '/api/wosnx/indic/export/saveToFile';
            const headerSets = [
              {'Content-Type':'text/plain;charset=UTF-8','Accept':'*/*','x-1p-wos-sid':sid},
              {'Content-Type':'application/x-www-form-urlencoded','Accept':'*/*','x-1p-wos-sid':sid},
              {'Content-Type':'application/json','Accept':'*/*','x-1p-wos-sid':sid}
            ];
            let last = {code: 0, b64:'', head:''};
            for (const headers of headerSets) {
              const rr = await fetch(url, {method:'POST', headers, body: payload.body});
              const head = (await rr.clone().text()).slice(0, 220);
              last = {code: rr.status, b64:'', head, ctype: headers['Content-Type']};
              if (rr.status !== 200) continue;
              const buf = await rr.arrayBuffer();
              let bin = '';
              const bytes = new Uint8Array(buf);
              for (let i = 0; i < bytes.length; i += 8192)
                bin += String.fromCharCode.apply(null, bytes.subarray(i, i+8192));
              return {code: rr.status, b64: btoa(bin), head:'', ctype: headers['Content-Type']};
            }
            return last;
        }""", {"body": body, "sid": sid})
        if r.get("code") == 200 and r.get("b64"):
            data = base64.b64decode(r["b64"])
            open(raw, "wb").write(data)
            log("[export %d-%d] %dKB %r" % (a, b, len(data) // 1024, data[:6]))
            return True
        log("[export fail]", r.get("code"), str(r.get("head") or "")[:180])
        return False
    except Exception as e:
        log("[export-err]", str(e)[:130])
        return False


def ingest(raw, out_path):
    from wos_xls import parse_xls, COLUMNS
    data = open(raw, "rb").read()
    hdr, rows = parse_xls(data)
    log("[xls] hdr", hdr[:6], "rows", len(rows))
    # Java WosExportDownloader maps by column index, not Excel header text
    # (headers are "UT (Unique WOS ID)" etc., keys are ut_unique_wos_id).
    ut = set()
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            for line in f:
                try:
                    v = json.loads(line).get("ut_unique_wos_id")
                    if v:
                        ut.add(v)
                except Exception:
                    pass
    added = 0
    with open(out_path, "a", encoding="utf-8") as f:
        for row in rows:
            rec = {}
            for i, h in enumerate(COLUMNS):
                if i >= len(row):
                    break
                v = row[i]
                if isinstance(v, float) and v == int(v):
                    v = int(v)
                if v not in (None, ""):
                    rec[h] = v
            utk = rec.get("ut_unique_wos_id", "")
            if not utk or utk in ut:
                continue
            ut.add(utk)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            added += 1
    return added, len(rows)


def harvest(pg, day):
    u = surl(pg)
    if "access.clarivate.com/login" in (u or "").lower() and "webofscience" not in (u or "").lower():
        log("[harvest-abort] still on login wall", u[:120])
        return 2
    wait_human_challenge(pg)
    qid, total = smart_search(pg, day)
    if not qid:
        return 2
    total = int(total or 0)
    log("[qid]", qid, "total", total)
    if total <= 0:
        log("[no-records] query returned 0 hits, skip export")
        snap(pg, "zero_hits")
        return 2
    out_path = os.path.join(OUT_DIR, "wos_%s.jsonl" % day[:7])
    new_all = 0
    batch = 1
    cap = total
    max_b = int(os.environ.get("WOS_MAX_BATCH", "0") or 0)
    while (batch - 1) * BATCH < cap:
        if max_b and batch > max_b:
            log("[max-batch stop]", max_b)
            break
        a = (batch - 1) * BATCH + 1
        b = min(batch * BATCH, cap)
        raw = os.path.join(RAW, "wos_%s_%d.xls" % (day, batch))
        exported = False
        for attempt in range(3):
            if page_export(pg, qid, a, b, raw):
                exported = True
                break
            log("[export-retry]", attempt + 1, "sleep 60s then re-search")
            time.sleep(60)
            qid2, total2 = api_search(pg, day)
            if qid2 and isinstance(total2, int) and total2 > 0:
                qid, total, cap = qid2, int(total2), int(total2)
                log("[qid-refresh]", qid, "total", total)
                b = min(batch * BATCH, cap)
        if not exported:
            return 4
        try:
            added, nrows = ingest(raw, out_path)
            new_all += added
            log("[parse b%d] rows=%d new=%d cum=%d" % (batch, nrows, added, new_all))
            if nrows < BATCH:
                break
        except Exception as e:
            log("[parse-err]", str(e)[:200])
            traceback.print_exc()
            return 5
        batch += 1
        time.sleep(6)
    complete = not (max_b and batch > max_b)
    st = {"last_qid": qid, "last_total": total, "last_new": new_all,
          "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "source": "scihuber"}
    if complete:
        st["last_done"] = day
    else:
        st["last_partial"] = day
        log("[partial] not marking last_done (once/max-batch)")
    json.dump(st, open(STATE, "w"), ensure_ascii=False, indent=1)
    log("[WINDOW_DONE]", day, "new", new_all, "complete", complete)
    return 0


def dates_todo():
    st = {}
    if os.path.exists(STATE):
        try:
            st = json.load(open(STATE, encoding="utf-8"))
        except Exception:
            pass
    today = datetime.date.today()
    end = today - datetime.timedelta(days=1)
    last = st.get("last_done", "")
    if last:
        try:
            start = datetime.date.fromisoformat(last) + datetime.timedelta(days=1)
        except Exception:
            start = end
    else:
        start = end
    out = []
    d = start
    while d <= end:
        out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out or [end.isoformat()]


def harvest_from_page(pg, days):
    try:
        pg.goto(SITE + "e/member/cp/", wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        log("[cp]", surl(pg)[:90], "vip" if "高级VIP" in scontent(pg) else "")
    except Exception as e:
        log("[cp-err]", str(e)[:80])
    if "高级VIP" not in scontent(pg) and "e/member/login" in surl(pg):
        log("FAIL_NOT_LOGGED")
        return 2
    save_pw_cookies(pg.context)
    app = None
    used = None
    for eid, name in WOS_ENTRIES:
        app = open_entry(pg, eid, name)
        if app is not None:
            used = name
            break
    if app is None:
        log("FAIL_LAND")
        return 3
    log("[using]", used)
    save_pw_cookies(app.context)
    wait_human_challenge(app)
    rc = 0
    for day in days:
        r = harvest(app, day)
        if r != 0:
            rc = r
            break
    return rc


def run_days(days, wait_login=False, auto_login=False, headed=True, login_only=False):
    from camoufox.sync_api import Camoufox
    saved = load_pw_cookies()
    vip = bool(saved and cookies_still_vip(saved))
    if vip:
        log("[reuse-session] cookie still VIP, skip scihuber login")
        need_user = False
    elif wait_login:
        need_user = True
    elif auto_login:
        need_user = False
    else:
        log("NO_SESSION: 请先本机运行  harvest/wos_pipeline.py --wait-login")
        log("（会弹出浏览器，账号密码已填，你只填验证码。远程先不要跑。）")
        return 2
    log("[browser]", "headed" if headed else "headless")
    with Camoufox(headless=not headed, exclude_addons=["ublock-origin"]) as b:
        pg = b.new_page()
        pg.set_default_timeout(90000)
        pg.on("dialog", lambda d: d.accept())
        if need_user:
            if not wait_user_login(pg):
                log("FAIL_LOGIN")
                return 2
        elif saved:
            inject_cookie_list(pg.context, saved)
        elif auto_login:
            log("[warn] --auto-login 会消耗登录次数，站点可能封号")
            sess = http_login()
            if not sess:
                log("FAIL_LOGIN")
                return 2
            inject_cookies(pg.context, sess)
        if login_only:
            save_pw_cookies(pg.context)
            log("[login-only] session saved, not harvesting")
            return 0
        return harvest_from_page(pg, days)


def main():
    args = sys.argv[1:]
    wait_login = "--wait-login" in args
    auto_login = "--auto-login" in args
    login_only = "--login-only" in args
    headless = "--headless" in args
    headed = (not headless) or wait_login or "--headed" in args
    if "--once" in args:
        os.environ["WOS_MAX_BATCH"] = "1"
    rest = [a for a in args if not a.startswith("--")]
    arg = rest[0] if rest else "daily"
    if re.match(r"\d{4}-\d{2}-\d{2}$", arg):
        days = [arg]
    else:
        days = dates_todo()
    log("=== scihuber_wos start", time.strftime("%Y-%m-%d %H:%M:%S"),
        "days", days, "wait_login", wait_login, "auto_login", auto_login,
        "headed", headed, "login_only", login_only, "===")
    rc = run_days(days, wait_login=wait_login, auto_login=auto_login,
                  headed=headed, login_only=login_only)
    log("DONE" if rc == 0 else "FAILED rc=%d" % rc)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        log("FAILED exception")
        sys.exit(3)
