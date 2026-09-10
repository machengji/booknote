# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# scidownload 各库通用全自动下载器（机制=有头链浏览器随入口JS进发布商 -> 抓授权cookie -> requests链批量）
# 用法:
#   python pub_gateway_batch.py <lib> session   # 建立该库发布商会话(存 {lib}_auth.pkl)，需真DOI向
#   python pub_gateway_batch.py <lib> bulk      # 复用会话批量下 pdfs(scidownload)/<lib>/
# 库名: sage tf iop oxford aip bioone muse
import re, sys, os, time, random, pickle, requests, urllib3
sys.stdout=open(sys.stdout.fileno(),mode='w',encoding='utf-8',buffering=1)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from camoufox.sync_api import Camoufox

BASE="https://www.scidownload.com"
CHAIN="http://127.0.0.1:10889"
OUT_ROOT=_os.path.join(PDFS, r"scidownload")
SCID=_os.path.join(FT, r"scid_session.pkl")

# 库配置: entry=(classid,ShowInfo id), hosts=发布商域名正则(判定已进入), pdf=候选URL生成器, prefix=DOI前缀(过滤)
PUBLISHERS={
    "sage":  {"entry":(197,2834),"hosts":r"journals\.sagepub\.com",
              "pdf":lambda d:["https://journals.sagepub.com/doi/pdf/"+d,"https://journals.sagepub.com/doi/pdfdirect/"+d],
              "prefix":"10.1177/"},
    "tf":    {"entry":(204,5462),"hosts":r"tandfonline\.com",
              "pdf":lambda d:["https://www.tandfonline.com/doi/pdf/"+d,"https://www.tandfonline.com/doi/pdfdirect/"+d],
              "prefix":"10.1080/"},
    "iop":   {"entry":(193,3844),"hosts":r"iopscience\.iop\.org|sjuku\.top",
              "pdf":lambda d:["https://iopscience.iop.org/article/"+d+"/pdf"],
              "prefix":"10.1088/"},
    "oxford":{"entry":(200,5336),"hosts":r"academic\.oup\.com|oup\.com",
              "pdf":lambda d:["https://academic.oup.com/doi/pdf/"+d,"https://academic.oup.com/doi/pdfdirect/"+d],
              "prefix":"10.1093/"},
    "aip":   {"entry":(213,5347),"hosts":r"pubs\.aip\.org|scitation\.aip\.org",
              "pdf":lambda d:["https://pubs.aip.org/doi/pdf/"+d,"https://pubs.aip.org/doi/abs/"+d],
              "prefix":"10.1063/"},
    "bioone":{"entry":(239,5422),"hosts":r"bioone\.org|meridian\.allenpress\.com",
              "pdf":lambda d:["https://bioone.org/journals/"+d.replace("/","_").replace(".","_")+"/article-pdf/"],
              "prefix":"10.1656/"},
    "muse":  {"entry":(239,5585),"hosts":r"muse\.jhu\.edu",
              "pdf":lambda d:["https://muse.jhu.edu/article/"+d+"/pdf"],
              "prefix":"10.1353/"},
}
DOIS_DIR=FT

def fn(d): return d.replace("/","_").replace(".","_").replace(":","_")
def load_dois(lib):
    p=DOIS_DIR+"/"+lib+"_dois.txt"
    if not os.path.exists(p): return []
    pref=PUBLISHERS[lib]["prefix"]
    out=[]
    for l in open(p,encoding="utf-8"):
        l=l.strip()
        if not l: continue
        if pref and not l.startswith(pref): continue
        out.append(l)
    return out

def phase_session(lib):
    cfg=PUBLISHERS[lib]; cid,eid=cfg["entry"]
    pkl=f_os.path.join(FT, r"{lib}_auth.pkl")
    if os.path.exists(pkl): os.remove(pkl)
    scid=pickle.load(open(SCID,"rb"))
    os.makedirs(OUT_ROOT,exist_ok=True)
    entry=f"{BASE}/e/action/ShowInfo.php?classid={cid}&id={eid}"
    try:
        with Camoufox(headless=False, proxy={"server":CHAIN}) as b:
            pg=b.new_page()
            try: pg.context.add_cookies([{"name":c.name,"value":c.value,"domain":".scidownload.com","path":"/"} for c in scid.cookies])
            except Exception as e: print("addc",e)
            pg.goto(entry,timeout=60000,wait_until="domcontentloaded",referer=BASE+"/yingwenku/")
            print("entry url:",(pg.url or "")[:80],flush=True)
            time.sleep(1)
            # 触发入口的JS跳转链(autosubmit/location/setTimeout)，循环等待直到进入发布商域名
            ok=False
            for i in range(90):  # 最多 ~4.5min
                try: u=pg.url or ""; c=pg.content() or ""
                except Exception: u=""; c=""
                if re.search(cfg["hosts"],u) and "just a moment" not in c.lower() and len(c)>1500:
                    ok=True; break
                # 若当前仍停在 scidownload，尝试触发跳转
                if "scidownload.com" in u or "downsci" in u or "sjuku" in u:
                    try:
                        if pg.query_selector("form[name=form1],form"): pg.evaluate("()=>{const f=document.querySelector('form[name=form1]'); if(f) f.submit(); else {const suf=document.querySelectorAll('form'); if(suf[0]) suf[0].submit();}}")
                    except Exception: pass
                if i%10==0: print(f"  wait#{i} url={u[:60]}",flush=True)
                time.sleep(3)
            print("landed:",ok,"url=",(pg.url or "")[:80],flush=True)
            if not ok:
                try: pg.screenshot(path=f_os.path.join(FT, r"{lib}_sessfail.png"))
                except Exception: pass
                return False
            time.sleep(4)
            cookies=[{"name":c["name"],"value":c["value"],"domain":c.get("domain",""),"path":c.get("path","/")} for c in pg.context.cookies()]
            ua=pg.evaluate("()=>navigator.userAgent")
            dom=re.search(r'https?://([^/]+)',pg.url or "")
            pubdom=dom.group(1) if dom else ""
            pickle.dump({"cookies":cookies,"ua":ua,"pubdom":pubdom},open(pkl,"wb"))
            print(f"SESS ok pubdom={pubdom} cookies={len(cookies)} ua={ua[:35]}",flush=True)
            return True
    except Exception as e:
        import traceback; traceback.print_exc()
        return False

def phase_bulk(lib):
    cfg=PUBLISHERS[lib]; pkl=f_os.path.join(FT, r"{lib}_auth.pkl")
    dois=load_dois(lib)
    if not os.path.exists(pkl): print("无会话,先session"); return
    if not dois: print("无DOI或全被前缀过滤"); return
    st=pickle.load(open(pkl,"rb")); cookies=st["cookies"]; ua=st["ua"]; pubdom=st.get("pubdom","")
    out_dir=os.path.join(OUT_ROOT,lib); os.makedirs(out_dir,exist_ok=True)
    jar=requests.cookies.RequestsCookieJar()
    doms=[".%s"%pubdom, pubdom, cfg["hosts"].replace("\\","")] if pubdom else [".com"]
    # 覆盖常见发布商域
    doms=[".sagepub.com",".tandfonline.com",".iopscience.iop.org",".oup.com",".academic.oup.com",".aip.org",".pubs.aip.org",".scitation.aip.org",".bioone.org",".muse.jhu.edu",".sjuku.top"]
    for c in cookies:
        for dom in doms:
            try: jar.set(c["name"],c["value"],domain=dom,path="/")
            except Exception: pass
    proxies={"http":CHAIN,"https":CHAIN}
    ok=skip=cf=html=err=0; t0=time.time()
    print(f"[bulk:{lib}] dois={len(dois)}",flush=True)
    for i,d in enumerate(dois,1):
        out=os.path.join(out_dir,fn(d)+".pdf")
        if os.path.exists(out) and os.path.getsize(out)>1024: skip+=1; continue
        got=None
        for u in cfg["pdf"](d):
            try:
                r=requests.get(u,headers={"User-Agent":ua,"Accept":"application/pdf"},cookies=jar,proxies=proxies,timeout=45,verify=False)
            except Exception as e:
                err+=1; break
            b=r.content
            if b[:4]==b"%PDF" or b[:5]==b"%PDF-":
                got=b; break
            if r.status_code in (403,429) or b"just a moment" in b[:3000].lower() or b"turnstile" in b[:2000].lower():
                cf+=1; break
            html+=1; break
        if got is not None:
            open(out,"wb").write(got); ok+=1
            if i%20==0: print(f"[{i}/{len(dois)}][ok] {d} {len(got)}",flush=True)
        elif i%20==0:
            print(f"[{i}/{len(dois)}] done-so-far ok={ok} skip={skip} cf={cf} html={html} err={err}",flush=True)
        time.sleep(0.2+random.uniform(0,0.3))
    print(f"[bulk:{lib}] DONE ok={ok} skip={skip} cf={cf} html={html} err={err} elapsed={time.time()-t0:.0f}s",flush=True)

if __name__=="__main__":
    lib=sys.argv[1] if len(sys.argv)>1 else "sage"
    phase=sys.argv[2] if len(sys.argv)>2 else "session"
    if lib not in PUBLISHERS: print("libs:",list(PUBLISHERS)); sys.exit(1)
    if phase=="session": phase_session(lib)
    elif phase=="bulk": phase_bulk(lib)
    else: print("pe=session|bulk")
