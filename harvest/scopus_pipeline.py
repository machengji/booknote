# -*- coding: utf-8 -*-
"""Scopus 题录：scihuber 入口铸会话 → Advanced Search LOAD-DATE → 导出 JSONL。
对照 WoS Java：先检索拿结果，再按页导出。不自动登录 scihuber。
用法: python harvest/scopus_pipeline.py [--headed] [YYYY-MM-DD]
"""
import sys, os, json, time, re, datetime, traceback

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
for _k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
    os.environ.pop(_k, None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wos_pipeline import (
    load_pw_cookies, cookies_still_vip, inject_cookie_list, SITE,
    surl, scontent, snap, SESSION_FILE, USER, PWD, save_pw_cookies,
)


def log(*a):
    print(" ".join(str(x) for x in a), flush=True)


ANTI_DEBUG = r"""
(() => {
  try {
    const _si = window.setInterval.bind(window);
    window.setInterval = function(fn, t) {
      try {
        const s = Function.prototype.toString.call(fn);
        if (t === 50 && s.indexOf('debugger') >= 0) return 0;
      } catch (e) {}
      return _si(fn, t);
    };
    window.close = function() {};
    window.addEventListener('resize', ev => ev.stopImmediatePropagation(), true);
    const _open = window.open.bind(window);
    window.open = function(url, name, spec) {
      const u = String(url || '');
      if (u && /scidownload\\.com|lunwenxiazai/i.test(u)) {
        location.href = u;
        return window;
      }
      return _open(url, name, spec);
    };
  } catch (e) {}
})();
"""

NEED_LOGIN = object()


def is_logged(pg):
    u = surl(pg) or ""
    c = scontent(pg) or ""
    t = page_text(pg) or ""
    blob = c + "\n" + t
    if "e/member/login" in u and "loginjs.php" not in u:
        return False
    if "您还未登陆" in blob or "您还未登录" in blob:
        return False
    if "只能一人在线" in blob:
        return False
    if "高级VIP" in blob:
        return True
    if USER and USER in t and "退出" in blob:
        return True
    return False


def takeover_single_session(pg):
    t = page_text(pg) or ""
    c = scontent(pg) or ""
    if "只能一人在线" not in (t + c) and "请点击这里" not in (t + c):
        return False
    log("[single-session] 抢占本窗口会话")
    hit = pg.evaluate("""() => {
        const as = Array.from(document.querySelectorAll('a'));
        const ok = a => {
            const s = (a.innerText||'').replace(/\\s+/g,' ').trim();
            const h = (a.getAttribute('href')||'') + (a.href||'');
            if (/register/i.test(h)) return false;
            return s.indexOf('请点击这里') >= 0
                || (s.indexOf('点击这里') >= 0 && !/register/i.test(h));
        };
        let a = as.find(x => ok(x) && /member\\/cp/i.test((x.getAttribute('href')||'')+(x.href||'')));
        if (!a) a = as.find(ok);
        if (!a) return null;
        const href = a.href || a.getAttribute('href') || '';
        if (href && href.indexOf('javascript:') < 0) {
            location.href = href;
            return href.slice(0,120);
        }
        a.click();
        return (a.innerText||'').trim().slice(0,40);
    }""")
    log("[takeover-click]", hit)
    time.sleep(3)
    return True


def only_one_tab(context, keep):
    try:
        pages = list(context.pages)
    except Exception:
        return
    for p in pages:
        if p is keep:
            continue
        try:
            p.close()
            log("[close-extra]", (surl(p) or "")[:80])
        except Exception:
            pass


def confirm_cp(pg):
    t = page_text(pg) or ""
    if "只能一人在线" in t:
        takeover_single_session(pg)
        time.sleep(2)
        t = page_text(pg) or ""
    if "高级VIP" in t or (USER in t and "退出" in t):
        log("[cp-confirm] already-ok", (surl(pg) or "")[:90])
        return True
    try:
        pg.goto(SITE + "e/member/cp/", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("[cp-goto]", str(e)[:80])
    time.sleep(2)
    t = page_text(pg) or ""
    if "只能一人在线" in t:
        takeover_single_session(pg)
        time.sleep(2)
    ok = is_logged(pg)
    log("[cp-confirm]", "ok" if ok else "need-login", (surl(pg) or "")[:90])
    return ok


def wait_login(pg):
    log("WAIT_LOGIN: 请先关掉 Chrome 里的论文下载/scihuber 标签，只在本窗口填验证码登录，登录后不要关窗口")
    try:
        pg.goto(SITE + "e/member/login/", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("[login-goto]", str(e)[:80])
    time.sleep(2)
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
        log("[prefill] captcha focused")
    except Exception as e:
        log("[prefill-err]", str(e)[:80])
    for i in range(180):
        time.sleep(5)
        try:
            u = surl(pg) or ""
            t = page_text(pg) or ""
            c = scontent(pg) or ""
        except Exception as e:
            log("[login-page-err]", str(e)[:80])
            if "closed" in str(e).lower():
                return False
            continue
        blob = t + "\n" + c
        if "只能一人在线" in blob or ("信息提示" in blob and "请点击这里" in blob):
            takeover_single_session(pg)
            continue
        if "登录成功" in blob:
            takeover_single_session(pg)
            time.sleep(2)
            if confirm_cp(pg):
                log("[user-logged]", surl(pg)[:90], "t", i)
                save_pw_cookies(pg.context)
                only_one_tab(pg.context, pg)
                return True
        if "e/member/login" in u:
            if i % 6 == 0:
                log("[waiting-login]", i, u[:80])
            continue
        if "高级VIP" in blob or ("退出" in blob and USER and USER in t):
            if confirm_cp(pg):
                log("[user-logged]", surl(pg)[:90], "t", i)
                save_pw_cookies(pg.context)
                only_one_tab(pg.context, pg)
                return True
            log("[login-not-sticky]", i, surl(pg)[:80])
            try:
                pg.goto(SITE + "e/member/login/", wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log("[login-back]", str(e)[:80])
            time.sleep(1)
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
            except Exception:
                pass
            continue
        if i % 6 == 0:
            log("[waiting-login]", i, u[:80])
    log("WAIT_LOGIN_TIMEOUT")
    return False

DATA = os.environ.get("BOOKNOTE_DATA", r"E:\pubmed")
OUT_DIR = os.environ.get("SCOPUS_OUT", os.path.join(DATA, "scopus"))
RAW = os.path.join(OUT_DIR, "raw")
STATE = os.path.join(OUT_DIR, "scopus_state.json")
os.makedirs(RAW, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

ENTRIES = [
    (5460, "Scopus(mlkd)"),
]


def sci_cookies(saved):
    out = []
    for c in saved or []:
        d = str(c.get("domain") or "")
        if "scidownload" in d or str(c.get("name") or "").startswith("jtqet"):
            out.append(c)
    return out


def save_sci_only(context):
    ck = []
    try:
        for x in context.cookies():
            d = str(x.get("domain") or "")
            if "scidownload" not in d:
                continue
            ck.append({"name": x.get("name"), "value": x.get("value"),
                       "domain": d, "path": x.get("path") or "/"})
    except Exception:
        return
    if not ck:
        return
    json.dump({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "cookies": ck},
              open(SESSION_FILE, "w"), ensure_ascii=False, indent=1)
    log("[session-saved-sci]", len(ck))


def click_re(pg, pat, maxlen=40):
    hit = pg.evaluate("""(arg) => {
        const re = new RegExp(arg.pat, 'i');
        const els = Array.from(document.querySelectorAll('button, a, span, div'));
        const a = els.find(e => re.test((e.innerText||'').trim())
            && (e.offsetParent || e.getClientRects().length)
            && (e.innerText||'').trim().length < arg.maxlen);
        if (!a) return null;
        const r = a.getBoundingClientRect();
        return {x:r.x+r.width/2, y:r.y+r.height/2, t:(a.innerText||'').trim().slice(0,40)};
    }""", {"pat": pat, "maxlen": maxlen})
    log("[click]", hit)
    if hit:
        pg.mouse.click(hit["x"], hit["y"])
        return True
    return False


def page_text(pg, n=800):
    try:
        return pg.evaluate("() => (document.body && document.body.innerText) ? document.body.innerText : ''") or ""
    except Exception:
        return ""


def is_scopus_home(u, t):
    u = u or ""
    t = t or ""
    if "票据失效" in t or "Sorry, you have been blocked" in t:
        return False
    if "登录冲突" in t:
        return False
    if "scidownload.com" in u or "scihub.top" in u:
        return False
    if "Search documents" in t or "Start exploring" in t:
        return True
    if "Advanced Search" in t and "Clear form" in t:
        return True
    if "pages/home" in u and ("Brought to you" in t or "Scopus" in t):
        return True
    if re.search(r"scopus\.com|com/scopus/", u, re.I) and "pages/" in u:
        return True
    return False


def click_continue(p):
    hit = p.evaluate("""() => {
        const els = Array.from(document.querySelectorAll('button, a, input, span, div'));
        const e = els.find(x => {
            const t = (x.innerText||x.value||'').replace(/\\s+/g,' ').trim();
            return t === '继续访问';
        });
        if (!e) return null;
        e.scrollIntoView({block:'center'});
        e.click();
        const r = e.getBoundingClientRect();
        return {x:r.x+r.width/2, y:r.y+r.height/2, t:(e.innerText||e.value||'').trim().slice(0,20)};
    }""")
    log("[continue]", hit)
    if hit and hit.get("x") is not None:
        try:
            p.mouse.click(hit["x"], hit["y"])
        except Exception:
            pass
        return True
    return False


def extract_jump(p):
    try:
        urls = p.evaluate("""() => {
            const html = document.documentElement.innerHTML || '';
            const out = [];
            const re = /https?:\\/\\/[^"'\\s<>]+/g;
            let m;
            while ((m = re.exec(html))) {
                const u = m[0];
                if (/scopus|scihub\\.top|cwres|ersp\\.lib|ncu\\.edu|whu\\.edu/i.test(u)
                    && !/scidownload|xueshu365|51\\.la/.test(u))
                    out.push(u);
            }
            return out.slice(0, 8);
        }""") or []
    except Exception:
        urls = []
    return urls


def close_dead_tabs(context, keep=None):
    for p in list(context.pages):
        if keep is not None and p is keep:
            continue
        try:
            u = surl(p) or ""
            t = page_text(p) or ""
        except Exception:
            continue
        dead = ("票据失效" in t or "当日下载量已超标" in t or "403 Forbidden" in t
                or "您还未登陆" in t)
        keep_list = "ListInfo" in u or "member/cp" in u or "yingwenku" in u
        if dead and not keep_list:
            try:
                p.close()
                log("[close-dead]", u[:100])
            except Exception:
                pass


def open_entry(pg, eid, name):
    log("[open]", name, eid)
    close_dead_tabs(pg.context, keep=pg)
    list_url = SITE + "e/action/ListInfo/?classid=225"
    try:
        pg.goto(list_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("[list-err]", str(e)[:80])
        if "closed" in str(e).lower():
            return None
    time.sleep(3)
    t0 = page_text(pg) or ""
    if "只能一人在线" in t0:
        takeover_single_session(pg)
        time.sleep(2)
        if not confirm_cp(pg):
            return NEED_LOGIN
        try:
            pg.goto(list_url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        time.sleep(2)
        t0 = page_text(pg) or ""
    if "您还未登陆" in t0 or "您还未登录" in t0:
        log("[need-login] list", name)
        return NEED_LOGIN
    if USER not in t0 and "您好" not in t0 and "退出" not in t0:
        log("[need-login] header-guest", name, t0.replace("\n", " ")[:60])
        return NEED_LOGIN
    only_one_tab(pg.context, pg)
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
        hit = pg.evaluate("""(name) => {
            const vis = e => !!(e.offsetParent || (e.getClientRects && e.getClientRects().length));
            const a = Array.from(document.querySelectorAll('a, span, div, button')).find(e =>
                vis(e) && (e.innerText||'').trim() === name);
            if (!a) return null;
            a.click();
            const r = a.getBoundingClientRect();
            return {x:r.x+r.width/2, y:r.y+r.height/2};
        }""", name)
        log("[click-name]", hit)
        if hit:
            clicked = True
    if not clicked:
        try:
            pg.goto(SITE + "e/action/ShowInfo.php?classid=225&id=%d" % eid,
                    wait_until="domcontentloaded", timeout=90000)
            log("[goto-showinfo]", name, eid)
        except Exception as e:
            log("[goto-err]", str(e)[:80])
            if "closed" in str(e).lower():
                return None
    jumped = False
    dead = 0
    for i in range(20):
        time.sleep(3)
        try:
            pages = list(pg.context.pages)
        except Exception as e:
            log("[pages-err]", str(e)[:80])
            return None
        if i == 2 and not jumped:
            urls = []
            for p in pages:
                try:
                    urls.append(surl(p) or "")
                except Exception:
                    pass
            if not any(re.search(r"scopus|cwres|ersp|lunwen\.one|downsci", u, re.I) for u in urls):
                try:
                    pg.goto(SITE + "e/action/ShowInfo.php?classid=225&id=%d" % eid,
                            wait_until="domcontentloaded", timeout=90000)
                    jumped = True
                    log("[force-showinfo]", name, eid)
                except Exception as e:
                    log("[force-showinfo-err]", str(e)[:80])
        for p in pages:
            try:
                u = surl(p)
                t = page_text(p)
            except Exception:
                continue
            log("[wait %s %d]" % (name, i), (u or "")[:120], (t or "").replace("\n", " ")[:90])
            if "您还未登陆" in (t or "") or "您还未登录" in (t or ""):
                log("[need-login] showinfo", name)
                return NEED_LOGIN
            if "只能一人在线" in (t or ""):
                takeover_single_session(p)
                time.sleep(2)
                if confirm_cp(p):
                    save_pw_cookies(p.context)
                    only_one_tab(p.context, p)
                    try:
                        p.goto(SITE + "e/action/ShowInfo.php?classid=225&id=%d" % eid,
                               wait_until="domcontentloaded", timeout=90000)
                        log("[retry-showinfo]", name)
                    except Exception as e:
                        log("[retry-showinfo-err]", str(e)[:80])
                continue
            if "403 Forbidden" in (t or "") and re.search(r"lunwen\.one|kscopus|downsci\.top|mlkd\.php", u or "", re.I):
                dead += 1
                log("[hop]", dead, (u or "")[:100])
                if dead >= 3:
                    log("[skip-entry]", name, "403 hop")
                    return None
                continue
            if "ersp.lib.whu.edu.cn" in (u or "") or "cwres.ncu.edu.cn" in (u or ""):
                log("[skip-proxy]", name, "user-skip 南大/武大")
                try:
                    if len(list(pg.context.pages)) > 1:
                        p.close()
                except Exception:
                    pass
                return None
            if "票据失效" in (t or "") or "当日下载量已超标" in (t or "") or "403 Forbidden" in (t or ""):
                if "scidownload.com" in (u or ""):
                    continue
                dead += 1
                log("[dead-tab]", name, dead, (u or "")[:100])
                if dead >= 2:
                    log("[skip-entry]", name, (t or "").replace("\n", " ")[:40])
                    try:
                        if len(list(pg.context.pages)) > 1:
                            p.close()
                    except Exception:
                        pass
                    return None
                continue
            if "继续访问" in (t or "") or "登录冲突" in (t or ""):
                click_continue(p)
                log("WAIT_CONTINUE: 请在窗口点「继续访问」，点完不要关")
                time.sleep(6)
                continue
            if is_scopus_home(u, t):
                log("[landed]", name, (u or "")[:140])
                return p
            if (u or "") and re.search(r"cwres\.ncu|ersp\.lib\.whu|scihub\.top|scopus\.com", u, re.I):
                if "票据失效" not in (t or "") and "登录冲突" not in (t or ""):
                    log("[proxy-wait]", name, (u or "")[:140])
            if not jumped and "ShowInfo.php" in (u or ""):
                js = [x for x in (extract_jump(p) or [])
                      if not re.search(r"downsci|mlkd\.php|whu\.edu|ncu\.edu", x, re.I)]
                if js:
                    log("[jump-url]", js[0][:160])
                    try:
                        p.goto(js[0], wait_until="domcontentloaded", timeout=90000)
                        jumped = True
                    except Exception as e:
                        log("[jump-err]", str(e)[:80])
    snap(pg, "scopus_noload_%s" % eid)
    log("[no-land]", name, surl(pg)[:120])
    return None


def advanced_url(u):
    u = u or ""
    if "/s/" in u and "/pages/" in u:
        return u.split("/pages/")[0] + "/pages/search/publications?type=advanced"
    m = re.match(r"(https?://[^/]+)", u)
    if m and ("scopus" in u.lower() or "elsevier" in u.lower()):
        return m.group(1) + "/pages/search/publications?type=advanced"
    return ""


def goto_advanced(pg):
    u = surl(pg) or ""
    if "search/publications" in u and "advanced" in u:
        return True
    click_re(pg, r"Advanced document search")
    for i in range(8):
        time.sleep(2)
        n = 0
        try:
            n = pg.evaluate("() => document.querySelectorAll('.monaco-editor, .view-lines').length")
        except Exception:
            n = 0
        log("[monaco]", i, n)
        if n:
            return True
    adv = advanced_url(surl(pg) or u)
    if adv:
        log("[adv-url]", adv[:160])
        try:
            pg.goto(adv, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            log("[adv-goto]", str(e)[:80])
        for i in range(16):
            time.sleep(2)
            n = 0
            try:
                n = pg.evaluate("() => document.querySelectorAll('.monaco-editor, .view-lines').length")
            except Exception:
                n = 0
            log("[monaco2]", i, n)
            if n:
                return True
    return False


def fill_and_search(pg, day, q=None):
    ymd = day.replace("-", "")
    q = q or ("LOAD-DATE IS %s" % ymd)
    box = pg.evaluate("""() => {
        const e = document.querySelector('.monaco-editor') || document.querySelector('textarea');
        if (!e) return null;
        const r = e.getBoundingClientRect();
        return {x:r.x + Math.min(80, r.width/3), y:r.y + Math.min(40, r.height/2)};
    }""")
    if not box:
        pg.evaluate("""() => {
            const e = Array.from(document.querySelectorAll('button, a')).find(x =>
                /Edit in advanced search/i.test((x.innerText||'').trim()));
            if (e) e.click();
        }""")
        time.sleep(2)
        box = pg.evaluate("""() => {
            const e = document.querySelector('.monaco-editor') || document.querySelector('textarea');
            if (!e) return null;
            const r = e.getBoundingClientRect();
            return {x:r.x + Math.min(80, r.width/3), y:r.y + Math.min(40, r.height/2)};
        }""")
    log("[editor]", box)
    if box:
        pg.mouse.click(box["x"], box["y"])
        time.sleep(0.35)
        try:
            pg.keyboard.press("Control+A")
            time.sleep(0.1)
            pg.keyboard.press("Backspace")
        except Exception:
            pass
        pg.keyboard.type(q, delay=25)
        log("[typed-keys]", q)
    try:
        pg.evaluate("""(q) => {
            try {
                const mods = window.monaco && window.monaco.editor && window.monaco.editor.getModels();
                if (mods && mods.length) mods[mods.length-1].setValue(q);
            } catch (e) {}
            const ta = document.querySelector('textarea.inputarea, .monaco-editor textarea, textarea');
            if (ta) {
                ta.focus();
                const desc = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value');
                if (desc && desc.set) desc.set.call(ta, q); else ta.value = q;
                ta.dispatchEvent(new InputEvent('input', {bubbles:true, data:q, inputType:'insertText'}));
                ta.dispatchEvent(new Event('change', {bubbles:true}));
            }
        }""", q)
    except Exception as e:
        log("[typed-js-err]", str(e)[:80])
    time.sleep(0.6)
    info = pg.evaluate("""() => {
        const norm = e => ((e.innerText||e.value||'')+' '+(e.getAttribute('aria-label')||'')).replace(/\\s+/g,' ').trim();
        const els = Array.from(document.querySelectorAll('button, [role=button], input[type=submit]'));
        const all = els.map(b => {
            const r = b.getBoundingClientRect();
            return {t: norm(b).slice(0,40), y: Math.round(r.y), x: Math.round(r.x),
                    w: Math.round(r.width), h: Math.round(r.height), tag: b.tagName};
        }).filter(x => x.w > 8 && x.h > 8);
        const hit = all.find(x => x.y > 120 && /^search\\b/i.test(x.t) && x.t.length < 24)
                 || all.find(x => /^search\\b/i.test(x.t) && x.t.length < 24);
        return {n: all.length, sample: all.slice(0, 12), hit};
    }""")
    log("[btns]", json.dumps(info, ensure_ascii=False)[:600])
    hit = (info or {}).get("hit") if isinstance(info, dict) else None
    if hit:
        pg.mouse.click(hit["x"] + 8, hit["y"] + hit["h"] / 2)
        log("[search-click]", hit)
    else:
        pg.keyboard.press("Enter")
        log("[search-enter]")
    time.sleep(1)
    return True


def results_ready(pg):
    t = page_text(pg) or ""
    if "No documents matching" in t or "No documents were found" in t:
        return "zero"
    m = re.search(r"([\d,]+)\s+documents?\b", t, re.I)
    if m:
        try:
            n = int(m.group(1).replace(",", ""))
        except Exception:
            n = -1
        if n == 0:
            return "zero"
        if n > 0:
            return "ok"
    return ""


def wait_results(pg, box, n=36):
    for i in range(n):
        time.sleep(4)
        u = surl(pg)
        t = page_text(pg)
        st = results_ready(pg)
        log("[res %d]" % i, st or "-", (u or "")[:100], (t or "").replace("\n", " ")[:130])
        if box.get("docs"):
            log("[have-json]")
            return True
        if st == "zero":
            log("[zero]")
            return True
        if st == "ok":
            time.sleep(3)
            return True
    return bool(box.get("docs"))


def attach(pg, box):
    def on_resp(r):
        try:
            u = r.url or ""
            if re.search(r"pendo|_next/static|woff|png|css|nr-data", u, re.I):
                return
            if not re.search(r"search|export|document|graphql|gateway|elsevier", u, re.I):
                return
            body = ""
            try:
                ct = r.headers.get("content-type") or ""
                if "json" in ct or "csv" in ct:
                    body = (r.text() or "")[:80000]
            except Exception:
                body = ""
            if body:
                box.setdefault("net", []).append({"u": u[:200], "st": r.status, "b": body[:3000]})
                log("[net]", r.status, u[:120], re.sub(r"\s+", " ", body)[:160])
                if "/gateway/documents/search" in u and "parsequery" not in u and '"items"' in body:
                    recs, tot = parse_docs(body)
                    if recs:
                        box["docs"] = body
                        log("[docs-hit]", u[:100], "n", len(recs), "total", tot, "len", len(body))
                    elif not box.get("docs"):
                        box["docs"] = body
                        log("[docs-empty]", u[:100], "total", tot)
                elif (not box.get("docs")) and re.search(r'"eid"|dc:identifier|totalResults|"entries"', body):
                    box["docs"] = body
                    log("[docs-hit]", u[:100], "len", len(body))
        except Exception:
            pass
    pg.on("response", on_resp)


def _txt(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return v.get("text") or v.get("value") or v.get("name") or ""
    return str(v)


def _authors(v):
    if not v:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        names = []
        for a in v:
            if isinstance(a, str):
                names.append(a)
            elif isinstance(a, dict):
                names.append(_txt(a.get("preferredName") or a.get("name") or a.get("surname") or a))
        return "; ".join(x for x in names if x)
    return _txt(v)


def parse_docs(body):
    recs = []
    if not body:
        return recs, None
    try:
        d = json.loads(body)
    except Exception:
        return recs, None
    total = None
    items = None
    if isinstance(d, dict) and isinstance(d.get("items"), list):
        md = d.get("metadata") or {}
        total = md.get("totalCount") or md.get("total") or d.get("totalCount")
        items = d["items"]
    else:
        sr = d.get("search-results") or d.get("searchResults") or d
        if isinstance(sr, dict):
            total = sr.get("opensearch:totalResults") or sr.get("totalResults") or sr.get("total")
            items = sr.get("entry") or sr.get("entries") or sr.get("documents") or sr.get("items") or []
            if isinstance(items, dict):
                items = list(items.values())
    try:
        total = int(total) if total is not None else None
    except Exception:
        total = None
    for e in items or []:
        if not isinstance(e, dict):
            continue
        cited = e.get("citedby-count") or e.get("citedByCount")
        if cited is None and isinstance(e.get("citations"), dict):
            cited = e["citations"].get("count")
        rec = {
            "eid": _txt(e.get("eid") or e.get("dc:identifier")),
            "doi": _txt(e.get("prism:doi") or e.get("doi")).replace("https://doi.org/", ""),
            "title": _txt(e.get("dc:title") or e.get("title")),
            "year": _txt(e.get("prism:coverDate") or e.get("coverDate") or e.get("publicationYear") or e.get("year"))[:4],
            "source": _txt(e.get("prism:publicationName") or e.get("publicationName") or e.get("sourceTitle")),
            "citedby": cited,
            "subtype": _txt(e.get("subtypeDescription") or e.get("documentType") or e.get("subtype")),
            "authors": _authors(e.get("dc:creator") or e.get("creator") or e.get("authors") or e.get("author")),
        }
        if rec["eid"] or rec["doi"] or rec["title"]:
            recs.append(rec)
    return recs, total


def scrape_table(pg):
    try:
        rows = pg.evaluate("""() => {
            const trs = Array.from(document.querySelectorAll('table tbody tr, [role=row]'));
            const out = [];
            trs.forEach(tr => {
                const tds = Array.from(tr.querySelectorAll('td, [role=cell]')).map(e => (e.innerText||'').trim());
                const a = tr.querySelector('a');
                if (tds.filter(Boolean).length >= 2)
                    out.push({title: (a && a.innerText || tds[0] || '').slice(0,400),
                              href: a ? (a.href||'') : '', cells: tds.slice(0,8)});
            });
            return out.slice(0, 200);
        }""") or []
    except Exception as e:
        log("[scrape-err]", str(e)[:80])
        return []
    recs = []
    for r in rows:
        cells = r.get("cells") or []
        rec = {
            "title": r.get("title") or "",
            "authors": cells[1] if len(cells) > 1 else "",
            "source": cells[2] if len(cells) > 2 else "",
            "year": cells[3] if len(cells) > 3 else "",
            "citedby": cells[4] if len(cells) > 4 else "",
            "href": r.get("href") or "",
        }
        m = re.search(r"/publications/(\d+)", rec["href"] or "")
        if m:
            rec["eid"] = "2-s2.0-" + m.group(1) if not m.group(1).startswith("2-s2") else m.group(1)
        if rec["title"] and len(rec["title"]) > 8:
            recs.append(rec)
    log("[scrape]", len(recs))
    return recs


def gateway_base(u):
    u = u or ""
    m = re.search(r"(https?://[^/]+(?:/s/[^/]+(?:/[^/]+)*/G\.https)?)", u)
    if m and "/s/" in u:
        cut = u.split("/pages/")[0].split("/gateway/")[0]
        return cut
    return re.match(r"https?://[^/]+", u).group(0) if u.startswith("http") else ""


def fetch_by_search_id(pg, box, query=None):
    sid = None
    for item in box.get("net") or []:
        m = re.search(r'"searchId"\s*:\s*"([^"]+)"', item.get("b") or "")
        if m:
            sid = m.group(1)
    log("[searchId]", sid)
    base = gateway_base(surl(pg))
    q = query or "LOAD-DATE IS 20260908"
    try:
        r = pg.evaluate("""async (arg) => {
            const base = (arg.base || '').replace(/\\/$/, '');
            const sid = arg.sid;
            const q = arg.q;
            const paths = [];
            const rel = [
                '/gateway/documents/search',
                '/gateway/documents/search/results',
            ];
            if (sid) rel.push('/gateway/search-management-service/searchmanager/' + sid + '/results');
            for (const p of rel) paths.push(base + p);
            const bodies = [
                {query: q, offset: 0, limit: 100, documentType: 's'},
                {searchId: sid, offset: 0, limit: 100},
                {searchId: sid, start: 0, count: 100},
            ];
            const out = [];
            for (const p of paths) {
                for (const b of bodies) {
                    if (!b.searchId && p.indexOf('searchmanager') >= 0) continue;
                    try {
                        const rr = await fetch(p, {method:'POST',
                            headers:{'Content-Type':'application/json','Accept':'application/json'},
                            body: JSON.stringify(b), credentials:'include'});
                        const t = await rr.text();
                        out.push({p, st: rr.status, n: t.length});
                        if (rr.status === 200 && t.length > 80) return {p, st: rr.status, t};
                    } catch (e) { out.push({p, err: String(e).slice(0,60)}); }
                }
            }
            return {tries: out};
        }""", {"base": base, "sid": sid, "q": q})
    except Exception as e:
        log("[fetch-sid-err]", str(e)[:80])
        return [], None
    log("[fetch-sid]", json.dumps({k: (r or {}).get(k) if k != "t" else str((r or {}).get("t"))[:200]
                                   for k in (r or {})}, ensure_ascii=False)[:900]
        if isinstance(r, dict) else str(r)[:200])
    body = (r or {}).get("t") or ""
    recs, total = parse_docs(body)
    return recs, total


def parse_scopus_csv(path):
    recs = []
    try:
        import csv as csvmod
        raw = open(path, "r", encoding="utf-8-sig", errors="replace").read()
        lines = raw.splitlines()
        if not lines:
            return recs
        rdr = csvmod.DictReader(lines)
        for row in rdr:
            low = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            rec = {
                "eid": low.get("eid") or low.get("scopus eid") or "",
                "doi": (low.get("doi") or "").replace("https://doi.org/", ""),
                "title": low.get("title") or low.get("document title") or "",
                "year": (low.get("year") or low.get("year of publication") or "")[:4],
                "source": low.get("source title") or low.get("publication name") or "",
                "authors": low.get("authors") or low.get("author(s)") or "",
                "citedby": low.get("cited by") or low.get("citedby") or "",
                "subtype": low.get("document type") or "",
            }
            if rec["eid"] or rec["doi"] or rec["title"]:
                recs.append(rec)
    except Exception as e:
        log("[csv-parse-err]", str(e)[:80])
    log("[csv-parse]", path, "n", len(recs))
    return recs


def export_csv(pg, box):
    """对照 WoS saveToFile：结果页勾选 → Export → CSV → 等下载。"""
    os.makedirs(RAW, exist_ok=True)
    info = pg.evaluate("""() => {
        const norm = e => (e.innerText||e.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim();
        const els = Array.from(document.querySelectorAll('button, a, [role=button], input[type=checkbox]'));
        return els.map(e => {
            const r = e.getBoundingClientRect();
            return {t: norm(e).slice(0,40), tag: e.tagName, y: Math.round(r.y),
                    x: Math.round(r.x), w: Math.round(r.width), h: Math.round(r.height),
                    type: e.getAttribute('type')||''};
        }).filter(x => x.w>8 && x.h>8).slice(0, 40);
    }""")
    log("[export-ui]", json.dumps(info, ensure_ascii=False)[:900])
    pg.evaluate("""() => {
        const cbs = Array.from(document.querySelectorAll('input[type=checkbox], [role=checkbox]'));
        const head = cbs.find(e => {
            const t = ((e.getAttribute('aria-label')||'')+' '+(e.closest('th,label,[role=columnheader]')||{}).innerText||'').toLowerCase();
            return /select all|all results|all documents/.test(t);
        }) || cbs[0];
        if (head) head.click();
    }""")
    time.sleep(0.6)
    hit = pg.evaluate("""() => {
        const norm = e => (e.innerText||e.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim();
        const e = Array.from(document.querySelectorAll('button, a, [role=button]')).find(b => {
            const t = norm(b);
            return /^export$/i.test(t) || t.toLowerCase() === 'export';
        });
        if (!e) return null;
        e.scrollIntoView({block:'center'});
        e.click();
        const r = e.getBoundingClientRect();
        return {t: norm(e).slice(0,30), y: r.y};
    }""")
    log("[export-click]", hit)
    if not hit:
        return []
    time.sleep(1.2)
    snap(pg, "scopus_export_menu")
    dest = os.path.join(RAW, "scopus_export_%s.csv" % time.strftime("%H%M%S"))
    try:
        with pg.expect_download(timeout=120000) as dlinfo:
            csv_hit = pg.evaluate("""() => {
                const norm = e => (e.innerText||'').replace(/\\s+/g,' ').trim();
                const e = Array.from(document.querySelectorAll('button, a, li, span, div, [role=menuitem]')).find(b => {
                    const t = norm(b);
                    return t === 'CSV' || (/^csv\\b/i.test(t) && t.length < 24);
                });
                if (e) e.click();
                const go = Array.from(document.querySelectorAll('button')).find(b => {
                    const t = norm(b);
                    return t === 'Export' || t === 'Download';
                });
                if (go) go.click();
                return {csv: e ? norm(e).slice(0,30) : null, go: go ? norm(go) : null};
            }""")
            log("[export-csv]", csv_hit)
        d = dlinfo.value
        d.save_as(dest)
        log("[export-file]", dest, "name", d.suggested_filename)
        box["csv"] = dest
        return parse_scopus_csv(dest)
    except Exception as e:
        log("[export-dl]", str(e)[:120])
    for item in box.get("net") or []:
        u = item.get("u") or ""
        if re.search(r"export|download", u, re.I):
            log("[export-net]", item.get("st"), u[:140], str(item.get("b") or "")[:120])
    snap(pg, "scopus_export_after")
    return []


def fetch_rest(pg, box, query, recs, total):
    base = gateway_base(surl(pg))
    have = set((r.get("eid") or r.get("doi") or r.get("title") or "") for r in recs)
    offset = len(recs)
    limit = 10
    while total and offset < min(int(total), 2000):
        try:
            body = pg.evaluate("""async (arg) => {
                const payloads = [
                    {query: arg.q, offset: arg.offset, limit: arg.limit, documentType: 's'},
                    {query: arg.q, start: arg.offset, count: arg.limit, documentType: 's'},
                    {query: arg.q, offset: arg.offset, limit: arg.limit, documentType: 's',
                     metadata: {offset: arg.offset, itemCount: arg.limit}}
                ];
                let last = '';
                for (const b of payloads) {
                    const r = await fetch(arg.base + '/gateway/documents/search', {
                        method: 'POST',
                        headers: {'Content-Type':'application/json','Accept':'application/json'},
                        credentials: 'include',
                        body: JSON.stringify(b)
                    });
                    const t = await r.text();
                    last = t;
                    if (t && t.indexOf('"eid"') >= 0 && t.length > 200) return t;
                }
                return last;
            }""", {"base": base, "q": query, "offset": offset, "limit": limit})
        except Exception as e:
            log("[page-err]", offset, str(e)[:80])
            break
        more, tot = parse_docs(body or "")
        if tot:
            total = tot
        if not more:
            log("[page-empty]", offset, (body or "")[:120])
            break
        nadd = 0
        for rec in more:
            k = rec.get("eid") or rec.get("doi") or rec.get("title") or ""
            if not k or k in have:
                continue
            have.add(k)
            recs.append(rec)
            nadd += 1
        log("[page]", offset, "got", len(more), "new", nadd, "have", len(recs), "/", total)
        if nadd == 0:
            break
        offset += max(len(more), limit)
        time.sleep(0.4)
    return recs, total


def harvest(pg, day, box):
    snap(pg, "scopus_land")
    if not goto_advanced(pg):
        log("[no-adv]")
        snap(pg, "scopus_no_adv")
        return 2
    snap(pg, "scopus_adv")
    try:
        fields = pg.evaluate("""() => {
            const click = t => {
                const e = Array.from(document.querySelectorAll('button, a, div, span')).find(
                    x => (x.innerText||'').trim() === t);
                if (e) e.click();
            };
            click('Document');
            const all = Array.from(document.querySelectorAll('button, a, li, span, div'))
                .map(e => (e.innerText||'').replace(/\\s+/g,' ').trim())
                .filter(t => t.length>1 && t.length<48);
            const uniq = [];
            all.forEach(t => { if (uniq.indexOf(t)<0) uniq.push(t); });
            return uniq.filter(t => /date|load|year|pub|orig/i.test(t)).slice(0, 40);
        }""")
        log("[fields]", json.dumps(fields, ensure_ascii=False)[:800])
    except Exception as e:
        log("[fields-err]", str(e)[:80])
    ymd = day.replace("-", "")
    d0 = datetime.date.fromisoformat(day)
    prev = (d0 - datetime.timedelta(days=1)).strftime("%Y%m%d")
    nxt = (d0 + datetime.timedelta(days=1)).strftime("%Y%m%d")
    queries = [
        "ORIG-LOAD-DATE AFT %s AND ORIG-LOAD-DATE BEF %s" % (prev, nxt),
        "ORIG-LOAD-DATE AFT %s" % prev,
        "LOAD-DATE = %s" % ymd,
        "LOAD-DATE > %s" % prev,
    ]
    last_st = ""
    used_q = ""
    for q in queries:
        box.pop("docs", None)
        log("[try-q]", q)
        fill_and_search(pg, day, q=q)
        wait_results(pg, box, n=10)
        last_st = results_ready(pg)
        snap(pg, "scopus_results")
        log("[q-st]", last_st or "-", q)
        if last_st == "ok" or box.get("docs"):
            used_q = q
            break
        if last_st == "zero":
            log("[zero-try-next-syntax]", q)
    recs, total = parse_docs(box.get("docs") or "")
    log("[parsed-hook]", "n", len(recs), "total", total, "st", last_st, "q", used_q or queries[-1])
    if recs and total and len(recs) < min(int(total), 500):
        recs, total = fetch_rest(pg, box, used_q or queries[0], recs, total)
        log("[paged]", "n", len(recs), "total", total)
    if last_st == "ok" and not recs:
        recs = export_csv(pg, box)
        total = total or len(recs)
        log("[parsed-export]", "n", len(recs))
    if last_st != "ok" and not recs:
        recs = scrape_table(pg)
        total = total or len(recs)
        log("[parsed-table]", "n", len(recs))
    if last_st == "zero" and not recs:
        recs, total = [], 0
    out_path = os.path.join(OUT_DIR, "scopus_%s.jsonl" % day[:7])
    have = set()
    if os.path.exists(out_path):
        for line in open(out_path, encoding="utf-8"):
            try:
                v = json.loads(line)
                have.add(v.get("eid") or v.get("doi") or "")
            except Exception:
                pass
    added = 0
    with open(out_path, "a", encoding="utf-8") as f:
        for rec in recs:
            k = rec.get("eid") or rec.get("doi") or rec.get("title") or ""
            if not k or k in have:
                continue
            have.add(k)
            rec["load_date"] = day
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            added += 1
    st = {"last_done": day, "last_total": total, "last_new": added, "n_parsed": len(recs),
          "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    json.dump(st, open(STATE, "w"), ensure_ascii=False, indent=1)
    log("[WINDOW_DONE]", day, "new", added, "parsed", len(recs), "total", total)
    if recs or added:
        return 0
    if last_st == "zero":
        log("[zero-next-entry] 本入口无结果，换下一个")
        return 2
    return 2


def run_day(day, headed=True):
    from camoufox.sync_api import Camoufox
    saved = load_pw_cookies()
    vip = bool(saved and cookies_still_vip(saved))
    ck = sci_cookies(saved)
    if not vip:
        log("[vip-stale] will inject cookies anyway; headed wait-login if cp is not logged in")
        if not headed and not ck:
            log("NO_SESSION: 请先 python harvest/wos_pipeline.py --wait-login")
            return 2
    box = {}
    with Camoufox(headless=not headed, exclude_addons=["ublock-origin"]) as b:
        pg = b.new_page()
        pg.set_default_timeout(90000)
        try:
            pg.context.add_init_script(ANTI_DEBUG)
        except Exception as e:
            log("[init-script]", str(e)[:80])
        pg.on("dialog", lambda d: d.accept())
        if ck and vip:
            inject_cookie_list(pg.context, ck)
        elif ck:
            log("[skip-stale-cookies] vip-check false, stay on login form")
        try:
            pg.goto(SITE + "e/member/cp/", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log("[cp-first]", str(e)[:80])
        time.sleep(2)
        logged = is_logged(pg)
        t0 = page_text(pg) or ""
        if (not logged) and ("只能一人在线" in t0 or "请点击这里" in t0):
            takeover_single_session(pg)
            time.sleep(2)
            logged = is_logged(pg)
        log("[cp]", (surl(pg) or "")[:90], "logged" if logged else "need-login")
        if not logged:
            if not headed:
                log("NO_SESSION")
                return 2
            if not wait_login(pg):
                log("FAIL_LOGIN")
                return 2
        only_one_tab(pg.context, pg)
        attach(pg, box)
        pg.context.on("page", lambda p: attach(p, box))
        last_rc = 3
        for attempt in range(2):
            for eid, name in ENTRIES:
                try:
                    app = open_entry(pg, eid, name)
                except Exception as e:
                    log("[open-ex]", name, str(e)[:100])
                    if "closed" in str(e).lower():
                        return 3
                    app = None
                if app is NEED_LOGIN:
                    log("[relogin-needed]", name, "attempt", attempt)
                    if not headed or not wait_login(pg):
                        log("FAIL_LOGIN")
                        return 2
                    break
                if app is None:
                    continue
                log("[using]", name)
                save_sci_only(app.context)
                last_rc = harvest(app, day, box)
                if last_rc == 0:
                    return 0
                log("[harvest-fail-next]", name, "rc", last_rc)
            else:
                continue
            continue
        log("FAIL_LAND")
        return last_rc


def main():
    args = sys.argv[1:]
    headed = "--headless" not in args
    rest = [a for a in args if not a.startswith("--")]
    arg = rest[0] if rest else (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    log("=== scopus start", time.strftime("%Y-%m-%d %H:%M:%S"), "day", arg, "headed", headed, "===")
    rc = run_day(arg, headed=headed)
    log("DONE" if rc == 0 else "FAILED rc=%d" % rc)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        log("FAILED exception")
        sys.exit(3)
