# -*- coding: utf-8 -*-
"""Shared paths and .env loader for full-text download scripts."""
import os

def load_dotenv():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for path in (os.path.join(root, ".env"), os.path.join(here, ".env")):
        if not os.path.isfile(path):
            continue
        for line in open(path, encoding="utf-8"):
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()
if os.environ.get("ZJU_PWD") and not os.environ.get("ZJU_PASS"):
    os.environ["ZJU_PASS"] = os.environ["ZJU_PWD"]

DATA = os.environ.get("BOOKNOTE_DATA", r"E:\pubmed")
PDFS = os.environ.get("BOOKNOTE_PDFS", r"E:\pdfs")
LOGS = os.environ.get("BOOKNOTE_LOG", os.path.join(DATA, "logs"))
FT = os.environ.get("BOOKNOTE_FULLTEXT", os.path.join(DATA, "fulltext"))
for _p in (DATA, PDFS, LOGS, FT):
    os.makedirs(_p, exist_ok=True)


def zju_creds():
    out = []
    u, p = os.environ.get("ZJU_USER") or "", os.environ.get("ZJU_PWD") or os.environ.get("ZJU_PASS") or ""
    if u and p:
        out.append((u, p))
    u2, p2 = os.environ.get("ZJU_USER_2") or "", os.environ.get("ZJU_PWD_2") or ""
    if u2 and p2:
        out.append((u2, p2))
    return out
