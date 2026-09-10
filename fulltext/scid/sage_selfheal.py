# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# SAGE 自愈批：检测 cf 连击 -> 进程内重建会话(链Camoufox) -> 继续下一DOI，无需重启批、无 pkl 文件竞态
import re, sys, os, time, random, pickle, requests, urllib3
sys.stdout=open(sys.stdout.fileno(),mode='w',encoding='utf-8',buffering=1)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from camoufox.sync_api import Camoufox
BASE="https://www.scidownload.com"; CHAIN="http://127.0.0.1:10889"
OUT=_os.path.join(PDFS, r"scidownload/sage")
DOIS_FILE=_os.path.join(FT, r"sage_dois.txt")
UA0="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
def getsession():
    s=requests.Session(); s.headers.update({"User-Agent":UA0})
    s.get(BASE+"/e/member/login/",timeout=20,verify=False)
    s.post(BASE+"/e/member/doaction.php",data={"fmdo":"login","dopost":"login_old","enews":"login","ecmsfrom":"/yingwenku/","tobind":"0","username":_os.environ.get("SCID_USER") or "","password":_os.environ.get("SCID_PWD") or "","lifetime":"315360000","Submit":"\u767b \u5f55"},timeout=25,verify=False,allow_redirects=True)
    with Camoufox(headless=False, proxy={"server":CHAIN}) as b:
        pg=b.new_page()
        try: pg.context.add_cookies([{"name":c.name,"value":c.value,"domain":".scidownload.com","path":"/"} for c in s.cookies])
        except Exception: pass
        pg.goto(BASE+"/e/action/ShowInfo.php?classid=197&id=2834",timeout=60000,wait_until="domcontentloaded",referer=BASE+"/yingwenku/")
        ok=False
        for i in range(60):
            try: u=pg.url or ""; c=pg.content() or ""
            except Exception: u=""; c=""
            if re.search(r"journals\.sagepub\.com",u) and "just a moment" not in c.lower() and len(c)>1500: ok=True; break
            time.sleep(3)
        if not ok:
            print("[sess] NEVER LANDED",flush=True); return None
        time.sleep(4)
        cookies=[{"name":c["name"],"value":c["value"],"domain":c.get("domain",""),"path":c.get("path","/")} for c in pg.context.cookies()]
        ua=pg.evaluate("()=>navigator.userAgent")
    jar=requests.cookies.RequestsCookieJar()
    for c in cookies:
        for dom in ["journals.sagepub.com",".sagepub.com"]:
            try: jar.set(c["name"],c["value"],domain=dom,path="/")
            except Exception: pass
    print("[sess] rebuilt cookies=%d ua=%s"%(len(cookies),ua[:30]),flush=True)
    return (jar, ua)
def fn(d): return d.replace("/","_").replace(".","_").replace(":","_")
def main():
    os.makedirs(OUT,exist_ok=True)
    dois=[l.strip() for l in open(DOIS_FILE,encoding="utf-8") if l.strip().startswith("10.1177/")]
    total=len(dois)
    if total==0: print("no dois"); return
    first=getsession()
    if not first: print("init session failed"); return
    jar, ua = first
    time.sleep(2)
    proxies={"http":CHAIN,"https":CHAIN}
    ok=skip=0; t0=time.time()
    cf_streak=0
    print("[main] total=%d"%total,flush=True)
    for i,d in enumerate(dois,1):
        out=os.path.join(OUT,fn(d)+".pdf")
        if os.path.exists(out) and os.path.getsize(out)>1024: skip+=1; continue
        got=None
        for u in ["https://journals.sagepub.com/doi/pdf/"+d,"https://journals.sagepub.com/doi/pdfdirect/"+d]:
            try:
                r=requests.get(u,headers={"User-Agent":ua,"Accept":"application/pdf"},cookies=jar,proxies=proxies,timeout=40,verify=False)
            except Exception as e:
                print(f"[{i}][err] {d} {str(e)[:40]}",flush=True); got="ERR"; break
            b=r.content
            if b[:4]==b"%PDF": got=b; cf_streak=0; break
            if r.status_code in (403,429) or b"just a moment" in b[:3000].lower() or b"turnstile" in b[:2000].lower():
                cf_streak+=1; got="CF"; break
            got="HTML"; cf_streak=0; break
        if got is not None and got!="ERR" and got!="CF" and got!="HTML":
            open(out,"wb").write(got); ok+=1
            if i%25==0: print(f"[{i}/{total}][ok] {d} {len(got)} ok={ok} skip={skip} cfstrk={cf_streak}",flush=True)
        elif i%25==0:
            print(f"[{i}/{total}] ok={ok} skip={skip} cfstrk={cf_streak}",flush=True)
        if cf_streak>=8:
            print(f"[{i}][rebuild] cf_streak={cf_streak} -> rebuild session",flush=True)
            nr=getsession()
            if nr:
                jar, ua = nr; cf_streak=0
            time.sleep(5)
        time.sleep(0.2+random.uniform(0,0.3))
    print(f"[main] DONE ok={ok} skip={skip} elapsed={time.time()-t0:.0f}s",flush=True)
if __name__=="__main__": main()
