# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# Wiley 授权全自动批量：一次有头住宅链会话建授权 cookie -> requests 走同一住宅链批量下 pdfdirect
# 用法: python wiley_gateway_batch.py <dois_file> [--out DIR] [--sleep N]
import re, sys, os, time, random, requests, urllib3, pickle, argparse
sys.stdout=open(sys.stdout.fileno(),mode='w',encoding='utf-8',buffering=1)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
OUT_DEFAULT=_os.path.join(PDFS, r"Wiley Online Library")
SESSION_PKL=_os.path.join(FT, r"wiley_auth_session.pkl")
CHAIN="http://127.0.0.1:10889"
ACCEPT={"Accept":"application/pdf"}
def load_or_build_session():
    # if session exists, reuse; else build via headed chain browser
    if os.path.exists(SESSION_PKL):
        st=pickle.load(open(SESSION_PKL,"rb"))
        print("reuse session cookies:", [c["name"] for c in st["cookies"]][:8], flush=True)
        return st
    print("=== build auth session via headed chain browser ===",flush=True)
    import ddddocr
    from camoufox.sync_api import Camoufox
    BASE="http://www.wytsg.com"; CRED = {"username": _os.environ.get("WYTSG_USER") or "", "password": _os.environ.get("WYTSG_PWD") or ""}
    DIRECT={"firefox_user_prefs":{"network.proxy.type":0}}
    ocr=ddddocr.DdddOcr()
    DOI="10.1111/ablj.70014"
    def login(page):
        page.goto(BASE+"/e/member/login/", timeout=45000, wait_until="domcontentloaded"); time.sleep(3)
        for _ in range(20):
            try:
                if "无忧图书馆登录" in (page.title() or ""): break
            except Exception: pass
            time.sleep(1.5)
            try: page.reload(wait_until="domcontentloaded")
            except Exception: pass
        for a in range(8):
            try:
                if page.url!=BASE+"/e/member/login/":
                    page.goto(BASE+"/e/member/login/", wait_until="domcontentloaded"); time.sleep(2.5)
                cap=ocr.classification(page.context.request.get("http://www.wytsg.com/e/ShowKey/?v=login").body()).strip()
                page.fill("input[name=username]",CRED["username"]); page.fill("input[name=password]",CRED["password"]); page.fill("input[name=key]",cap)
                page.click("input[type=submit][name=ok]"); time.sleep(4)
                if "ujgpvmlauth" in {c["name"]:c["value"] for c in page.context.cookies()}: return True
            except Exception as e: time.sleep(2)
        return False
    # get docapi via direct browserA
    with Camoufox(headless=True, **DIRECT) as browserA:
        page=browserA.new_page()
        if not login(page): raise RuntimeError("wytsg login failed")
        rs=requests.Session()
        for c in page.context.cookies(): rs.cookies.set(c["name"],c["value"])
        rs.headers.update({"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"})
        xg=rs.get(BASE+"/xm/xg/wiley.php?doi="+DOI, headers={"Referer":BASE+"/e/action/ListInfo/?classid=239"}, timeout=20, verify=False)
        m=re.search(r'href=["\']?(https?://[^"\']*docapi[^"\']*)["\']?', xg.text)
        docapi=m.group(1) if m else None
        page.close()
    if not docapi: raise RuntimeError("no docapi")
    print("docapi:",docapi[-36:],flush=True)
    # headed chain browser build wiley auth
    from camoufox.sync_api import Camoufox as CF2
    with CF2(headless=False, proxy={"server":CHAIN}) as bB:
        pg=bB.new_page()
        pg.goto(docapi, timeout=45000, wait_until="domcontentloaded", referer=BASE+"/xm/xg/wiley.php?doi="+DOI); time.sleep(4)
        if pg.query_selector('form[action*="authenticateSharedSP"], form#asder'):
            pg.evaluate("() => { const f=document.querySelector('form'); if(f) f.submit(); }")
        time.sleep(6)
        for _ in range(12):
            time.sleep(2)
            if "onlinelibrary.wiley.com" in (pg.url or ""): break
        for i in range(40):
            try: c=pg.content() or ""
            except Exception: c=""
            if "onlinelibrary.wiley.com" in (pg.url or "") and "just a moment" not in c.lower() and len(c)>2000: break
            time.sleep(2)
        time.sleep(4)
        cookies_raw=[{"name":c["name"],"value":c["value"],"domain":c.get("domain",""),"path":c.get("path","/")} for c in pg.context.cookies()]
        ua=pg.evaluate("() => navigator.userAgent")
        st={"cookies":cookies_raw,"ua":ua}
        pickle.dump(st, open(SESSION_PKL,"wb"))
        print("session saved:",len(cookies_raw),"cookies, ua=",ua[:45],flush=True)
    return st
def out_path(out_dir, d):
    return os.path.join(out_dir, d.replace("/","_").replace(".","_").replace(":","_")+".pdf")
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("dois_file"); ap.add_argument("--out",default=OUT_DEFAULT); ap.add_argument("--sleep",type=float,default=0.3)
    ap.add_argument("--rebuild",action="store_true")
    args=ap.parse_args()
    out_dir=args.out
    os.makedirs(out_dir, exist_ok=True)
    if args.rebuild and os.path.exists(SESSION_PKL): os.remove(SESSION_PKL)
    st=load_or_build_session()
    cookies_raw=st["cookies"]; ua=st["ua"]
    dois=[l.strip() for l in open(args.dois_file,encoding="utf-8") if l.strip()]
    print("total DOIs:",len(dois),flush=True)
    proxies={"http":CHAIN,"https":CHAIN}
    jar=requests.cookies.RequestsCookieJar()
    for c in cookies_raw:
        for dom in (c.get("domain") or ".wiley.com", ".wiley.com", "agupubs.onlinelibrary.wiley.com", ".onlinelibrary.wiley.com"):
            if not dom: continue
            try: jar.set(c["name"],c["value"],domain=dom,path=(c["path"] or "/"))
            except Exception: pass
    ok=skip=cf=html=err=0; t0=time.time()
    for i,doi in enumerate(dois,1):
        out=out_path(out_dir, doi)
        if os.path.exists(out) and os.path.getsize(out)>1024: skip+=1; continue
        try:
            r=requests.get("https://onlinelibrary.wiley.com/doi/pdfdirect/"+doi, headers={"User-Agent":ua,"Accept":"application/pdf"}, cookies=jar, proxies=proxies, timeout=45, verify=False)
            b=r.content
            if b[:4]==b"%PDF" or b[:5]==b"%PDF-":
                open(out,"wb").write(b); ok+=1
                print(f"[{i}/{len(dois)}][ok] {doi} {len(b)}",flush=True)
            elif r.status_code in (403,429) or b"just a moment" in b[:3000].lower():
                cf+=1; print(f"[{i}/{len(dois)}][cf] {doi} st={r.status_code}",flush=True)
            else:
                html+=1
                if i%20==0: print(f"[{i}/{len(dois)}][html] {doi} st={r.status_code} len={len(b)}",flush=True)
        except Exception as e:
            err+=1; print(f"[{i}/{len(dois)}][err] {doi} {str(e)[:50]}",flush=True)
        if args.sleep: time.sleep(args.sleep+random.uniform(0,0.3))
    print(f"DONE ok={ok} skip={skip} cf={cf} html={html} err={err} elapsed={time.time()-t0:.0f}s",flush=True)
if __name__=="__main__":
    main()
