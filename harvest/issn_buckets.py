# -*- coding: utf-8 -*-
"""Disjoint ISSN buckets built once from the official Scopus source list xlsx.

The Scopus search gateway hard-caps resultSet offset+itemCount at 5000 and its
facet endpoint returns only 7 source titles. Instead of peeling journals one by
one, partition the whole ISSN universe offline: every journal article's ISSN
appears in the official source list, so fixed buckets cover the day with no
overlap and no window math.

Build once (list refreshes monthly):
    python issn_buckets.py build E:\\mazcodework\\scopus_source_list_aug2026.xlsx

Lookup order for the built JSON:
    $BOOKNOTE_ISSN_BUCKETS, else <BOOKNOTE_DATA>/scopus_issn_buckets.json
"""
import json
import os
import re
import time

DATA = os.environ.get("BOOKNOTE_DATA", r"E:\pubmed")
BUCKET_SIZE = int(os.environ.get("BOOKNOTE_ISSN_BUCKET_SIZE", "400"))
MAX_Q = 12000  # gateway rejects longer queries; observed NOT-strings die nearby


def _default_out():
    return os.path.join(os.environ.get("BOOKNOTE_DATA", r"E:\pubmed"),
                        "scopus_issn_buckets.json")


def _codes_from_xlsx(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb[wb.sheetnames[0]]
    out = set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        for j in (2, 3):  # ISSN, EISSN
            v = row[j] if j < len(row) else None
            if not v:
                continue
            s = re.sub(r"[^0-9Xx]", "", str(v))
            if len(s) == 8:
                out.add(s.upper())
    return sorted(out)


def hyphen(code8):
    return code8[:4] + "-" + code8[4:]


def build(xlsx_path, out_path=None, size=BUCKET_SIZE):
    codes = _codes_from_xlsx(xlsx_path)
    buckets = [[hyphen(c) for c in codes[i:i + size]] for i in range(0, len(codes), size)]
    payload = {
        "built": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": os.path.basename(xlsx_path),
        "count": len(codes),
        "bucket_size": size,
        "buckets": buckets,
    }
    out_path = out_path or _default_out()
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return out_path, len(codes), len(buckets)


def load(path=None):
    path = path or os.environ.get("BOOKNOTE_ISSN_BUCKETS") or _default_out()
    with open(path, encoding="utf-8") as f:
        j = json.load(f)
    return j.get("buckets") or []


def bucket_queries(base_q):
    """Yield (bucket_index, query) strings: base AND ISSN(x OR y ...)."""
    for i, grp in enumerate(load()):
        clause = " AND ISSN(%s)" % " OR ".join(grp)
        q = base_q + clause
        if len(q) <= MAX_Q:
            yield i, q


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "build":
        out, n, nb = build(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
        print("wrote", out, "issns", n, "buckets", nb)
    else:
        print("usage: python issn_buckets.py build <source_list.xlsx> [out.json]")
