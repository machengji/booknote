# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# ieee_zju_session.py — IEEE ZJU Shibboleth 会话引导（干净判定版）
# 原则：只在验证码不可见时提交（自适应验证码出现=状态不干净，放弃本轮）
#       无验证码提交失败 ⇒ 凭据确凿错误 → 换下一组凭据；成功 ⇒ 探PDF+存state
import sys, os, time, json, re
_log = open(_os.path.join(LOGS, r"ieee_zju_session.log"), "ab", buffering=0)
os.dup2(_log.fileno(), 1)
os.dup2(_log.fileno(), 2)
sys.stdout = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
from camoufox.sync_api import Camoufox

# 混沌卡32凭据 + 用户自有 CARSI 备用账号（08-31 验证 ZJU_USER 有效，排第一）
CRED_LIST = zju_creds()
WAYF = ("https://ieeexplore.ieee.org/servlet/wayf.jsp"
        "?entityId=https://idp.zju.edu.cn/idp/shibboleth"
        "&url=https://ieeexplore.ieee.org/Xplore/home.jsp")
PROXY = os.environ.get("IEEE_PROXY", "direct")
STATE_OUT = os.environ.get("IEEE_STATE", _os.path.join(FT, r"ieee_zju_state.json"))
PROBE_ARNUM = "3353194"  # 10.1109/JPHOT.2024.3353194
OUT_DIR = _os.path.join(PDFS, r"IEEE Xplore")

_kw = {}
if PROXY.strip().lower() not in ("direct", "", "none"):
    _kw = {"proxy": {"server": PROXY}}


def content(pg):
    try:
        return pg.content() or ""
    except Exception:
        return ""


def dump_page(pg, tag):
    try:
        open(_os.path.join(LOGS, r"_ieee_%s.html") % tag, "w", encoding="utf-8").write(content(pg))
        pg.screenshot(path=_os.path.join(LOGS, r"_ieee_%s.png") % tag)
    except Exception:
        pass


def cas_probe(pg, user, pwd):
    """单次 CAS 干净判定。返回 'OK' | 'WRONG' | 'LOCKED_WAITED' | 'DIRTY' | 'ERR'
    DIRTY = 验证码出现（状态不干净，本浏览器会话已污染，放弃）"""
    # 等锁定：最多 2 轮
    for _ in range(2):
        c0 = content(pg)
        if "锁定" in c0 or "登录异常" in c0:
            m = re.search(r"(\d{2})时\s*(\d{2})分\s*(\d{2})秒", c0)
            wait = min(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + 45, 700) if m else 320
            print("  [locked] waiting %ds" % wait, flush=True)
            time.sleep(wait)
            try:
                pg.reload(wait_until="domcontentloaded")
            except Exception:
                pass
            time.sleep(6)
        else:
            break
    eu = pg.query_selector("#username")
    ep = pg.query_selector("#password")
    if not eu or not ep:
        print("  [cas] fields-missing", flush=True)
        dump_page(pg, "fields_missing")
        return "ERR"
    vis = pg.evaluate(
        """() => {
            const g = id => { const e = document.getElementById(id); return e ? !!(e.offsetParent || e.getClientRects().length) : false; };
            return {auth: g('authcode'), pic: g('yzmPic')};
        }""")
    print("  [vis]", vis, flush=True)
    if vis.get("pic"):
        print("  [captcha-required] DIRTY, skip submit", flush=True)
        return "DIRTY"
    eu.fill(user)
    ep.fill(pwd)
    print("  [clean submit] user=%s" % user, flush=True)
    pg.evaluate("var b=document.getElementById('dl'); if(b){b.click();} else {var f=document.querySelector('#fm1')||document.querySelector('form'); if(f){f.submit();}}")
    # 轮询等待 SAML 链走完（最长 45s）：离开 zjuam 或 IEEE 页出现机构标识
    for i in range(15):
        time.sleep(3)
        u = pg.url or ""
        c2 = content(pg)
        if "zjuam.zju.edu.cn" not in u or "Access provided by" in c2:
            print("  [cas] landed ->", u[:110], flush=True)
            print("  [access]", "Access provided by: Zhejiang University" in c2, flush=True)
            return "OK"
        if "用户名或密码错误" in c2 or "密码错误" in c2:
            print("  [clean submit] -> WRONG CREDENTIALS (no captcha involved)", flush=True)
            dump_page(pg, "wrongpwd")
            return "WRONG"
        if "锁定" in c2 or "登录异常" in c2:
            print("  [clean submit] -> LOCKED (was not fully expired)", flush=True)
            return "LOCKED_WAITED"
    print("  [clean submit] -> stuck on intermediate page", flush=True)
    print("  [url]", pg.url[:130], flush=True)
    dump_page(pg, "stuck")
    return "ERR"


def probe_pdf(pg):
    """登录后试抓一篇 PDF 验证全链路"""
    arnum = PROBE_ARNUM
    try:
        resp = pg.goto("https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber=%s&ref=" % arnum,
                       wait_until="domcontentloaded", timeout=90000)
        time.sleep(4)
        body = resp.body() if resp else b""
        print("[probe] st=%s len=%d head=%r" % (resp.status if resp else "?", len(body), body[:8]), flush=True)
        if body[:5] == b"%PDF-":
            out = os.path.join(OUT_DIR, "_zju_probe_%s.pdf" % arnum)
            open(out, "wb").write(body)
            print("PROBE_PDF_OK", out, flush=True)
            return True
        open(_os.path.join(LOGS, r"_ieee_probe_resp.bin"), "wb").write(body[:60000])
        return False
    except Exception as e:
        print("  probe-err", str(e)[:120], flush=True)
        return False


def run_cred(user, pwd):
    """一个凭据一次完整尝试（独立浏览器上下文）。返回 'OK'|'WRONG'|'DIRTY'|'LOCKED'|'ERR'"""
    with Camoufox(headless=True, exclude_addons=["ublock-origin"], **_kw) as b:
        pg = b.new_page()
        pg.set_default_timeout(60000)
        time.sleep(2)
        print("[1] goto WAYF (user=%s)" % user, flush=True)
        pg.goto(WAYF, wait_until="domcontentloaded")
        time.sleep(6)
        print("[url]", pg.url[:130], flush=True)
        verdict = cas_probe(pg, user, pwd)
        if verdict != "OK":
            return verdict
        # 成功：等 SAML 跳回 IEEE
        for i in range(10):
            time.sleep(4)
            u = pg.url or ""
            if "ieeexplore" in u:
                break
            print("  [wait saml] %s" % u[:100], flush=True)
        html = content(pg)
        print("[ieee url]", pg.url[:130], flush=True)
        print("[has Zhejiang]", "Zhejiang" in html, flush=True)
        dump_page(pg, "after_login")
        if "ieeexplore" not in (pg.url or ""):
            return "ERR"
        try:
            pg.context.storage_state(path=STATE_OUT)
            print("[state saved]", STATE_OUT, os.path.getsize(STATE_OUT), "bytes", flush=True)
        except Exception as e:
            print("  state-err", str(e)[:80], flush=True)
        probe_pdf(pg)
        return "OK"


def main():
    for user, pwd in CRED_LIST:
        for attempt in range(2):  # 同一凭据最多两轮（第一轮可能锁定等待后 DIRTY）
            print("== cred %s attempt %d ==" % (user, attempt), flush=True)
            try:
                v = run_cred(user, pwd)
            except Exception as e:
                print("  run-err", str(e)[:140], flush=True)
                v = "ERR"
            print("== verdict %s -> %s ==" % (user, v), flush=True)
            if v == "OK":
                print("SESSION_DONE", flush=True)
                return 0
            if v == "WRONG":
                break  # 换下一凭据
            if v == "DIRTY":
                print("  cooldown 600s before next clean try", flush=True)
                time.sleep(600)
            else:
                time.sleep(120)
    print("SESSION_FAIL_ALL", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
