# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# Oxford Academic 批量下载器：scid Oxford-gw SSO -> OUP cookie -> article-pdf 批量
# 用法: python oxford_download.py [--limit N] [--workers 2]
import sys, os, time, pickle, re, argparse, requests, urllib3, threading
sys.stdout=open(sys.stdout.fileno(),mode='w',encoding='utf-8',buffering=1)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from concurrent.futures import ThreadPoolExecutor, as_completed
from camoufox.sync_api import Camoufox

BASE="https://www.scidownload.com"; CHAIN="http://127.0.0.1:10889"
OUT=_os.path.join(PDFS, r"Oxford Academic")
DOIS_FILE=_os.path.join(FT, r"oxford_dois.txt")
SESS_PKL=_os.path.join(FT, r"oup_session.pkl")
_lock=threading.Lock()

def _content(pg):
    try: return pg.content() or ""
    except Exception: return ""

def scid_login():
    s=requests.Session()
    s.headers.update({"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"})
    s.get(BASE+"/e/member/login/",timeout=20,verify=False)
    s.post(BASE+"/e/member/doaction.php",data={"fmdo":"login","dopost":"login_old","enews":"login","ecmsfrom":"/yingwenku/","tobind":"0","username":_os.environ.get("SCID_USER") or "","password":_os.environ.get("SCID_PWD") or "","lifetime":"315360000","Submit":"登 录"},timeout=25,verify=False,allow_redirects=True)
    return s

def rebuild_session():
    """scid -> Oxford-gw SSO -> OUP cookie 保存到 SESS_PKL。返回 bool"""
    print("[sess] rebuilding OUP session...",flush=True)
    try:
        s=scid_login()
        with Camoufox(headless=False, proxy={"server":CHAIN}) as b:
            pg=b.new_page()
            try: pg.context.add_cookies([{"name":c.name,"value":c.value,"domain":".scidownload.com","path":"/"} for c in s.cookies])
            except: pass
            pg.goto(BASE+"/e/action/ShowInfo.php?classid=200&id=5336",timeout=30000,wait_until="domcontentloaded")
            time.sleep(4)
            if pg.query_selector('form'):
                try: pg.evaluate("()=>{const f=document.querySelector('form'); if(f) f.submit();}")
                except: pass
            # 等 SAML 回调设置 OUP_SessionId（zoup.php 上约 10s 出现）
            got_oup=False
            for i in range(10):
                time.sleep(2.5)
                names={c["name"] for c in pg.context.cookies()}
                if "OUP_SessionId" in names:
                    got_oup=True; print("[sess] OUP_SessionId set at t=%ds"%(i*2.5),flush=True); break
            if not got_oup:
                print("[sess] OUP_SessionId not set after wait",flush=True)
            # 导航到文章页触发完整授权
            try: pg.goto("https://academic.oup.com/doi/10.1093/ajcp/aqag021",timeout=60000,wait_until="domcontentloaded")
            except: pass
            for i in range(15):
                time.sleep(3)
                c=_content(pg)
                if "just a moment" not in c.lower() and len(c)>3000: break
            time.sleep(3)
            cookies=[{"name":c["name"],"value":c["value"],"domain":c.get("domain",""),"path":c.get("path","/")} for c in pg.context.cookies()]
            ua=pg.evaluate("()=>navigator.userAgent")
            b.close()
        if not any("SIGMA_USER_ID" in c["name"] or "cf_clearance" in c["name"] for c in cookies):
            print("[sess] NO auth cookie, rebuild failed",flush=True); return False
        pickle.dump({"cookies":cookies,"ua":ua},open(SESS_PKL,"wb"))
        print("[sess] OUP session rebuilt cookies=%d"%len(cookies),flush=True)
        # 用测试 DOI 验证能下载
        jar, ua2 = load_ctx()
        try:
            r=requests.get("https://academic.oup.com/ajcp/article-pdf/doi/10.1093/ajcp/aqag028/0/aqag028.pdf",
                headers={"User-Agent":ua2,"Accept":"application/pdf,*/*","Referer":"https://academic.oup.com/"},
                cookies=jar,proxies={"http":CHAIN,"https":CHAIN},timeout=45,verify=False)
            if r.content.find(b"%PDF")>=0:
                print("[sess] verify download OK len=%d"%len(r.content),flush=True)
                return True
            print("[sess] verify download NOT PDF st=%d"%r.status_code,flush=True); return False
        except Exception as e:
            print("[sess] verify ERR %s"%str(e)[:50],flush=True); return False
    except Exception as e:
        print("[sess] rebuild ERR %s"%str(e)[:50],flush=True)
        return False

def load_ctx():
    st=pickle.load(open(SESS_PKL,"rb"))
    jar=requests.cookies.RequestsCookieJar()
    for c in st["cookies"]:
        for dom in (c.get("domain") or ".academic.oup.com",".academic.oup.com",".oup.com"):
            try: jar.set(c["name"],c["value"],domain=dom,path="/")
            except: pass
    return jar, st["ua"]

def oup_url(doi):
    parts=doi.split("/")
    j=parts[1] if len(parts)>1 else "?"
    shortid=parts[-1]
    return f"https://academic.oup.com/{j}/article-pdf/doi/{doi}/0/{shortid}.pdf"

def fn(d): return d.replace("/","_").replace(".","_").replace(":","_")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--limit",type=int,default=0)
    ap.add_argument("--workers",type=int,default=2)
    ap.add_argument("--cf-exit",type=int,default=20)
    args=ap.parse_args()
    os.makedirs(OUT,exist_ok=True)
    dois=[l.strip() for l in open(DOIS_FILE,encoding="utf-8") if l.strip()]
    # 排除已知 404 的期刊（历史期刊/特殊端点）
    EXCLUDE={"ajhp","bfgp","biomtc","chemle","etojnl","jhps"}
    filtered=[d for d in dois if d.split("/")[1] not in EXCLUDE]
    if len(filtered)<len(dois):
        print("[filter] excluded %d (%s) left %d"%(len(dois)-len(filtered),",".join(EXCLUDE),len(filtered)),flush=True)
    dois=filtered
    if args.limit: dois=dois[:args.limit]
    # 初始会话（无 pkl 时也强制重建，避免 or 短路跳过 rebuild）
    if not rebuild_session():
        print("INIT SESSION FAILED"); return
    jar,ua=load_ctx()
    proxies={"http":CHAIN,"https":CHAIN}
    total=len(dois); done=ok=cf=err=0; cf_streak=0
    print("[main] total=%d workers=%d"%(total,args.workers),flush=True)
    def worker(d):
        out=os.path.join(OUT,fn(d)+".pdf")
        if os.path.exists(out) and os.path.getsize(out)>1000: return ("skip",d,0)
        try:
            r=requests.get(oup_url(d),headers={"User-Agent":ua,"Accept":"application/pdf,*/*","Referer":"https://academic.oup.com/"},
                           cookies=jar,proxies=proxies,timeout=45,verify=False)
            b=r.content
            if b.find(b"%PDF")>=0:
                with _lock: open(out,"wb").write(b)
                return ("ok",d,len(b))
            if r.status_code in (403,429) or b"just a moment" in b[:3000].lower():
                return ("cf",d,0)
            return ("html",d,r.status_code)
        except Exception as e:
            return ("err",d,str(e)[:25])
    while True:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs={ex.submit(worker,d):d for d in dois[done:done+args.workers*50]}
            for fut in as_completed(futs):
                r,d,info=fut.result(); done+=1
                if r=="ok":
                    ok+=1; cf_streak=0
                    if ok%25==0: print("[%d/%d] ok=%d cf=%d err=%d last=%s"% (done,total,ok,cf,err,d),flush=True)
                elif r=="skip": pass
                elif r=="cf":
                    cf+=1; cf_streak+=1
                    if cf_streak>=args.cf_exit:
                        print("[REBUILD] cf_streak=%d done=%d -> rebuild session"%(cf_streak,done),flush=True)
                        with _lock:
                            if rebuild_session(): jar,ua=load_ctx()
                            cf_streak=0
                elif r=="html":
                    err+=1; cf_streak=0
                else:
                    err+=1; cf_streak=0
                    if err%50==0: print("[err] %s %s"%(d,info),flush=True)
                if args.workers and done%args.workers==0:
                    time.sleep(0.4)
        if done>=total: break
    print("[DONE] ok=%d cf=%d err=%d"%(ok,cf,err),flush=True)

if __name__=="__main__":
    main()
