# -*- coding: utf-8 -*-
"""
学术题录元数据后置增强解析器 (collectors/literature_oa_resolver.py)
================================================================================
核心功能（与 8090 采集通道 100% 物理隔离）：
1. 通过公网开放接口 (doi.org, Europe PMC API, Unpaywall API) 反查 landing_page_url、target_database、oa_pdf_url；
2. 优先从最近入库日期倒序 (ORDER BY latest_date DESC) 反查并原子回写 2026_scopus_details 与 2026_wos_records；
3. 数据库配置统一通过 config.settings 从 .env 加载，无任何硬编码密钥。
"""

import os
import sys
import re
import json
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import urllib3

from config.settings import load_settings

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def _detect_working_proxies():
    p = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
    try:
        r = requests.get("https://httpbin.org/ip", proxies=p, timeout=2, verify=False)
        if r.status_code == 200:
            return p
    except Exception:
        pass
    return None


PROXIES = _detect_working_proxies()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DATABASE_MATCH_RULES = [
    (r"(?:sciencedirect\.com|linkinghub\.elsevier\.com|elsevier\.com)", "Elsevier ScienceDirect"),
    (r"(?:link\.springer\.com|nature\.com|springeropen\.com|biomedcentral\.com)", "Springer Nature"),
    (r"onlinelibrary\.wiley\.com", "Wiley Online Library"),
    (r"ieeexplore\.ieee\.org", "IEEE Xplore"),
    (r"tandfonline\.com", "Taylor & Francis Online"),
    (r"pubs\.acs\.org", "ACS Publications"),
    (r"mdpi\.com", "MDPI"),
    (r"frontiersin\.org", "Frontiers"),
    (r"hindawi\.com", "Hindawi"),
    (r"(?:journals\.plos\.org|plos\.org)", "PLOS"),
    (r"academic\.oup\.com", "Oxford University Press"),
    (r"iopscience\.iop\.org", "IOP Publishing"),
    (r"pubs\.rsc\.org", "Royal Society of Chemistry"),
    (r"journals\.sagepub\.com", "SAGE Publications"),
    (r"(?:wkhealth\.com|lww\.com|ovid\.com)", "Wolters Kluwer / Ovid"),
    (r"ahajournals\.org", "AHA Journals"),
    (r"(?:aip\.scitation\.org|pubs\.aip\.org)", "AIP Publishing"),
    (r"cambridge\.org", "Cambridge University Press"),
    (r"emerald\.com", "Emerald Insight"),
    (r"karger\.com", "Karger Publishers"),
    (r"degruyter\.com", "De Gruyter"),
    (r"annualreviews\.org", "Annual Reviews"),
    (r"pnas\.org", "PNAS"),
    (r"jamanetwork\.com", "JAMA Network"),
    (r"bmj\.com", "BMJ"),
    (r"nejm\.org", "NEJM"),
    (r"cell\.com", "Cell Press"),
    (r"thelancet\.com", "The Lancet"),
    (r"liebertpub\.com", "Mary Ann Liebert"),
    (r"worldscientific\.com", "World Scientific"),
    (r"cnki\.net", "CNKI"),
    (r"wanfangdata\.com\.cn", "Wanfang Data"),
]


def identify_database(url: str) -> str:
    if not url:
        return "Unknown"
    url_lower = url.lower()
    for pattern, db_name in DATABASE_MATCH_RULES:
        if re.search(pattern, url_lower):
            return db_name
    try:
        domain = urllib.parse.urlparse(url).netloc
        return re.sub(r"^www\.", "", domain)
    except Exception:
        return "Unknown"


def resolve_doi_landing_page(session: requests.Session, doi: str, timeout: int = 15):
    doi = doi.strip()
    if not doi:
        return None, "empty_doi"

    doi_clean = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    doi_url = f"https://doi.org/{urllib.parse.quote(doi_clean)}"
    try:
        resp = session.get(
            doi_url,
            headers=HEADERS,
            timeout=timeout,
            allow_redirects=True,
            verify=False,
            stream=False,
        )
        final_url = resp.url
        if "linkinghub.elsevier.com" in final_url:
            pii_m = re.search(r"/pii/([A-Z0-9]+)", final_url)
            if pii_m:
                final_url = f"https://www.sciencedirect.com/science/article/pii/{pii_m.group(1)}"
            else:
                redirect_m = re.search(r"Redirect=([^\"\'&]+)", resp.text)
                if redirect_m:
                    final_url = urllib.parse.unquote(redirect_m.group(1))

        meta_refresh = re.search(
            r'<meta[^>]*http-equiv=[\'"]?refresh[\'"]?[^>]*content=[\'"]?\d+;\s*url=([^\'">\s]+)',
            resp.text,
            re.IGNORECASE,
        )
        if meta_refresh:
            next_url = urllib.parse.urljoin(final_url, meta_refresh.group(1).strip())
            try:
                r2 = session.get(
                    next_url, headers=HEADERS, timeout=timeout, allow_redirects=True, verify=False
                )
                final_url = r2.url
            except Exception:
                final_url = next_url

        return final_url, "success"
    except requests.exceptions.RequestException as e:
        return None, f"request_error: {str(e)[:50]}"


def resolve_oa_pdf_url(session: requests.Session, doi: str, landing_url: str = "", timeout: int = 10):
    doi_clean = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi.strip()).lower()
    if "10.1371/" in doi_clean:
        return True, "gold", f"https://journals.plos.org/plosone/article/file?id={doi_clean}&type=printable"
    if "10.1186/" in doi_clean:
        return True, "gold", f"https://link.springer.com/content/pdf/{doi_clean}.pdf"
    if "10.3389/" in doi_clean:
        return True, "gold", f"https://www.frontiersin.org/articles/{doi_clean}/pdf"
    if "10.3390/" in doi_clean and landing_url:
        return True, "gold", landing_url.rstrip("/") + "/pdf"
    if "10.1155/" in doi_clean and landing_url:
        return True, "gold", f"https://downloads.hindawi.com/journals/{doi_clean}.pdf"

    try:
        epmc_api = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:{urllib.parse.quote(doi_clean)}&format=json&resultType=core"
        r = session.get(epmc_api, timeout=timeout, headers=HEADERS, verify=False)
        if r.status_code == 200:
            results = r.json().get("resultList", {}).get("result", [])
            if results:
                hit = results[0]
                is_oa = hit.get("isOpenAccess") == "Y"
                pdf_urls = [
                    u.get("url")
                    for u in hit.get("fullTextUrlList", {}).get("fullTextUrl", [])
                    if u.get("documentStyle") == "pdf" and u.get("url")
                ]
                if pdf_urls:
                    return True, "gold" if is_oa else "repository", pdf_urls[0]
                if is_oa:
                    if landing_url and "ieeexplore.ieee.org" in landing_url:
                        m = re.search(r"/document/(\d+)", landing_url)
                        if m:
                            return True, "gold", f"https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber={m.group(1)}"
                    if landing_url and "onlinelibrary.wiley.com" in landing_url:
                        return True, "gold", f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi_clean}"
                    return True, "gold", None
    except Exception:
        pass

    try:
        unpaywall_api = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi_clean)}?email=academic_harvester@qiyan.org"
        r = session.get(unpaywall_api, timeout=timeout, headers=HEADERS, verify=False)
        if r.status_code == 200:
            upw_json = r.json()
            if upw_json.get("is_oa"):
                oa_locations = upw_json.get("oa_locations", [])
                best_loc = upw_json.get("best_oa_location") or {}
                pdf_url = best_loc.get("url_for_pdf")
                if not pdf_url:
                    for loc in oa_locations:
                        if loc.get("url_for_pdf"):
                            pdf_url = loc.get("url_for_pdf")
                            break
                if not pdf_url and landing_url:
                    if "ieeexplore.ieee.org" in landing_url:
                        m = re.search(r"/document/(\d+)", landing_url)
                        if m:
                            pdf_url = f"https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber={m.group(1)}"
                    elif "onlinelibrary.wiley.com" in landing_url:
                        pdf_url = f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi_clean}"
                return True, upw_json.get("oa_status", "oa"), pdf_url
    except Exception:
        pass

    return False, None, None


def resolve_single_record(doi: str, session: requests.Session = None) -> dict:
    close_session = False
    if session is None:
        session = requests.Session()
        session.proxies = PROXIES
        close_session = True

    try:
        landing_url, status = resolve_doi_landing_page(session, doi)
        target_db = identify_database(landing_url) if landing_url else "Unknown"
        is_oa, oa_type, oa_pdf_url = resolve_oa_pdf_url(session, doi, landing_url or "")

        return {
            "doi": doi,
            "landing_page_url": landing_url,
            "target_database": target_db,
            "is_oa": 1 if is_oa else 0,
            "oa_type": oa_type,
            "oa_pdf_url": oa_pdf_url,
            "status": status if landing_url else "failed",
        }
    finally:
        if close_session:
            session.close()


def save_resolved_batch(records: list) -> None:
    if not records:
        return
    settings = load_settings()
    conn = settings.create_db_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            sql_index = """
            INSERT INTO `literature_landing_oa_index`
                (`doi`, `landing_page_url`, `target_database`, `is_oa`, `oa_type`, `oa_pdf_url`, `status`)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                `landing_page_url` = VALUES(`landing_page_url`),
                `target_database` = VALUES(`target_database`),
                `is_oa` = VALUES(`is_oa`),
                `oa_type` = VALUES(`oa_type`),
                `oa_pdf_url` = VALUES(`oa_pdf_url`),
                `status` = VALUES(`status`),
                `updated_at` = CURRENT_TIMESTAMP
            """
            params = [
                (
                    r["doi"],
                    r["landing_page_url"],
                    r["target_database"],
                    r["is_oa"],
                    r["oa_type"],
                    r["oa_pdf_url"],
                    r["status"],
                )
                for r in records
            ]
            cur.executemany(sql_index, params)

            valid_updates = [
                (r["landing_page_url"], r["target_database"], r["oa_pdf_url"], r["doi"])
                for r in records
                if r["landing_page_url"]
            ]
            if valid_updates:
                cur.executemany(
                    "UPDATE `2026_wos_records` SET `landing_page_url`=%s, `target_database`=%s, `oa_pdf_url`=%s WHERE `doi`=%s",
                    valid_updates,
                )
                cur.executemany(
                    "UPDATE `2026_scopus_details` SET `landing_page_url`=%s, `target_database`=%s, `oa_pdf_url`=%s WHERE `doi`=%s",
                    valid_updates,
                )
    finally:
        conn.close()


def sync_existing_index_to_tables() -> None:
    settings = load_settings()
    conn = settings.create_db_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE `2026_scopus_details` d
                INNER JOIN `literature_landing_oa_index` idx ON d.doi = idx.doi
                SET d.landing_page_url = idx.landing_page_url,
                    d.target_database = idx.target_database,
                    d.oa_pdf_url = idx.oa_pdf_url
                WHERE d.target_database IS NULL
                  AND idx.landing_page_url IS NOT NULL
                  AND idx.landing_page_url != ''
                """
            )
            scopus_synced = cur.rowcount

            cur.execute(
                """
                UPDATE `2026_wos_records` w
                INNER JOIN `literature_landing_oa_index` idx ON w.doi = idx.doi
                SET w.landing_page_url = idx.landing_page_url,
                    w.target_database = idx.target_database,
                    w.oa_pdf_url = idx.oa_pdf_url
                WHERE w.target_database IS NULL
                  AND idx.landing_page_url IS NOT NULL
                  AND idx.landing_page_url != ''
                """
            )
            wos_synced = cur.rowcount
        if scopus_synced or wos_synced:
            print(
                f"[索引直连回填] 已同步回填 Scopus 详情表 {scopus_synced} 条, WoS 主表 {wos_synced} 条。"
            )
    finally:
        conn.close()


def process_unresolved_batch(limit: int = 200, workers: int = 16) -> int:
    settings = load_settings()
    conn = settings.create_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT r.doi, MAX(r.rec_date) AS latest_date
                FROM (
                    SELECT doi, load_date AS rec_date
                    FROM `2026_scopus_details`
                    WHERE doi IS NOT NULL AND doi != '' AND target_database IS NULL
                    UNION ALL
                    SELECT doi, date_of_export AS rec_date
                    FROM `2026_wos_records`
                    WHERE doi IS NOT NULL AND doi != '' AND target_database IS NULL
                ) r
                LEFT JOIN `literature_landing_oa_index` idx ON r.doi = idx.doi
                WHERE idx.doi IS NULL OR idx.status = 'pending'
                GROUP BY r.doi
                ORDER BY latest_date DESC
                LIMIT {int(limit)}
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print("未发现待解析题录。")
        return 0

    dois = [row[0] for row in rows]
    date_max = str(rows[0][1])
    date_min = str(rows[-1][1])
    print(
        f"开始批量解析 {len(dois)} 条题录 | 优先日期区间: [{date_max} -> {date_min}] (并发线程: {workers})..."
    )
    start_time = time.time()
    results = []

    def _worker(d):
        s = requests.Session()
        s.proxies = PROXIES
        try:
            return resolve_single_record(d, s)
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_worker, d): d for d in dois}
        for future in as_completed(futures):
            try:
                res = future.result()
                results.append(res)
                if len(results) % 20 == 0 or len(results) == len(dois):
                    print(
                        f"已解析: {len(results)}/{len(dois)} ({date_max}) | 当前: {res['doi'][:30]} -> {res['target_database']} (OA: {res['is_oa']})"
                    )
            except Exception as e:
                print(f"解析失败: {futures[future]}, 错误: {e}")

    save_resolved_batch(results)
    elapsed = time.time() - start_time
    print(
        f"完成！共解析入库 {len(results)} 条 (日期: {date_max}~{date_min})，耗时 {elapsed:.2f} 秒。"
    )
    return len(results)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="学术题录落地页与 OA 直链后置反查器")
    parser.add_argument("--test-single", type=str, help="测试解析单个 DOI")
    parser.add_argument("--batch-limit", type=int, default=200, help="单批次解析条数限制")
    parser.add_argument("--workers", type=int, default=16, help="并发工作线程数")
    parser.add_argument("--loop", action="store_true", help="是否持续循环解析（从最近日期往回推进）")
    args = parser.parse_args()

    if args.test_single:
        res = resolve_single_record(args.test_single)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        sync_existing_index_to_tables()
        if args.loop:
            batch_no = 0
            while True:
                batch_no += 1
                print(f"\n=== [第 {batch_no} 批] 按最近日期倒序反查任务启动 ===")
                try:
                    cnt = process_unresolved_batch(limit=args.batch_limit, workers=args.workers)
                    if cnt == 0:
                        time.sleep(300)
                except Exception as exc:
                    print(f"[异常告警] 批次执行出错: {exc}，10 秒后重试...")
                    time.sleep(10)
        else:
            process_unresolved_batch(limit=args.batch_limit, workers=args.workers)
