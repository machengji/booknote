# -*- coding: utf-8 -*-
"""把官方 Scopus 来源清单灌进按刊线的刊名表。

为什么需要它
    按刊线的刊名表原来是从「采到的记录」播种的（--seed 读 JSONL 的 source 字段），
    这是循环依赖 —— 从未采到的刊永远发现不了。官方 ext_list（49,009 个来源，
    其中 28,370 个覆盖到 2026）是权威全集，用它播种才能主动覆盖。

做什么
    1. 读 F:\\pubmed\\scopus\\_待探来源.tsv（source_id / title / issn / eissn / active / type）
    2. 不在刊名表里的 → 新增，带 issn（这样查询走 ISSN 而不是 EXACTSRCTITLE）
    3. 已在表里的（刊名完全一致）→ 只补 issn，不动 tot/new/done 等已有状态
    4. 落盘前备份原 state

新增条目的 tot=None，而 main() 的排序是 `-(tot or 0)`，所以**新刊天然排在最前面**，
不需要另外做优先级队列。

用法
    python harvest/scopus_seed_official.py            # 干跑，只报数
    python harvest/scopus_seed_official.py --apply    # 真写
"""
import io
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

os.environ.setdefault("BOOKNOTE_DATA", r"F:\pubmed")
os.environ.setdefault("BOOKNOTE_LOG", r"F:\pubmed\logs")
os.environ.setdefault("SCOPUS_OUT", r"F:\pubmed\scopus")

import scopus_pipeline as sc  # noqa: E402

SRC = os.path.join(sc.OUT_DIR, "_待探来源.tsv")
STATE = os.path.join(sc.OUT_DIR, "scopus_journal_state.json")
BACKUP = os.path.join(sc.OUT_DIR, "scopus_journal_state.json.bak-%s"
                      % time.strftime("%Y%m%d-%H%M%S"))


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def main():
    apply_ = "--apply" in sys.argv
    if not os.path.isfile(SRC):
        log("[no-src]", SRC, "先放好待探来源.tsv")
        return 2
    try:
        st = json.load(io.open(STATE, encoding="utf-8")) or {}
    except Exception:
        st = {}
    log("现有刊名表", len(st), "本")

    added = 0
    patched = 0
    skipped = 0
    n = 0
    with io.open(SRC, encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) < 6 or p[0] == "source_id":
                continue
            sid, title, issn, eissn, active, typ = p[0], p[1].strip(), p[2], p[3], p[4], p[5]
            if not title:
                skipped += 1
                continue
            n += 1
            issn = (issn or eissn).replace("-", "").strip().upper()
            cur = st.get(title)
            if cur is None:
                st[title] = {"tot": None, "new": 0, "done": False, "ts": "",
                             "note": "official-seed", "issn": issn,
                             "src_id": sid, "src_type": typ, "src_active": active}
                added += 1
            else:
                if issn and not cur.get("issn"):
                    cur["issn"] = issn
                    patched += 1
                # 已有的 tot/new/done 一律不动
    log("读到 %d 行 | 新增 %d | 补 issn %d | 跳过 %d" % (n, added, patched, skipped))
    log("灌后刊名表 %d 本" % len(st))

    if not apply_:
        log("（干跑，未写盘。加 --apply 才写）")
        return 0
    try:
        shutil.copy2(STATE, BACKUP)
        log("已备份", os.path.basename(BACKUP))
    except Exception as e:
        log("[backup-skip]", str(e)[:80])
    tmp = STATE + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)
    log("已写盘", STATE)

    # 回读校验
    st2 = json.load(io.open(STATE, encoding="utf-8"))
    seeded = sum(1 for v in st2.values() if v.get("note") == "official-seed")
    with_issn = sum(1 for v in st2.values() if v.get("issn"))
    log("回读: 总数 %d | official-seed %d | 带 issn %d" % (len(st2), seeded, with_issn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
