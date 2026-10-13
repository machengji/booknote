# -*- coding: utf-8 -*-
"""WoS JSONL -> MySQL 全字段导入。
用法: python harvest/wos_to_mysql.py F:\\pubmed\\wos\\wos_2026-09.jsonl [--dsn ...]
DSN 形如 mysql://user:pwd@host:3306/db，默认读环境 MYSQL_DSN。
密码可以含 @（从最后一个 @ 切开 host）。
表名默认 2026_wos_records（MYSQL_WOS_TABLE 可改）。
依赖: pip install pymysql
"""
import json
import os
import sys
import time


def load_dotenv():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, ".env")
    if not os.path.isfile(path):
        return
    for line in open(path, encoding="utf-8"):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()

TABLE_COLS = (
    "ut_unique_wos_id,publication_type,article_title,source_title,book_series_title,"
    "book_series_subtitle,language,document_type,authors,author_full_names,book_authors,"
    "book_author_full_names,group_authors,book_group_authors,book_editors,conference_title,"
    "conference_date,conference_location,conference_sponsor,conference_host,author_keywords,"
    "keywords_plus,abstract_text,addresses,affiliations,reprint_addresses,email_addresses,"
    "researcher_ids,orc_ids,funding_orgs,funding_name_preferred,funding_text,cited_references,"
    "cited_reference_count,times_cited_wos_core,times_cited_all_db,usage_count_180_days,"
    "usage_count_since_2013,publisher,publisher_city,publisher_address,issn,eissn,isbn,"
    "journal_abbreviation,journal_iso_abbreviation,publication_date,publication_year,volume,"
    "issue,part_number,supplement,special_issue,meeting_abstract,start_page,end_page,"
    "article_number,doi,doi_link,book_doi,early_access_date,number_of_pages,wos_categories,"
    "web_of_science_index,research_areas,ids_number,pubmed_id,open_access_designations,"
    "highly_cited_status,hot_paper_status,date_of_export,web_of_science_record"
).split(",")

INT_COLS = {"cited_reference_count", "times_cited_wos_core", "times_cited_all_db",
            "usage_count_180_days", "usage_count_since_2013", "publication_year",
            "number_of_pages"}


def to_int(v):
    try:
        if v in (None, ""):
            return None
        if isinstance(v, float):
            return int(v)
        return int(str(v).strip().split(".")[0])
    except Exception:
        return None


def parse_dsn(dsn):
    """mysql://user:password@host:port/db。密码可以含 @，从最后一个 @ 切开 host。"""
    assert dsn.startswith("mysql://"), dsn
    body = dsn[len("mysql://"):]
    at = body.rfind("@")
    if at < 0:
        raise ValueError("dsn missing @host")
    auth, hostdb = body[:at], body[at + 1:]
    user, _, pwd = auth.partition(":")
    hostport, _, db = hostdb.partition("/")
    host, _, port = hostport.partition(":")
    try:
        from urllib.parse import unquote
        user, pwd = unquote(user), unquote(pwd)
    except Exception:
        pass
    return {"host": host or "127.0.0.1", "port": int(port or 3306),
            "user": user, "password": pwd,
            "database": db or "qiyan_literature_collection"}


def mark_sync(conn, table, status, read_n, write_n, skip_n, err=""):
    """写 sync_run_log / sync_state。表不存在就跳过，不挡导入。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sync_run_log (group_name, source_table, started_at, "
                "finished_at, status, read_count, write_count, skip_count, error) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                ("wos-jsonl", table, now, now, status, int(read_n), int(write_n),
                 int(skip_n), (err or "")[:2000] or None))
            cur.execute(
                "INSERT INTO sync_state (source_table, group_name, last_run_at, "
                "last_status, last_message, read_count, write_count) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE last_run_at=VALUES(last_run_at), "
                "last_status=VALUES(last_status), last_message=VALUES(last_message), "
                "read_count=VALUES(read_count), write_count=VALUES(write_count)",
                ("jsonl:" + table, "wos-jsonl", now, status,
                 ("read=%s write=%s skip=%s" % (read_n, write_n, skip_n))[:1000],
                 int(read_n), int(write_n)))
        conn.commit()
    except Exception as e:
        print("sync-log-skip", str(e)[:120], flush=True)


def main():
    import pymysql
    path = sys.argv[1]
    dsn = next((a.split("=", 1)[1] for a in sys.argv[2:] if a.startswith("--dsn=")),
               os.environ.get("MYSQL_DSN", ""))
    table = next((a.split("=", 1)[1] for a in sys.argv[2:] if a.startswith("--table=")),
                 os.environ.get("MYSQL_WOS_TABLE") or "2026_wos_records")
    if not dsn:
        print("need --dsn=mysql://user:pwd@host:3306/db or MYSQL_DSN env")
        return 2
    kw = parse_dsn(dsn)
    print("dsn host", kw["host"], "db", kw["database"], "table", table,
          "user", kw["user"], flush=True)
    conn = pymysql.connect(charset="utf8mb4", autocommit=False, **kw)
    ident = "`%s`" % table.replace("`", "")
    ph = ",".join(["%s"] * len(TABLE_COLS))
    sql = ("INSERT INTO %s (%s) VALUES (%s) ON DUPLICATE KEY UPDATE "
           "times_cited_wos_core=VALUES(times_cited_wos_core),"
           "times_cited_all_db=VALUES(times_cited_all_db),"
           "usage_count_180_days=VALUES(usage_count_180_days),"
           "usage_count_since_2013=VALUES(usage_count_since_2013),"
           "date_of_export=VALUES(date_of_export)" % (ident, ",".join(TABLE_COLS), ph))
    n = skip = 0
    batch = []
    err = ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    skip += 1
                    continue
                if not r.get("ut_unique_wos_id"):
                    skip += 1
                    continue
                row = []
                for c in TABLE_COLS:
                    v = r.get(c)
                    if c in INT_COLS:
                        v = to_int(v)
                    elif isinstance(v, (list, dict)):
                        v = json.dumps(v, ensure_ascii=False)
                    elif v is not None:
                        v = str(v)
                    row.append(v)
                # WoS 导出的 DL 字段给的是数字 0，不是链接。用 doi 合成一个能点的 URL，
                # 否则每轮导入都会把新记录重新写成 "0"（2026-09-24 已回填 103,668 行）。
                dl = TABLE_COLS.index("doi_link") if "doi_link" in TABLE_COLS else -1
                if dl >= 0 and str(row[dl] or "").strip() in ("", "0", "0.0", "None"):
                    d = str(r.get("doi") or "").strip()
                    if d:
                        row[dl] = "https://doi.org/" + d
                batch.append(tuple(row))
                if len(batch) >= 500:
                    with conn.cursor() as cur:
                        cur.executemany(sql, batch)
                    conn.commit()
                    n += len(batch)
                    batch = []
                    print("loaded", n, flush=True)
        if batch:
            with conn.cursor() as cur:
                cur.executemany(sql, batch)
            conn.commit()
            n += len(batch)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM %s" % ident)
            dbn = cur.fetchone()[0]
        print("DONE loaded=%d skipped=%d db_count=%d" % (n, skip, dbn))
        mark_sync(conn, table, "ok", n + skip, n, skip)
        conn.close()
        return 0
    except Exception as e:
        err = str(e)[:500]
        print("FAIL", err, flush=True)
        try:
            mark_sync(conn, table, "error", n + skip, n, skip, err)
            conn.close()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
