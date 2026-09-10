# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# SAGE 全自动：scidownload SSO 入口 + 住宅链 Camoufox 过CF建 .sagepub.com 授权会话 -> 抓cookie -> requests批量下
import re, sys, os, time, pickle, requests, urllib3
sys.stdout=open(sys.stdout.fileno(),mode='w',encoding='utf-8',buffering=1)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from camoufox.sync_api import Camoufox
BASE="https://www.scidownload.com"
HOSTS=r"journals\.sagepub\.com"
CHAIN="http://127.0.0.1:10889"
PROX={"server":CHAIN}
DOI="10.1177/00027642241240333"
OUT=_os.path.join(PDFS, r"scidownload/sage")
os.makedirs(OUT,exist_ok=True)
# scidownload login cookies
scid=pickle.load(open("scid_session.pkl","rb"))
cookie_list=[{"name":c.name,"value":c.value,"domain":".scidownload.com","path":"/"} for c in scid.cookies]
entry=BASE+"/e/action/ShowInfo.php?classid=197&id=2834"
print("=== SAGE auto session build ===",flush=True)
def sc(p):
    try: return p.content() or ""
    except Exception: return ""
with Camoufox(headless=False, proxy=PROX) as b:
    pg=b.new_page()
    try: pg.context.add_cookies(cookie_list)
    except Exception as e: print("add_cookies",e,flush=True)
    pg.goto(entry, timeout=60000, wait_until="domcontentloaded", referer=BASE+"/yingwenku/"); time.sleep(4)
    # look for a redirect script/form (window.location.href or submit) to reach the publisher
    arrived=False
    for i in range(120):
        u=pg.url or ""
        if re.search(HOSTS,u): arrived=True; break
        # attempt to trigger the SSO link/button: find a elements/buttons with sagepub/SSO
        try:
            lnk=pg.eval_on_selector_all("a,button,input[type=submit]", "els => { for(const e of els){ const t=(e.textContent||'')+' '+(e.getAttribute('href')||''); if(/sage|登录|进入|下载|auth/i.test(t)) return e.href||'click'; } return ''; }")
            if lnk=="": 
                # try submitting first form
                if pg.query_selector("form"): pg.evaluate("()=>document.querySelector('form').submit()")
        except Exception as e:
            pass
        if i%10==0: print(f"  wait #{i} url={u[:60]} title={(pg.title() or '')[:30]}",flush=True)
        time.sleep(3)
    print("arrived=",arrived,"url=",(pg.url or "")[:80],flush=True)
    if not arrived:
        try: pg.screenshot(path="sage_sso_fail.png")
        except Exception: pass
        b.close(); sys.exit(1)
    # wait CF clear on publisher
    cleared=False
    for i in range(40):
        c=sc(pg)
        if re.search(HOSTS,pg.url or "") and "just a moment" not in c.lower() and len(c)>2000: cleared=True; break
        time.sleep(2)
    print("cleared=",cleared,flush=True)
    time.sleep(4)
    # capture cookies + UA
    cookies_raw=[{"name":c["name"],"value":c["value"],"domain":c.get("domain",""),"path":c.get("path","/")} for c in pg.context.cookies()]
    ua=pg.evaluate("() => navigator.userAgent")
    pickle.dump({"cookies":cookies_raw,"ua":ua}, open("sage_auth_session.pkl","wb"))
    print("captured",len(cookies_raw),"cookies",flush=True)
    b.close()
# now requests batch test
print("=== requests batch test (same chain) ===",flush=True)
jar=requests.cookies.RequestsCookieJar()
for c in cookies_raw:
    for dom in (".sagepub.com", c.get("domain") or ".sagepub.com"):
        try: jar.set(c["name"],c["value"],domain=dom,path="/")
        except Exception: pass
proxies={"http":CHAIN,"https":CHAIN}
for doi in ["10.1177/00027642241240333","10.1177/00027642241240335"]:
    for u in ["https://journals.sagepub.com/doi/pdf/"+doi,"https://journals.sagepub.com/doi/pdfdirect/"+doi]:
        try:
            r=requests.get(u, headers={"User-Agent":ua,"Accept":"application/pdf"}, cookies=jar, proxies=proxies, timeout=45, verify=False)
            b=r.content
            print("  ",doi,u[-18:],"st=",r.status_code,"len=",len(b),"head=",b[:5],flush=True)
        except Exception as e:
            print("  ERR",doi,str(e)[:50],flush=True)
