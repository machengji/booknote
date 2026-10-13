# -*- coding: utf-8 -*-
"""Offline checks for the Scopus >5000 split helpers."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scopus_pipeline as sc


def test_title_group():
    g = sc._title_group("a")
    assert g.startswith("aa*")
    assert "az*" in g
    assert "ba*" not in g
    d = sc._digit_group()
    assert "00*" in d and "99*" in d


def test_parts_useful():
    many = [("q", [], 700, "let-%s" % a) for a in sc.AZ]
    assert sc.parts_useful(many, 19096)
    assert not sc.parts_useful([("q", [], 19096, "D:cp")], 19096)
    assert sc.parts_useful(
        [("q", [], 40000, "D:cp"), ("q", [], 10000, "D:ar")], 50551
    )
    assert not sc.parts_useful([], 100)
    assert not sc.parts_useful([("q", [], 3, "x")], 50000)
    assert not sc.parts_useful([("q", [], 4995, "a")], 5000)
    assert not sc.parts_useful([("q", [], 5168, "D:ar")], 5181)
    jparts = [("q", [], 7712, "j")] + [("q", [], 30, "j") for _ in range(7)]
    assert sc.parts_useful(jparts, 7961)


def test_splitters_for():
    names = [n for n, _ in sc.splitters_for("root")]
    assert names[0] == "doctype"
    assert "journals" not in names
    assert "letters" not in names
    assert "pubyear" in names
    names = [n for n, _ in sc.splitters_for("D:ar")]
    assert "doctype" not in names
    names = [n for n, _ in sc.splitters_for("S:j")]
    assert "srctype" not in names


def test_sources_from_recs():
    recs = [
        {"source": "Nature", "issn": "0028-0836"},
        {"source": "Nature", "issn": "0028-0836"},
        {"source": "Science", "issn": "0036-8075"},
        {"source": "", "issn": ""},
    ]
    rows = sc.sources_from_recs(recs)
    assert rows[0][0] == "Nature" and rows[0][1] == 2
    assert rows[1][0] == "Science"


def test_parse_facets():
    txt = '{"facetFields":{"srctitle":[{"Systems":144},{"Plos One":128}]}}'
    rows = sc.parse_srctitle_facets(txt)
    assert rows[0] == ("Systems", 144)
    assert rows[1] == ("Plos One", 128)


def test_pubyear_syntax():
    assert sc.pubyear_clause(2026) == "PUBYEAR = 2026"


def test_source_and_remainder():
    q = 'ORIG-LOAD-DATE AFT 20260831 AND ORIG-LOAD-DATE BEF 20260902'
    sq = sc.source_query(q, 'Nature')
    assert 'EXACTSRCTITLE("Nature")' in sq
    rem, n = sc.remainder_query(q, ["Nature", "Science"])
    assert n == 2
    assert "NOT EXACTSRCTITLE" in rem
    long_q = q + ' AND NOT EXACTSRCTITLE("Old")'
    s = sc.strip_src_nots(long_q)
    assert "Old" not in s
    rem, n = sc.remainder_query(long_q, ["Nature", "Science"])
    assert n == 2
    assert "Old" not in rem


if __name__ == "__main__":
    test_title_group()
    test_parts_useful()
    test_splitters_for()
    test_pubyear_syntax()
    test_source_and_remainder()
    print("ok")
