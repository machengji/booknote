# -*- coding: utf-8 -*-
"""外文采集清单 全量 PDF 下载器（断点续传、按库限速）。

用法:
  python download_all.py --out F:\\外文PDF下载 --only-feasible
  python download_all.py --out F:\\外文PDF下载 --dbs "J-STAGE,SpringerLink" --limit 10
"""
from __future__ import annotations
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

import requests
import urllib3
from openpyxl import load_workbook

for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
    os.environ.pop(k, None)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = Path(__file__).resolve().parent
SRC_XLSX = Path(os.environ.get("BOOKNOTE_LIST", str(Path(DATA) / "外文采集清单.xlsx")))
FEAS_CSV = Path(os.environ.get("BOOKNOTE_FEAS", str(BASE / "feasibility.csv")))
DEFAULT_OUT = Path(PDFS)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
DELAYS = {"ScienceDirect": 40.0, "J-STAGE": 3.0}   # 每库两次请求最小间隔(秒)，默认见 --delay
DEFAULT_DELAY = 0.4
DEFAULT_WORKERS = 4

_lock = Lock()


def log(msg: str, log_path: Path) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with _lock:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def domain(v) -> str:
    if not v:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    h = (urlparse(s).hostname or "").lower().strip(".")
    return h[4:] if h.startswith("www.") else h


def classify(h: str) -> str:
    h = (h or "").lower()
    if h == "jstage.jst.go.jp": return "J-STAGE"
    if h == "link.springer.com": return "SpringerLink"
    if h.endswith("biomedcentral.com"): return "BMC / BioMed Central"
    if h == "onlinelibrary.wiley.com" or h.endswith(".onlinelibrary.wiley.com"): return "Wiley Online Library"
    if h == "academic.oup.com": return "Oxford Academic"
    if h == "iopscience.iop.org": return "IOPscience"
    if h == "tandfonline.com": return "Taylor & Francis Online"
    if h in {"pmc.ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov"}: return "PubMed / PubMed Central"
    if h == "mdpi.com": return "MDPI"
    if h == "journals.sagepub.com": return "SAGE Journals"
    if h == "bioone.org": return "BioOne Complete"
    if h == "brill.com": return "Brill"
    if "ovid" in h: return "Ovid"
    if h == "ieeexplore.ieee.org": return "IEEE Xplore"
    if h == "muse.jhu.edu": return "Project MUSE"
    if h == "thieme-connect.de": return "Thieme Connect"
    if "ebsco" in h: return "EBSCOhost"
    if h == "pubs.aip.org": return "AIP Publishing"
    if h == "degruyterbrill.com": return "De Gruyter Brill"
    if h in {"jove.com", "jovecomapi-svc-main-service"}: return "JoVE"
    if h == "kjpp.net": return "Korean Journal of Physiology & Pharmacology"
    if h == "pubs.rsc.org": return "RSC Publishing"
    if h.startswith("scielo.") or h == "scielo.org": return "SciELO"
    if h == "opg.optica.org": return "Optica Publishing Group"
    if h == "asmedigitalcollection.asme.org": return "ASME Digital Collection"
    if h == "worldscientific.com": return "World Scientific"
    if h == "pubs.acs.org": return "ACS Publications"
    if "frontiersin.org" in h or "frontierspartnerships.org" in h: return "Frontiers"
    if h == "ascelibrary.org": return "ASCE Library"
    if h == "doiserbia.nb.rs": return "DOI Serbia"
    if "kglmeridian.com" in h: return "KGL Meridian"
    if h == "karger.com": return "Karger"
    if h == "cpn.or.kr": return "Clinical Psychopharmacology and Neuroscience"
    if h == "dl.begellhouse.com": return "Begell House"
    if h == "read.dukeupress.edu": return "Duke University Press"
    if h == "brieflands.com": return "Brieflands"
    if h == "journals.uchicago.edu": return "University of Chicago Press"
    if h == "emerald.com": return "Emerald Insight"
    if h == "dl.acm.org": return "ACM Digital Library"
    if h == "psycnet.apa.org": return "APA PsycNet"
    if h == "degruyter.com": return "De Gruyter"
    if h == "cdnsciencepub.com": return "Canadian Science Publishing"
    if h == "bjrbe-journals.rtu.lv": return "Baltic Journal of Road and Bridge Engineering"
    if h == "connectsci.au": return "ConnectSci"
    if h == "journals.ametsoc.org": return "AMS Journals"
    if h == "int-res.com": return "Inter-Research"
    if h == "ar.iiarjournals.org": return "Anticancer Research"
    if h in {"compass.astm.org", "store.astm.org"}: return "ASTM Compass"
    if h == "inderscienceonline.com": return "Inderscience"
    if h == "ezproxy.rice.edu": return "Rice University EZproxy"
    if h == "nature.com": return "Nature Portfolio"
    if h == "direct.mit.edu": return "MIT Press Direct"
    if h == "liebertpub.com": return "Mary Ann Liebert"
    if h == "eurekaselect.com": return "Bentham Science / EurekaSelect"
    if h == "library.imaging.org": return "Society for Imaging Science and Technology"
    if h in {"techno-press.com", "techno-press.org"}: return "Techno-Press"
    if h == "medicaljournalssweden.se": return "Medical Journals Sweden"
    if h == "journals.stfm.org": return "STFM Journals"
    if h == "thejns.org": return "Journal of Neurosurgery Publishing Group"
    if h == "sciencedirect.com": return "ScienceDirect"
    if h == "journals.lww.com": return "Lippincott Williams & Wilkins"
    if h == "navi.ion.org": return "Institute of Navigation"
    if h in {"public-pages-files-2025.ebm-journal.org", "ebm-journal.org"}: return "Experimental Biology and Medicine"
    if h == "content.ampp.org": return "Alliance of Medical Publishers"
    if h == "journals.humankinetics.com": return "Human Kinetics Journals"
    if h == "arc.aiaa.org": return "AIAA Aerospace Research Central"
    if h == "journals.aps.org": return "APS Journals"
    if h == "journals.biologists.com": return "The Company of Biologists"
    if h == "scholarlypublishingcollective.org": return "Scholarly Publishing Collective"
    if h == "online.ucpress.edu": return "University of California Press"
    if h == "molbiolcell.org": return "Molecular Biology of the Cell"
    if h == "aacrjournals.org": return "AACR Journals"
    if h == "microbiologyresearch.org": return "Microbiology Society"
    if h == "nopr.niscpr.res.in": return "NIScPR Online Periodicals"
    if h == "cdn.techscience.press": return "其他独立期刊/平台（cdn.techscience.press）"
    if h == "igi-global.com": return "IGI Global"
    if h == "pubs.geoscienceworld.org": return "GeoScienceWorld"
    if h == "journals.iucr.org": return "IUCr Journals"
    if h == "downloads.hindawi.com": return "Hindawi"
    if h == "journals.physiology.org": return "American Physiological Society"
    if h == "por-journal.com": return "其他独立期刊/平台（por-journal.com）"
    if h == "ijdb.ehu.eus": return "其他独立期刊/平台（ijdb.ehu.eus）"
    if h == "ijbms.mums.ac.ir": return "其他独立期刊/平台（ijbms.mums.ac.ir）"
    if h == "imim.pl": return "其他独立期刊/平台（imim.pl）"
    if h == "stet-review.org": return "其他独立期刊/平台（stet-review.org）"
    if h == "storage.forummmpub.com": return "其他独立期刊/平台（storage.forummmpub.com）"
    if h == "schweizerbart.de": return "其他独立期刊/平台（schweizerbart.de）"
    if h == "revistabiomedica.org": return "其他独立期刊/平台（revistabiomedica.org）"
    if h == "leprosyreview.org": return "其他独立期刊/平台（leprosyreview.org）"
    if h == "kovmat.sav.sk": return "其他独立期刊/平台（kovmat.sav.sk）"
    if h == "lmaleidykla.lt": return "其他独立期刊/平台（lmaleidykla.lt）"
    if h == "bp.ueb.cas.cz": return "其他独立期刊/平台（bp.ueb.cas.cz）"
    if h == "aimnet.it": return "其他独立期刊/平台（aimnet.it）"
    if h == "technology.matthey.com": return "其他独立期刊/平台（technology.matthey.com）"
    if h == "comptes-rendus.academie-sciences.fr": return "其他独立期刊/平台（comptes-rendus.academie-sciences.fr）"
    if h == "koreascience.or.kr": return "其他独立期刊/平台（koreascience.or.kr）"
    if h == "periodicodimineralogia.com": return "其他独立期刊/平台（periodicodimineralogia.com）"
    if h == "kjmm.org": return "其他独立期刊/平台（kjmm.org）"
    if h == "journalssystem.com": return "其他独立期刊/平台（journalssystem.com）"
    if h == "learnmem.cshlp.org": return "其他独立期刊/平台（learnmem.cshlp.org）"
    return f"其他独立期刊/平台（{h or '无域名'}）"


def safe_db(db: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", db).strip()[:80]


def is_pdf(b: bytes) -> bool:
    return b[:5] == b"%PDF-"


def is_complete_pdf(path: Path) -> bool:
    """校验文件是完整 PDF：文件头 %PDF- 且文件尾包含 %%EOF 标记。"""
    try:
        size = path.stat().st_size
        if size < 8:
            return False
        with path.open("rb") as f:
            head = f.read(5)
            if head != b"%PDF-":
                return False
            f.seek(max(0, size - 2048))
            tail = f.read()
        return b"%%EOF" in tail
    except Exception:
        return False


def alternatives(db: str, url: str):
    a = []
    if db == "SAGE Journals" and "/doi/reader/" in url:
        a.append(url.replace("/doi/reader/", "/doi/pdf/"))
    if db == "IEEE Xplore":
        m = re.search(r"arnumber=(\d+)", url)
        if m:
            a.append(f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={m.group(1)}&ref=")
    if db == "Wiley Online Library" and "/doi/pdf/" in url:
        a.append(url.replace("/doi/pdf/", "/doi/pdfdirect/"))
    if db == "Taylor & Francis Online" and "/doi/epdf/" in url:
        a.append(url.replace("/doi/epdf/", "/doi/pdf/"))
    if url.startswith("http://www.thieme-connect.de/"):
        a.append(url.replace("http://", "https://", 1))
    if db == "MDPI":
        m = re.search(r"(https://www\.mdpi\.com/[^\s?]+)", url)
        if m:
            a.append(m.group(1))
    if db == "JoVE":
        m = re.search(r"api/article/pdf/(\d+)", url)
        if m:
            aid = m.group(1)
            a.append(f"https://www.jove.com/api/article/pdf/{aid}")
    if db == "BMC / BioMed Central":
        m = re.search(r"https?://([a-z0-9-]+)\.biomedcentral\.com/counter/pdf/(10\.\S+)", url)
        if m:
            a.append(f"https://{m.group(1)}.biomedcentral.com/articles/{m.group(2)}")
    return a


def invalid_reason(url: str) -> str:
    low = url.lower()
    host = (urlparse(url).hostname or "").lower()
    if "/articles//pdf/" in low or low.endswith("/pdf/.pdf"):
        return "PMC链接缺少PMCID/路径不完整"
    if not host or "." not in host:
        return "URL域名无效或为内部服务地址"
    return ""


def download_one(session, db: str, url: str, out_path: Path, delay: float):
    reason = invalid_reason(url)
    if reason:
        return {"ok": False, "size": 0, "error": reason, "actual_url": url}
    tries = [url] + alternatives(db, url)
    last_err = ""
    for u in tries:
        for attempt in range(3):
            time.sleep(delay)
            try:
                r = session.get(u, timeout=(8, 30), allow_redirects=True, verify=False, headers={
                    "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
                    "Referer": f"https://{urlparse(u).hostname or ''}/",
                })
                b = r.content or b""
                if r.status_code == 200 and is_pdf(b):
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = out_path.with_suffix(".part")
                    tmp.write_bytes(b)
                    tmp.replace(out_path)
                    return {"ok": True, "size": len(b), "md5": hashlib.md5(b).hexdigest(),
                            "error": "", "actual_url": u}
                last_err = f"HTTP {r.status_code} CT={r.headers.get('Content-Type', '')[:40]}"
            except Exception as e:
                last_err = str(e)[:200]
    return {"ok": False, "size": 0, "error": last_err or "unknown", "actual_url": url}


def download_one_curl(session, db: str, url: str, out_path: Path, delay: float):
    """用 curl.exe 下载（远程机器上 requests 被重置连接，curl 可用）。"""
    reason = invalid_reason(url)
    if reason:
        return {"ok": False, "size": 0, "error": reason, "actual_url": url}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tries = [url] + alternatives(db, url)
    curl = r"C:\Windows\System32\curl.exe"
    if not Path(curl).exists():
        curl = "curl"
    last_err = ""
    for u in tries:
        tmp = out_path.with_suffix(".part")
        # 单 URL 最多尝试 6 次（网络抖动如 J-STAGE exit 56 需要多次续传机会）；
        # 空响应/非 PDF 响应是最终结果，第 1 次即 break，不影响提速
        for attempt in range(6):
            time.sleep(delay)
            try:
                curl_args = [curl, "-s", "-L", "-k", "--connect-timeout", "15",
                        "--max-time", "300", "-A",
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                        "-e", f"https://{urlparse(u).hostname or ''}/"]
                if attempt > 0 and tmp.exists() and tmp.stat().st_size > 0 and is_pdf(tmp.read_bytes()[:5]):
                    curl_args += ["-C", "-"]  # PDF 断点续传
                elif attempt > 0 and tmp.exists():
                    try:
                        tmp.unlink()  # 非 PDF 断点（如 HTML 页）删掉重新下载
                    except Exception:
                        pass
                curl_args += ["-o", str(tmp), u]
                p = subprocess.run(curl_args, capture_output=True, timeout=140)
                if p.returncode == 0 and tmp.exists() and is_complete_pdf(tmp):
                    tmp.replace(out_path)
                    return {"ok": True, "size": out_path.stat().st_size,
                            "md5": hashlib.md5(out_path.read_bytes()).hexdigest(),
                            "error": "", "actual_url": u}
                if tmp.exists() and tmp.stat().st_size == 0:
                    # 空响应（如 IEEE stamp.jsp 返回 HTTP 202 无内容）是最终结果
                    last_err = f"curl exit={p.returncode} size=0"
                    break
                if tmp.exists() and not is_pdf(tmp.read_bytes()[:5]):
                    # 非 PDF（HTML 登录页/反爬页）重试无意义，快速失败
                    last_err = f"curl exit={p.returncode} 非PDF内容 size={tmp.stat().st_size}"
                    try:
                        tmp.unlink()
                    except Exception:
                        pass
                    break
                last_err = f"curl exit={p.returncode} size={tmp.stat().st_size if tmp.exists() else 0}"
            except Exception as e:
                last_err = str(e)[:200]
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
    return {"ok": False, "size": 0, "error": last_err or "unknown", "actual_url": url}


def host_ok(url: str, curl: str) -> bool:
    """连通性预检：能收到 HTTP 响应头即视为可达（响应慢/403 不影响，仅 000 判不可达）。"""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return True
    probe = f"https://{host}/"
    try:
        p = subprocess.run(
            [curl, "-s", "-o", "NUL", "-k", "--connect-timeout", "15",
             "--max-time", "25", "-w", "%{http_code}", "-A", UA, probe],
            capture_output=True, timeout=40)
        code = p.stdout.decode("utf-8", "replace").strip()
        return code not in ("", "000")
    except Exception:
        return False


def load_feasible_dbs() -> set:
    dbs = set()
    if not FEAS_CSV.exists():
        return dbs
    with FEAS_CSV.open("r", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("结论") in ("可行", "部分可行"):
                dbs.add(r["数据库/平台"])
    return dbs


def load_records(only_dbs=None, limit=0):
    wb = load_workbook(SRC_XLSX, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = ws.iter_rows(values_only=True)
    headers = [str(c or "").strip() for c in next(rows)]
    idx = {h: i for i, h in enumerate(headers)}
    grouped = defaultdict(list)
    count = 0
    for row in rows:
        pid = str(row[idx["ID"]] or "").strip()
        url = str(row[idx["PdfURL"]] or "").strip()
        if not url:
            continue
        db = classify(domain(url))
        if only_dbs and db not in only_dbs:
            continue
        grouped[db].append({
            "id": pid, "db": db, "url": url,
            "title": str(row[idx["Title"]] or "").strip() if "Title" in idx else "",
            "doi": str(row[idx["DOI"]] or "").strip() if "DOI" in idx else "",
        })
        count += 1
        if limit and count >= limit:
            break
    wb.close()
    return grouped


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--dbs", default="", help="逗号分隔的数据库名，空=全部")
    ap.add_argument("--dbs-file", default="", help="每行一个数据库名的文件")
    ap.add_argument("--only-feasible", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="测试用：最多处理记录数")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    args = ap.parse_args()

    if not SRC_XLSX.exists():
        sys.exit(f"找不到清单文件: {SRC_XLSX}")

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    log_path = out_root / "下载日志.txt"
    state_path = out_root / "进度.json"
    result_csv = out_root / "下载结果.csv"

    only_dbs = None
    db_order = None
    if args.dbs:
        only_dbs = set(x.strip() for x in args.dbs.split(",") if x.strip())
    if args.dbs_file:
        fp = Path(args.dbs_file)
        if not fp.exists():
            sys.exit(f"找不到数据库名单文件: {fp}")
        names = [x.strip() for x in fp.read_text(encoding="utf-8").splitlines() if x.strip()]
        only_dbs = set(names) if only_dbs is None else (only_dbs & set(names))
        db_order = names
        log(f"按名单文件下载，共 {len(only_dbs)} 个库", log_path)
    if args.only_feasible:
        feas = load_feasible_dbs()
        only_dbs = feas if only_dbs is None else (only_dbs & feas)
        log(f"仅下载可行/部分可行库，共 {len(only_dbs)} 个", log_path)

    log(f"读取清单: {SRC_XLSX} (limit={args.limit or '全部'})", log_path)
    grouped = load_records(only_dbs, args.limit)
    if db_order:
        grouped = {db: grouped[db] for db in db_order if db in grouped}
    total = sum(len(v) for v in grouped.values())
    log(f"共 {len(grouped)} 个库 / {total} 条记录", log_path)

    if result_csv.exists():
        result_csv.unlink()
    with result_csv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ID", "数据库/平台", "Title", "DOI", "PdfURL", "下载状态", "文件大小", "文件MD5", "失败原因", "本地路径", "检测时间"])
        w.writeheader()

    session = requests.Session()
    session.verify = False
    session.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})

    done = ok = 0
    started = datetime.now().isoformat(timespec="seconds")

    def write_result(rec, res):
        with result_csv.open("a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["ID", "数据库/平台", "Title", "DOI", "PdfURL", "下载状态", "文件大小", "文件MD5", "失败原因", "本地路径", "检测时间"])
            w.writerow({
                "ID": rec["id"], "数据库/平台": rec["db"], "Title": rec["title"],
                "DOI": rec["doi"], "PdfURL": rec["url"],
                "下载状态": "ok" if res["ok"] else "fail",
                "文件大小": res["size"], "文件MD5": res.get("md5", ""),
                "失败原因": res["error"],
                "本地路径": str(out_root / safe_db(rec["db"]) / f"{rec['id']}.pdf") if res["ok"] else "",
                "检测时间": datetime.now().isoformat(timespec="seconds"),
            })

    done_path = out_root / "done_dbs.json"
    done_dbs = set()
    if done_path.exists():
        try:
            done_dbs = set(json.loads(done_path.read_text(encoding="utf-8")))
        except Exception:
            done_dbs = set()
    if done_dbs:
        log(f"跳过上轮已完成数据库: {sorted(done_dbs)}", log_path)
    pending = list(grouped.items())
    retried = set()
    while pending:
        db, recs = pending.pop(0)

        if db in done_dbs:
            log(f"===== {db}：上轮已完成，跳过 =====", log_path)
            continue
        db_dir = out_root / safe_db(db)
        todo = []
        skipped = 0
        for rec in recs:
            target = db_dir / f"{rec['id']}.pdf"
            if target.exists() and target.stat().st_size > 0:
                if is_complete_pdf(target):
                    skipped += 1
                    continue
                try:
                    target.unlink()
                except Exception:
                    pass
            todo.append((rec, target))
        # 连通性预检：主机连不上直接整库跳过，避免每条约 96 秒空耗
        if todo:
            curl = r"C:\Windows\System32\curl.exe"
            if not Path(curl).exists():
                curl = "curl"
            if not host_ok(todo[0][0]["url"], curl):
                if db not in retried:
                    retried.add(db)
                    pending.append((db, recs))
                    log(f"!!!!! {db} 主机暂不可达，已放回队列末尾，稍后自动重试", log_path)
                    continue
                log(f"!!!!! {db} 重试后主机仍不可达，整库跳过（网络恢复后重跑可下）", log_path)
                for rec, _target in todo:
                    done += 1
                    res = {"ok": False, "size": 0, "error": "主机不可达(connect timeout)", "actual_url": rec["url"]}
                    write_result(rec, res)
                continue
        log(f"===== {db}：{len(recs)} 条（已存在 {skipped}，待下 {len(todo)}）=====", log_path)
        delay = DELAYS.get(db, args.delay)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(download_one_curl, session, db, rec["url"], target, delay): (rec, target) for rec, target in todo}
            for fut in as_completed(futs):
                rec, target = futs[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = {"ok": False, "size": 0, "error": str(e)[:200], "actual_url": rec["url"]}
                done += 1
                ok += 1 if res["ok"] else 0
                write_result(rec, res)
                if done % 20 == 0:
                    state = {"started": started, "updated": datetime.now().isoformat(timespec="seconds"),
                             "total": total, "done": done, "ok": ok, "fail": done - ok,
                             "last_db": db, "last_id": rec["id"], "last_result": "ok" if res["ok"] else res["error"]}
                    with state_path.open("w", encoding="utf-8") as f:
                        json.dump(state, f, ensure_ascii=False, indent=2)
                log(f"[{done}/{total}] {rec['id']} {'OK' if res['ok'] else 'FAIL'} {res.get('error', '')}", log_path)
        done_dbs.add(db)
        try:
            done_path.write_text(json.dumps(sorted(done_dbs)), encoding="utf-8")
        except Exception:
            pass

    state = {"started": started, "updated": datetime.now().isoformat(timespec="seconds"),
             "total": total, "done": done, "ok": ok, "fail": done - ok, "finished": True}
    with state_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    log(f"===== 完成：{ok}/{done} 成功，输出目录 {out_root} =====", log_path)


if __name__ == "__main__":
    main()
