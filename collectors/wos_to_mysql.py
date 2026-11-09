# -*- coding: utf-8 -*-
"""
Web of Science (WoS) JSONL -> MySQL (2026_wos_records) 71 列全字段同步模块 (collectors/wos_to_mysql.py)
"""

import json
import os
import sys
import time
from config.settings import load_settings

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

INT_COLS = {
    "cited_reference_count",
    "times_cited_wos_core",
    "times_cited_all_db",
    "usage_count_180_days",
    "usage_count_since_2013",
    "publication_year",
    "number_of_pages",
}


def _to_int(v):
    try:
        if v in (None, ""):
            return None
        if isinstance(v, float):
            return int(v)
        return int(str(v).strip().split(".")[0])
    except Exception:
        return None


def _mark_sync(conn, table: str, status: str, read_n: int, write_n: int, skip_n: int, err: str = "") -> None:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sync_run_log (group_name, source_table, started_at, "
                "finished_at, status, read_count, write_count, skip_count, error) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    "wos-jsonl",
                    table,
                    now,
                    now,
                    status,
                    int(read_n),
                    int(write_n),
                    int(skip_n),
                    (err or "")[:2000] or None,
                ),
            )
            cur.execute(
                "INSERT INTO sync_state (source_table, group_name, last_run_at, "
                "last_status, last_message, read_count, write_count) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE last_run_at=VALUES(last_run_at), "
                "last_status=VALUES(last_status), last_message=VALUES(last_message), "
                "read_count=VALUES(read_count), write_count=VALUES(write_count)",
                (
                    "jsonl:" + table,
                    "wos-jsonl",
                    now,
                    status,
                    f"read={read_n} write={write_n} skip={skip_n}"[:1000],
                    int(read_n),
                    int(write_n),
                ),
            )
        conn.commit()
    except Exception as e:
        print(f"[wos_to_mysql] sync_state 记录提示: {str(e)[:120]}", flush=True)


def sync_jsonl_to_mysql(jsonl_path: str, table: str = "2026_wos_records") -> int:
    """将指定 WoS JSONL 文件同步导入阿里云 RDS MySQL 生产表"""
    if not os.path.exists(jsonl_path):
        raise FileNotFoundError(f"未找到待导入的 JSONL 文件: {jsonl_path}")

    settings = load_settings()
    conn = settings.create_db_connection(autocommit=False)
    ident = f"`{table.replace('`', '')}`"
    placeholders = ",".join(["%s"] * len(TABLE_COLS))
    sql = (
        f"INSERT INTO {ident} ({','.join(TABLE_COLS)}) VALUES ({placeholders}) "
        "ON DUPLICATE KEY UPDATE "
        "times_cited_wos_core=VALUES(times_cited_wos_core),"
        "times_cited_all_db=VALUES(times_cited_all_db),"
        "usage_count_180_days=VALUES(usage_count_180_days),"
        "usage_count_since_2013=VALUES(usage_count_since_2013),"
        "date_of_export=VALUES(date_of_export)"
    )

    loaded_count = 0
    skipped_count = 0
    batch = []

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    skipped_count += 1
                    continue
                if not rec.get("ut_unique_wos_id"):
                    skipped_count += 1
                    continue

                row = []
                for col in TABLE_COLS:
                    val = rec.get(col)
                    if col in INT_COLS:
                        val = _to_int(val)
                    elif isinstance(val, (list, dict)):
                        val = json.dumps(val, ensure_ascii=False)
                    elif val is not None:
                        val = str(val)
                    row.append(val)

                batch.append(tuple(row))
                if len(batch) >= 500:
                    with conn.cursor() as cur:
                        cur.executemany(sql, batch)
                    conn.commit()
                    loaded_count += len(batch)
                    batch.clear()

        if batch:
            with conn.cursor() as cur:
                cur.executemany(sql, batch)
            conn.commit()
            loaded_count += len(batch)

        _mark_sync(conn, table, "ok", loaded_count + skipped_count, loaded_count, skipped_count)
        conn.close()
        print(
            f"[wos_to_mysql] 同步完成: {jsonl_path} -> {table} (写入={loaded_count:,}, 跳过={skipped_count:,})",
            flush=True,
        )
        return loaded_count
    except Exception as e:
        err_msg = str(e)[:500]
        print(f"[wos_to_mysql] 导入失败: {err_msg}", flush=True)
        try:
            _mark_sync(conn, table, "error", loaded_count + skipped_count, loaded_count, skipped_count, err_msg)
            conn.close()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python -m collectors.wos_to_mysql <jsonl_path>", flush=True)
        sys.exit(1)
    sync_jsonl_to_mysql(sys.argv[1])
