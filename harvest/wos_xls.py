# -*- coding: utf-8 -*-
"""WoS Excel export parser (COLUMNS match Java WosExportDownloader)."""
import io

COLUMNS = ("publication_type,authors,book_authors,book_editors,book_group_authors,author_full_names,book_author_full_names,group_authors,article_title,source_title,book_series_title,book_series_subtitle,language,document_type,conference_title,conference_date,conference_location,conference_sponsor,conference_host,author_keywords,keywords_plus,abstract_text,addresses,affiliations,reprint_addresses,email_addresses,researcher_ids,orc_ids,funding_orgs,funding_name_preferred,funding_text,cited_references,cited_reference_count,times_cited_wos_core,times_cited_all_db,usage_count_180_days,usage_count_since_2013,publisher,publisher_city,publisher_address,issn,eissn,isbn,journal_abbreviation,journal_iso_abbreviation,publication_date,publication_year,volume,issue,part_number,supplement,special_issue,meeting_abstract,start_page,end_page,article_number,doi,doi_link,book_doi,early_access_date,number_of_pages,wos_categories,web_of_science_index,research_areas,ids_number,pubmed_id,open_access_designations,highly_cited_status,hot_paper_status,date_of_export,ut_unique_wos_id,web_of_science_record").split(",")


def parse_xls(data):
    """返回 (headers, rows)；自动识别 xls/xlsx"""
    if data[:4] == b"\xd0\xcf\x11\xe0":  # OLE2 -> 真 xls
        import xlrd
        wb = xlrd.open_workbook(file_contents=data)
        sh = wb.sheet_by_index(0)
        hdr = [str(sh.cell_value(0, c)).strip() for c in range(sh.ncols)]
        rows = []
        for r in range(1, sh.nrows):
            rows.append([sh.cell_value(r, c) for c in range(sh.ncols)])
        return hdr, rows
    if data[:2] == b"PK":  # xlsx
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sh = wb.worksheets[0]
        it = sh.iter_rows(values_only=True)
        hdr = [str(x).strip() if x is not None else "" for x in next(it)]
        rows = [list(x) for x in it]
        wb.close()
        return hdr, rows
    raise RuntimeError("unknown export format: %r" % data[:80])
