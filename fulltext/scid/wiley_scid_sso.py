# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# 用 scid 账号 + Camoufox SSO 重建 Wiley 授权（停 SAGE 释放账号后运行）
# 流程: scid 重新登录 -> 打开 Wiley 条目(classid=179) -> JS auto 跳 wileyN.php -> Wiley authenticateSharedSP -> 过CF -> iam -> 验证 pdfdirect
import sys, re, time, pickle, os, requests, urllib3
sys.stdout=open(sys.stdout.fileno(),mode='w',encoding='utf-8',buffering=1)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from camoufox.sync_api import Camoufox
BASE="https://www.scidownload.com"; CHAIN="http://127.0.0.1:10889"
SESSION_PKL=_os.path.join(FT, r"wiley_auth_session.pkl")
DOI="10.1111/ablj.70014"
ENTRY="https://www.scidownload.com/e/action/ShowInfo.php?classid=179&id=2813"  # SK -> wiley4.php
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
def _content(pg):
    try: return pg.content() or ""
    except Exception: return ""

# 1) scid 重新登录（无验证码）
s=requests.Session(); s.headers.update({"User-Agent":UA})
s.get(BASE+"/e/member/login/",timeout=20,verify=False)
s.post(BASE+"/e/member/doaction.php",data={"fmdo":"login","dopost":"login_old","enews":"login","ecmsfrom":"/yingwenku/","tobind":"0","username":_os.environ.get("SCID_USER") or "","password":_os.environ.get("SCID_PWD") or "","lifetime":"315360000","Submit":"登 录"},timeout=25,verify=False,allow_redirects=True)
print("scid login cookies:",sorted(c.name for c in s.cookies),flush=True)
cookies=None; ua=None
with Camoufox(headless=False, proxy={"server":CHAIN}) as b:
    pg=b.new_page()
    try: pg.context.add_cookies([{"name":c.name,"value":c.value,"domain":".scidownload.com","path":"/"} for c in s.cookies])
    except Exception as e: print("addc",e,flush=True)
    pg.goto(ENTRY,timeout=45000,wait_until="domcontentloaded",referer=BASE+"/yingwenku/")
    print("entry:",(pg.url or "")[:60],flush=True); time.sleep(3)
    if pg.query_selector('form'):
        try: pg.evaluate("()=>{const f=document.querySelector('form'); if(f) f.submit();}")
        except Exception: pass
    # 等 wiley 域
    landed=False
    for i in range(40):
        time.sleep(3)
        u=pg.url or ""; c=_content(pg)
        ck=sorted({c["name"] for c in pg.context.cookies()})
        if "wiley.com" in u:
            print(f"  wiley SSO url={u[:85]} ck={ck[:10]}",flush=True)
            landed=True
            # 等 CF 过 + iam
            for j in range(50):
                time.sleep(3)
                u=pg.url or ""; c=_content(pg)
                names={c["name"] for c in pg.context.cookies()}
                if "just a moment" not in c.lower() and len(c)>3000 and any("iam" in n for n in names):
                    print(f"  >>> iam OK t={j*3}s url={u[:80]}",flush=True); break
            break
        if i%3==0: print(f"  t={(i+1)*3}s url={u[:75]}",flush=True)
    if not landed: print("NOT landed on wiley",flush=True); b.close(); sys.exit(1)
    # 强导航到真实文章 abs 页触发 iam
    try: pg.goto("https://onlinelibrary.wiley.com/doi/"+DOI,timeout=60000,wait_until="load")
    except Exception as e: print("abs err",str(e)[:50],flush=True)
    for i in range(30):
        time.sleep(2)
        c=_content(pg)
        if "just a moment" not in c.lower() and len(c)>3000: break
    time.sleep(3)
    cookies=[{"name":c["name"],"value":c["value"],"domain":c.get("domain",""),"path":c.get("path","/")} for c in pg.context.cookies()]
    ua=pg.evaluate("()=>navigator.userAgent")
    b.close()
names=[c["name"] for c in cookies]
print("final cookies:",len(cookies),names,flush=True)
if any("iam" in n for n in names):
    pickle.dump({"cookies":cookies,"ua":ua},open(SESSION_PKL,"wb"))
    print("SESSION SAVED (iam present)",flush=True)
else:
    print("NO iam",flush=True)
# 验证下载多个真实 DOI
jar=requests.cookies.RequestsCookieJar()
for c in cookies:
    for dom in (c.get("domain") or ".wiley.com",".wiley.com",".onlinelibrary.wiley.com"):
        try: jar.set(c["name"],c["value"],domain=dom,path=(c["path"] or "/"))
        except Exception: pass
for doi in ["10.1111/ablj.70013","10.1111/ablj.70014","10.1002/cind.70107"]:
    try:
        r=requests.get("https://onlinelibrary.wiley.com/doi/pdfdirect/"+doi,headers={"User-Agent":ua,"Accept":"application/pdf"},cookies=jar,proxies={"http":CHAIN,"https":CHAIN},timeout=45,verify=False)
        b=r.content
        print(f"  {doi} st={r.status_code} len={len(b)} pdf@={b.find(b'%PDF')}",flush=True)
    except Exception as e: print("  ERR",doi,str(e)[:50],flush=True)
