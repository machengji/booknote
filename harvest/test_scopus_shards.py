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


def test_splitters_for():
    names = [n for n, _ in sc.splitters_for("root")]
    assert names[0] == "doctype" and "letters" in names
    names = [n for n, _ in sc.splitters_for("let-t")]
    assert names[0].startswith("bigram-t")
    names = [n for n, _ in sc.splitters_for("bi-th")]
    assert "journals" in names


def test_source_and_remainder():
    q = 'ORIG-LOAD-DATE AFT 20260831 AND ORIG-LOAD-DATE BEF 20260902'
    sq = sc.source_query(q, 'Nature')
    assert 'EXACTSRCTITLE("Nature")' in sq
    rem, n = sc.remainder_query(q, ["Nature", "Science"])
    assert n == 2
    assert "NOT EXACTSRCTITLE" in rem


if __name__ == "__main__":
    test_title_group()
    test_parts_useful()
    test_splitters_for()
    test_source_and_remainder()
    print("ok")
