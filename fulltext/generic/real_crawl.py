# -*- coding: utf-8 -*-
"""
真实 PDF 采集
输入：id,pdf_url 对应表
流程：
  1) 可选登录浙大 CAS -> CARSI
  2) 按 URL 识别数据库并限速
  3) 真实下载 PDF
  4) 以 id 命名保存，并写 manifest 保证可回对

用法：
  python real_crawl.py --map data/sample_id_pdf_map.csv --limit 1
  python real_crawl.py --map data/prod_id_pdf_map.csv
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
import html as htmlmod
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import urllib3

# 清掉可能干扰 SSL 的本地 CA
for _k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
    os.environ.pop(_k, None)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = Path(__file__).resolve().parent
MAP_DIR = BASE / "data"
OUT = BASE / "out"
PDF_DIR = Path(PDFS)
LOG_DIR = Path(LOGS)
CFG_PATH = Path(os.environ.get("BOOKNOTE_ZJU_CFG") or str(Path(DATA) / "config.local.json"))
SEC_JS = Path(__file__).resolve().parent / "js_login_security.js"

LOGIN_ENTRY = (
    "https://ds.carsi.edu.cn/Shibboleth.sso/Login"
    "?entityID=https://idp.zju.edu.cn/idp/shibboleth"
    "&target=https://ds.carsi.edu.cn/resource/login.php"
)

# 按库限速（秒）
DEFAULT_DELAYS = {
    "sciencedirect": 40.0,
    "springer": 5.0,
    "acm": 5.0,
    "ieee": 5.0,
    "nature": 8.0,
    "geoscienceworld": 5.0,
    "wiley": 5.0,
    "acs": 5.0,
    "other": 5.0,
}

# 若 PDF 直链被拦，可先经 CARSI 进入对应 SP 建立会话
DB_RESOURCE_IDS = {
    "sciencedirect": "58",
    "springer": "9",
    "acm": "555",
    "ieee": "6",
    "nature": "8",
    "acs": "178",
}


def ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_dirs() -> None:
    for p in (MAP_DIR, OUT, PDF_DIR, LOG_DIR):
        p.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    cfg = {
        "username": os.environ.get("ZJU_USER", ""),
        "password": os.environ.get("ZJU_PASS", ""),
        "verify_ssl": False,
    }
    if CFG_PATH.exists():
        raw = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        cfg.update(raw or {})
    return cfg


def detect_database(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    path = (urlparse(url).path or "").lower()
    blob = host + path
    rules = [
        ("sciencedirect", ["sciencedirect.com", "elsevier.com"]),
        ("springer", ["link.springer.com", "springer.com"]),
        ("acm", ["dl.acm.org", "acm.org"]),
        ("ieee", ["ieeexplore.ieee.org", "ieee.org"]),
        ("nature", ["nature.com"]),
        ("geoscienceworld", ["geoscienceworld.org"]),
        ("wiley", ["onlinelibrary.wiley.com", "wiley.com"]),
        ("acs", ["pubs.acs.org", "acs.org"]),
        ("tandfonline", ["tandfonline.com"]),
        ("oup", ["academic.oup.com", "oup.com"]),
        ("sage", ["journals.sagepub.com", "sagepub.com"]),
    ]
    for name, keys in rules:
        if any(k in blob for k in keys):
            return name
    return "other"


def safe_id(s: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff\-.]+", "_", s.strip())
    return s[:120] or "unknown"


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def looks_like_pdf(content: bytes, content_type: str = "") -> bool:
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return True
    return content[:5] == b"%PDF-" or content[:8].startswith(b"%PDF")


def decode(s: str) -> str:
    return htmlmod.unescape(s)


def extract_form(html: str):
    am = re.search(r'<form[^>]*action="([^"]+)"', html, re.I)
    action = decode(am.group(1)) if am else None
    fields = {}
    for m in re.finditer(r"<input[^>]+>", html, re.I):
        tag = m.group(0)
        nm = re.search(r'name="([^"]+)"', tag, re.I)
        vm = re.search(r'value="([^"]*)"', tag, re.I)
        if nm:
            fields[decode(nm.group(1))] = decode(vm.group(1) if vm else "")
    return action, fields


def encrypt_password_node(password: str, modulus: str, exponent: str) -> str:
    if not SEC_JS.exists():
        raise RuntimeError(f"缺少 RSA 脚本: {SEC_JS}")
    script = f"""
const fs=require('fs');
global.window=global;
eval(fs.readFileSync({json.dumps(str(SEC_JS))},'utf8'));
const key=new RSAUtils.getKeyPair({json.dumps(exponent)},'',{json.dumps(modulus)});
const reversedPwd={json.dumps(password)}.split('').reverse().join('');
process.stdout.write(RSAUtils.encryptedString(key, reversedPwd));
"""
    p = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr or p.stdout or "node encrypt failed")
    return p.stdout.strip()


class RealCrawler:
    def __init__(self, username: str, password: str, verify_ssl: bool = False):
        self.username = username
        self.password = password
        self.verify = verify_ssl
        self.s = requests.Session()
        self.s.verify = verify_ssl
        self.s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        self.sp_ready: set[str] = set()
        self.last_request_at: dict[str, float] = {}

    def follow_saml_forms(self, html: str, max_hops: int = 8):
        last = None
        for _ in range(max_hops):
            if "SAMLResponse" in html or (
                "document.forms[0].submit()" in html and "<form" in html
            ):
                action, fields = extract_form(html)
                if not action:
                    break
                last = self.s.post(
                    action,
                    data=fields,
                    timeout=60,
                    allow_redirects=True,
                    verify=self.verify,
                )
                html = last.text
                continue
            break
        return last, html

    def login_carsi(self) -> None:
        if not self.username or not self.password:
            raise RuntimeError("缺少账号密码：请配置 config.local.json 或环境变量 ZJU_USER/ZJU_PASS")
        print("[login] CAS/CARSI ...")
        r = self.s.get(LOGIN_ENTRY, timeout=40, allow_redirects=True, verify=self.verify)
        m = re.search(r'name="execution"\s+value="([^"]+)"', r.text)
        if not m:
            raise RuntimeError("登录页未找到 execution，可能页面结构变化")
        execution = m.group(1)
        fa = re.search(r'<form[^>]+action="([^"]+)"', r.text)
        action = fa.group(1).replace("&amp;", "&") if fa else r.url
        post_url = urljoin(r.url, action)

        pk = self.s.get(
            "https://zjuam.zju.edu.cn/cas/v2/getPubKey",
            timeout=20,
            verify=self.verify,
        ).json()
        enc = encrypt_password_node(self.password, pk["modulus"], pk["exponent"])
        data = {
            "username": self.username,
            "password": enc,
            "authcode": "",
            "execution": execution,
            "_eventId": "submit",
            "rememberMe": "true",
        }
        ys = re.search(r'name="ys"\s+value="([^"]+)"', r.text)
        if ys:
            data["ys"] = ys.group(1)

        # captcha?
        kap = self.s.get(
            "https://zjuam.zju.edu.cn/cas/v2/getKaptchaStatus",
            timeout=15,
            verify=self.verify,
        )
        if str(kap.text).strip().lower() in ("true", "1", "yes"):
            raise RuntimeError("当前需要验证码，请先浏览器登录一次或稍后再试")

        r2 = self.s.post(
            post_url,
            data=data,
            timeout=40,
            allow_redirects=True,
            verify=self.verify,
            headers={
                "Referer": r.url,
                "Origin": "https://zjuam.zju.edu.cn",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if "cas/login" in r2.url and "SAMLResponse" not in r2.text:
            err = re.search(r'id="errormsg"[^>]*>([\s\S]*?)</p>', r2.text)
            msg = re.sub(r"<[^>]+>", "", err.group(1)).strip() if err else r2.url
            raise RuntimeError(f"登录失败: {msg}")

        last, html = self.follow_saml_forms(r2.text)
        if last is None and "SAMLResponse" in r2.text:
            raise RuntimeError("SAML 回跳失败")
        print(f"[login] ok -> {(last.url if last else r2.url)}")

    def ensure_sp_session(self, db: str) -> None:
        rid = DB_RESOURCE_IDS.get(db)
        if not rid or db in self.sp_ready:
            return
        print(f"[sp] 建立 {db} 会话 via resource:{rid}")
        url = f"https://ds.carsi.edu.cn/resource/gotoResource.php?id=resource:{rid}"
        r = self.s.get(url, timeout=60, allow_redirects=True, verify=self.verify)
        last, _ = self.follow_saml_forms(r.text)
        # 有些会先到 idp 再出 SAML 表单
        if last is None and ("SAMLRequest" in r.url or "idp.zju.edu.cn" in r.url):
            last, _ = self.follow_saml_forms(r.text)
        self.sp_ready.add(db)
        print(f"[sp] {db} ready -> {(last.url if last else r.url)[:120]}")

    def wait_delay(self, db: str, delays: dict[str, float]) -> float:
        delay = float(delays.get(db, delays.get("other", 5.0)))
        last = self.last_request_at.get(db, 0.0)
        now = time.time()
        need = delay - (now - last)
        if need > 0:
            print(f"[delay] {db} 等待 {need:.1f}s")
            time.sleep(need)
        self.last_request_at[db] = time.time()
        return delay

    def fetch_bytes(self, url: str, db: str, retries: int = 2):
        last_err = ""
        last_code = 0
        last_ct = ""
        for attempt in range(retries + 1):
            try:
                # 被拦时尝试先建 SP 会话
                if attempt > 0 and db in DB_RESOURCE_IDS:
                    self.ensure_sp_session(db)

                r = self.s.get(
                    url,
                    timeout=90,
                    allow_redirects=True,
                    verify=self.verify,
                    headers={
                        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
                        "Referer": f"https://{urlparse(url).netloc}/",
                    },
                )
                last_code = r.status_code
                last_ct = r.headers.get("Content-Type", "")
                content = r.content or b""

                # 若返回登录/SAML 页，尝试自动提交后再请求一次
                text_head = ""
                if not looks_like_pdf(content, last_ct) and b"<html" in content[:2000].lower():
                    try:
                        text_head = content.decode("utf-8", errors="ignore")
                    except Exception:
                        text_head = ""
                    if "SAMLResponse" in text_head or "document.forms[0].submit()" in text_head:
                        last, _ = self.follow_saml_forms(text_head)
                        r = self.s.get(
                            url,
                            timeout=90,
                            allow_redirects=True,
                            verify=self.verify,
                        )
                        last_code = r.status_code
                        last_ct = r.headers.get("Content-Type", "")
                        content = r.content or b""

                if r.status_code == 200 and looks_like_pdf(content, last_ct):
                    return True, r.status_code, last_ct, content, r.url, ""

                # HTML 且像登录墙
                if b"<html" in content[:1500].lower() or "text/html" in last_ct.lower():
                    last_err = f"not_pdf_html status={r.status_code} final={r.url[:120]}"
                    if db in DB_RESOURCE_IDS and db not in self.sp_ready:
                        self.ensure_sp_session(db)
                        continue
                else:
                    last_err = f"bad_status_or_body status={r.status_code} ct={last_ct} size={len(content)}"
            except Exception as e:
                last_err = f"exception: {e}"
            time.sleep(1.5 * (attempt + 1))
        return False, last_code, last_ct, b"", "", last_err

    def download_one(self, row: dict, delays: dict[str, float], prepare_sp: bool) -> dict:
        rid = row["id"]
        url = row["pdf_url"]
        title = row.get("title", "")
        db = detect_database(url)
        delay = self.wait_delay(db, delays)

        result = {
            "id": rid,
            "pdf_url": url,
            "title": title,
            "database": db,
            "local_path": "",
            "final_url": "",
            "status": "pending",
            "http_code": 0,
            "content_type": "",
            "file_size": 0,
            "file_md5": "",
            "error_msg": "",
            "delay_sec": delay,
            "downloaded_at": "",
        }

        print(f"[{rid}] db={db} url={url[:100]}")
        if prepare_sp and db in DB_RESOURCE_IDS:
            try:
                self.ensure_sp_session(db)
            except Exception as e:
                print(f"  sp warn: {e}")

        ok, code, ct, content, final_url, err = self.fetch_bytes(url, db)
        result["http_code"] = code
        result["content_type"] = ct
        result["final_url"] = final_url
        result["downloaded_at"] = now_iso()

        if not ok:
            result["status"] = "fail"
            result["error_msg"] = err or f"download_failed code={code}"
            print(f"  -> FAIL {result['error_msg']}")
            return result

        db_dir = PDF_DIR / db
        db_dir.mkdir(parents=True, exist_ok=True)
        out_path = db_dir / f"{safe_id(rid)}.pdf"
        out_path.write_bytes(content)
        result["status"] = "ok"
        result["local_path"] = str(out_path)
        result["file_size"] = out_path.stat().st_size
        result["file_md5"] = file_md5(out_path)
        print(f"  -> OK {out_path} size={result['file_size']}")
        return result


def load_mapping(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "id" not in reader.fieldnames or "pdf_url" not in reader.fieldnames:
            raise SystemExit("CSV 必须至少包含列: id,pdf_url")
        for i, row in enumerate(reader, start=1):
            rid = (row.get("id") or "").strip()
            url = (row.get("pdf_url") or "").strip()
            if not rid or not url:
                print(f"[跳过空行] line={i}")
                continue
            rows.append(
                {
                    "id": rid,
                    "pdf_url": url,
                    "title": (row.get("title") or "").strip(),
                    "note": (row.get("note") or "").strip(),
                }
            )
    return rows


def save_manifest(rows: list[dict], stamp: str) -> None:
    csv_path = OUT / f"real_manifest_{stamp}.csv"
    jsonl_path = OUT / f"real_manifest_{stamp}.jsonl"
    latest_csv = OUT / "real_manifest.csv"
    latest_jsonl = OUT / "real_manifest.jsonl"
    fields = [
        "id",
        "pdf_url",
        "title",
        "database",
        "local_path",
        "final_url",
        "status",
        "http_code",
        "content_type",
        "file_size",
        "file_md5",
        "error_msg",
        "delay_sec",
        "downloaded_at",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    with jsonl_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    latest_csv.write_bytes(csv_path.read_bytes())
    latest_jsonl.write_text(jsonl_path.read_text(encoding="utf-8"), encoding="utf-8")

    ok = sum(1 for r in rows if r["status"] == "ok")
    fail = sum(1 for r in rows if r["status"] == "fail")
    by_db: dict[str, int] = {}
    for r in rows:
        by_db[r["database"]] = by_db.get(r["database"], 0) + 1
    summary = {
        "mode": "real",
        "total": len(rows),
        "ok": ok,
        "fail": fail,
        "by_database": by_db,
        "pdf_dir": str(PDF_DIR),
        "manifest_csv": str(latest_csv),
        "finished_at": now_iso(),
    }
    (OUT / f"real_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "real_summary_latest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n==== 真实采集汇总 ====")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def parse_args():
    p = argparse.ArgumentParser(description="真实 PDF 采集（id-pdf 对应）")
    p.add_argument("--map", default=str(MAP_DIR / "sample_id_pdf_map.csv"))
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--no-login", action="store_true", help="不登录 CARSI（仅公开可下链接）")
    p.add_argument("--no-sp", action="store_true", help="不预先建立各库 SP 会话")
    p.add_argument("--fast", action="store_true", help="测试加速：延迟改为 1 秒")
    p.add_argument("--skip-existing", action="store_true", help="已存在同名 PDF 则跳过")
    return p.parse_args()


def main() -> None:
    ensure_dirs()
    args = parse_args()
    cfg = load_config()
    map_path = Path(args.map)
    if not map_path.exists():
        raise SystemExit(f"找不到对应表: {map_path}")

    rows = load_mapping(map_path)
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]
    if not rows:
        raise SystemExit("没有可下载的数据行")

    delays = dict(DEFAULT_DELAYS)
    if args.fast:
        delays = {k: 1.0 for k in delays}

    crawler = RealCrawler(
        username=cfg.get("username", ""),
        password=cfg.get("password", ""),
        verify_ssl=bool(cfg.get("verify_ssl", False)),
    )
    if not args.no_login:
        crawler.login_carsi()
    else:
        print("[login] skipped")

    stamp = ts()
    results = []
    for row in rows:
        db = detect_database(row["pdf_url"])
        out_path = PDF_DIR / db / f"{safe_id(row['id'])}.pdf"
        if args.skip_existing and out_path.exists() and out_path.stat().st_size > 1000:
            results.append(
                {
                    "id": row["id"],
                    "pdf_url": row["pdf_url"],
                    "title": row.get("title", ""),
                    "database": db,
                    "local_path": str(out_path),
                    "final_url": "",
                    "status": "skip",
                    "http_code": 0,
                    "content_type": "",
                    "file_size": out_path.stat().st_size,
                    "file_md5": file_md5(out_path),
                    "error_msg": "already_exists",
                    "delay_sec": 0,
                    "downloaded_at": now_iso(),
                }
            )
            print(f"[{row['id']}] skip existing {out_path}")
            continue
        results.append(
            crawler.download_one(row, delays=delays, prepare_sp=not args.no_sp)
        )

    save_manifest(results, stamp)
    log_path = LOG_DIR / f"real_run_{stamp}.log"
    log_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in results),
        encoding="utf-8",
    )
    print(f"日志: {log_path}")


if __name__ == "__main__":
    main()
