# -*- coding: utf-8 -*-
"""Isolated single-URL PDF download worker."""
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
import json, os, re, sys
from pathlib import Path
from urllib.parse import urlparse
import requests, urllib3
for k in ("SSL_CERT_FILE","REQUESTS_CA_BUNDLE","CURL_CA_BUNDLE"): os.environ.pop(k,None)
urllib3.disable_warnings()

def is_pdf(b): return b[:5]==b"%PDF-"
def alternatives(db,url):
    a=[]
    if db=="SAGE Journals" and "/doi/reader/" in url:a.append(url.replace("/doi/reader/","/doi/pdf/"))
    if db=="IEEE Xplore":
        m=re.search(r"arnumber=(\d+)",url)
        if m:a.append(f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={m.group(1)}&ref=")
    if db=="Wiley Online Library" and "/doi/pdf/" in url:a.append(url.replace("/doi/pdf/","/doi/pdfdirect/"))
    if db=="Taylor & Francis Online" and "/doi/epdf/" in url:a.append(url.replace("/doi/epdf/","/doi/pdf/"))
    if url.startswith("http://www.thieme-connect.de/"):a.append(url.replace("http://","https://",1))
    return a

def bad(url):
    h=(urlparse(url).hostname or "").lower(); low=url.lower()
    if "/articles//pdf/" in low or low.endswith("/pdf/.pdf"): return "PMC链接缺少PMCID/路径不完整"
    if not h or "." not in h:return "URL域名无效或为内部服务地址"
    return ""

def classify(r,b,err):
    if err:return ("timeout","请求超时") if "timeout" in err.lower() else ("request_error",err[:300])
    ct=r.headers.get("Content-Type",""); head=b[:8000].decode("utf-8","ignore").lower(); code=r.status_code
    if code==403:
        if any(x in head for x in ["cloudflare","just a moment","verify you are human"]):return "anti_bot","Cloudflare/人机验证拦截"
        return "forbidden","HTTP 403，权限不足或服务器拒绝"
    if code in (401,407):return "unauthorized",f"HTTP {code}，需要登录或代理认证"
    if code==404:return "not_found","HTTP 404，链接不存在"
    if code==429:return "rate_limited","HTTP 429，请求频率受限"
    if code>=400:return "http_error",f"HTTP {code}"
    if any(x in head for x in ["cloudflare","just a moment","verify you are human"]):return "anti_bot","返回人机验证页面"
    if "text/html" in ct.lower() or b"<html" in b[:1500].lower():return "html_page","链接返回HTML页面，不是PDF文件流"
    return "not_pdf",f"返回内容不是PDF，Content-Type={ct}，大小={len(b)}"

def main():
    task=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out={"status":"fail","class":"","http":0,"content_type":"","final_url":"","actual_url":task["url"],"size":0,"md5":"","error":"","local_path":""}
    reason=bad(task["url"])
    if reason:out.update({"class":"invalid_url","error":reason}); print(json.dumps(out,ensure_ascii=False)); return
    s=requests.Session(); s.verify=False; s.headers.update(task["headers"])
    for c in task.get("cookies",[]):
        try:s.cookies.set(c["name"],c["value"],domain=c.get("domain"),path=c.get("path","/"))
        except Exception:pass
    final_fail=None
    for u in [task["url"]]+alternatives(task["db"],task["url"]):
        out["actual_url"]=u
        try:
            r=s.get(u,timeout=(6,15),allow_redirects=True,verify=False,headers={"Accept":"application/pdf,application/octet-stream;q=0.9,*/*;q=0.8","Referer":f"https://{urlparse(u).hostname or ''}/"})
            b=r.content or b""; out.update({"http":r.status_code,"content_type":r.headers.get("Content-Type",""),"final_url":r.url})
            if r.status_code==200 and is_pdf(b):
                p=Path(task["out_path"]); p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(b)
                import hashlib
                out.update({"status":"ok","class":"pdf_success","size":len(b),"md5":hashlib.md5(b).hexdigest(),"error":"","local_path":str(p)})
                print(json.dumps(out,ensure_ascii=False)); return
            final_fail=classify(r,b,"")
        except Exception as e:final_fail=classify(None,b"",str(e))
    out.update({"class":final_fail[0] if final_fail else "unknown","error":final_fail[1] if final_fail else "未知失败"})
    print(json.dumps(out,ensure_ascii=False))
if __name__=="__main__":main()
