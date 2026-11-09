# -*- coding: utf-8 -*-
"""
Web of Science (WoS) Full Record Excel 导出文件解析器 (collectors/wos_xls.py)
严格对齐官方 71 列标准字段顺序，支持 OLE2 (.xls) 与 OpenXML (.xlsx) 格式。
"""

import io
import os
import json

COLUMNS = (
    "publication_type,authors,book_authors,book_editors,book_group_authors,"
    "author_full_names,book_author_full_names,group_authors,article_title,source_title,"
    "book_series_title,book_series_subtitle,language,document_type,conference_title,"
    "conference_date,conference_location,conference_sponsor,conference_host,author_keywords,"
    "keywords_plus,abstract_text,addresses,affiliations,reprint_addresses,email_addresses,"
    "researcher_ids,orc_ids,funding_orgs,funding_name_preferred,funding_text,cited_references,"
    "cited_reference_count,times_cited_wos_core,times_cited_all_db,usage_count_180_days,"
    "usage_count_since_2013,publisher,publisher_city,publisher_address,issn,eissn,isbn,"
    "journal_abbreviation,journal_iso_abbreviation,publication_date,publication_year,volume,"
    "issue,part_number,supplement,special_issue,meeting_abstract,start_page,end_page,"
    "article_number,doi,doi_link,book_doi,early_access_date,number_of_pages,wos_categories,"
    "web_of_science_index,research_areas,ids_number,pubmed_id,open_access_designations,"
    "highly_cited_status,hot_paper_status,date_of_export,ut_unique_wos_id,web_of_science_record"
).split(",")


def parse_xls_bytes(data: bytes) -> tuple[list[str], list[list]]:
    """解析 WoS 导出的二进制 Excel 内容，返回 (headers, rows)"""
    if data[:4] == b"\xd0\xcf\x11\xe0":
        import xlrd

        wb = xlrd.open_workbook(file_contents=data)
        sh = wb.sheet_by_index(0)
        hdr = [str(sh.cell_value(0, c)).strip() for c in range(sh.ncols)]
        rows = [[sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(1, sh.nrows)]
        return hdr, rows

    if data[:2] == b"PK":
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sh = wb.worksheets[0]
        it = sh.iter_rows(values_only=True)
        hdr = [str(x).strip() if x is not None else "" for x in next(it)]
        rows = [list(x) for x in it]
        wb.close()
        return hdr, rows

    raise RuntimeError(f"[WOS_XLS_FORMAT_ERROR] 未知的 Excel 文件头格式: {data[:60]!r}")


def ingest_xls_to_jsonl(raw_xls_path: str, out_jsonl_path: str) -> tuple[int, int]:
    """将单个千篇切片 .xls 解析并去重追加写入月度 JSONL 文件，返回 (新增条数, 文件总行数)"""
    with open(raw_xls_path, "rb") as rf:
        data = rf.read()
    _, rows = parse_xls_bytes(data)

    existing_uts = set()
    if os.path.exists(out_jsonl_path):
        with open(out_jsonl_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.strip():
                    try:
                        ut_val = json.loads(line).get("ut_unique_wos_id")
                        if ut_val:
                            existing_uts.add(ut_val)
                    except Exception:
                        pass

    os.makedirs(os.path.dirname(out_jsonl_path), exist_ok=True)
    added = 0
    with open(out_jsonl_path, "a", encoding="utf-8") as f:
        for row in rows:
            rec = {}
            for i, col_name in enumerate(COLUMNS):
                if i >= len(row):
                    break
                val = row[i]
                if isinstance(val, float) and val == int(val):
                    val = int(val)
                if val not in (None, ""):
                    rec[col_name] = val
            ut_key = rec.get("ut_unique_wos_id", "")
            if not ut_key or ut_key in existing_uts:
                continue
            existing_uts.add(ut_key)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            added += 1

    return added, len(rows)
