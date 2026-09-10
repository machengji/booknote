# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# SAGE 全自动：scidownload 登录态 requests 取 sharedSP URL -> 住宅链有头 Camoufox 过 SSO -> 抓 .sagepub.com 授权 cookie -> requests 批量
import re, sys, os, time, pickle, requests, urllib3
sys.stdout=open(sys.stdout.fileno(),mode='w',encoding='utf-8',buffering=1)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from camoufox.sync_api import Camoufox
BASE="https://www.scidownload.com"
CHAIN="http://127.0.0.1:10889"
DOI_PDF=lambda d: ["https://journals.sagepub.com/doi/pdf/"+d, "https://journals.sagepub.com/doi/pdfdirect/"+d]
SSN_PKL=_os.path.join(FT, r"sage_auth_session.pkl")
os.makedirs(_os.path.join(PDFS, r"scidownload/sage"),exist_ok=True)
scid=pickle.load(open("scid_session.pkl","rb"))
scid.headers.update({"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36","Referer":BASE+"/yingwenku/"})
def get_sharedsp(lib_entry,cid,eid):
    r=scid.get(f"{BASE}/e/action/ShowInfo.php?classid={cid}&id={eid}",timeout=25,verify=False)
    t=r.text
    m=re.search(r'https?://[^\s"\']*authenticateSharedSP[^\s"\']*',t)
    if not m:
        m=re.search(r'https?://[^\s"\']*authenticateSharedSP[^\s"\']*',t)
    sp=m.group(0) if m else None
    if not sp:
        m2=re.search(r'location\.href\s*=\s*["\']([^"\']+)["\']',t)
        if m2: sp=m2.group(1)
    return sp, t
def build_session():
    sp,t=get_sharedsp("sage",197,2834)
    if not sp: print("no sharedsp. body:",t[:120]); return None
    print("sharedsp:",sp[:80],flush=True)
    with Camoufox(headless=False, proxy={"server":CHAIN}) as b:
        pg=b.new_page()
        try: pg.context.add_cookies([{"name":c.name,"value":c.value,"domain":".scidownload.com","path":"/"} for c in scid.cookies])
        except Exception as e: print("addc",e)
        pg.goto(sp,timeout=60000,wait_until="domcontentloaded",referer=BASE+"/yingwenku/"); time.sleep(5)
        # auto-submit sharedSP form if present
        if pg.query_selector('form[action*="authenticateSharedSP"],form#asder,form[id*=auth]'):
            try: pg.evaluate("()=>{const f=document.querySelector('form[action*=authenticateSharedSP]'); if(f) f.submit();}")
            except Exception: pass
        time.sleep(5)
        # wait until journals.sagepub.com and content beyond CF
        ok=False
        for i in range(50):
            try: c=pg.content() or ""; u=pg.url or ""
            except Exception: c=""; u=""
            if "journals.sagepub.com" in u and "just a moment" not in c.lower() and len(c)>2000:
                ok=True; break
            time.sleep(2)
        print("landed:",ok,"url=",(pg.url or "")[:70],flush=True)
        time.sleep(4)
        cookies=[{"name":c["name"],"value":c["value"],"domain":c.get("domain",""),"path":c.get("path","/")} for c in pg.context.cookies()]
        ua=pg.evaluate("()=>navigator.userAgent")
        pickle.dump({"cookies":cookies,"ua":ua},open(SSN_PKL,"wb"))
        print("saved",len(cookies),"cookies ua=",ua[:40],flush=True)
        b.close()
    return {"cookies":cookies,"ua":ua}
def test_batch(st):
    jar=requests.cookies.RequestsCookieJar()
    for c in st["cookies"]:
        for dom in (".sagepub.com",c.get("domain") or ".sagepub.com"):
            try: jar.set(c["name"],c["value"],domain=dom,path="/")
            except Exception: pass
    proxies={"http":CHAIN,"https":CHAIN}
    for doi in ["10.1177/00027642241240333","10.1177/00027642241240335","10.1177/2312006X241247658"]:
        got=False
        for u in DOI_PDF(doi):
            try:
                r=requests.get(u,headers={"User-Agent":st["ua"],"Accept":"application/pdf"},cookies=jar,proxies=proxies,timeout=45,verify=False)
                b=r.content
                print(f"  {doi} {u[-16:]} st={r.status_code} len={len(b)} head={b[:5]}",flush=True)
                if b[:4]==b"%PDF":
                    open(_os.path.join(PDFS, r"scidownload/sage/")+doi.replace("/","_").replace(".","_")+".pdf","wb").write(b); got=True; break
            except Exception as e:
                print("  ERR",doi,str(e)[:50],flush=True)
        print(("  OK" if got else "  FAIL"),doi,flush=True)
if __name__=="__main__":
    st=build_session()
    if st: test_batch(st)
