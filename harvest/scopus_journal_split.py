# -*- coding: utf-8 -*-
"""把已下载的 Scopus 记录按刊名拆开，落成一本刊一个文件。

为什么按刊拆
    2026-09-28 起 Scopus 采集只走「按刊」一条线（按天线已整段移除）。
    按刊线对账需要知道「这本刊我手上已有哪些记录」，一本刊一个文件让这件事
    可以直接看、可以直接 diff，也便于按刊交付。
    已有的 scopus_journal_index.json 是同一份信息的聚合缓存（{刊名: set(eid)}），
    给引擎用的；本脚本产出的是人能看、能拿走的形态。

产出
    F:\\pubmed\\scopus\\by_journal\\<安全刊名>.jsonl   每本刊一个文件，一行一条原记录
    F:\\pubmed\\scopus\\scopus_journal_counts.json     每本刊的条数/唯一 eid/日期范围

口径
    条数按行算（去重键是 (eid, load_date)，一条记录在不同 load_date 下可以出现
    多次，这是有意的，不能按 eid 合并丢历史）；同时给出 unique eid 数，
    按刊线对账用的是后者。

用法
    python harvest/scopus_journal_split.py                # 拆全部月份
    python harvest/scopus_journal_split.py --months 2026-09
    python harvest/scopus_journal_split.py --dry          # 只统计不落盘
"""
import argparse
import collections
import io
import json
import os
import re
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

os.environ.setdefault("BOOKNOTE_DATA", r"F:\pubmed")
os.environ.setdefault("BOOKNOTE_LOG", r"F:\pubmed\logs")
os.environ.setdefault("SCOPUS_OUT", r"F:\pubmed\scopus")

import scopus_pipeline as sc  # noqa: E402

OUT_ROOT = os.path.join(sc.OUT_DIR, "by_journal")
COUNTS_PATH = os.path.join(sc.OUT_DIR, "scopus_journal_counts.json")
MONTH_RE = re.compile(r"^scopus_(\d{4}-\d{2})\.jsonl$")
MAX_OPEN = 2000
NAME_MAX = 120

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED = ({"CON", "PRN", "AUX", "NUL"}
             | {"COM%d" % i for i in range(1, 10)}
             | {"LPT%d" % i for i in range(1, 10)})
_JUNK = {"", "undefined", "null", "none", "nan", "-", "n/a", "na", "unknown"}
UNKNOWN = "_未标注刊名"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def safe_name(name, taken):
    """taken: {小写安全名: 原名}，必须按**小写**做键。

    Windows 文件系统不区分大小写，而 Scopus 的 source 里确实存在只差大小写的
    两个刊名（实测 GLIA / Glia、CA A Cancer Journal for Clinicians /
    Ca A Cancer Journal for Clinicians）。按原样做键会漏判冲突，两本刊落到
    同一个文件上，交替用 "w" 打开互相截断（09-28 实测 4 本刊的字节全部对不上）。
    冲突时补一段**确定性**后缀（crc32，不能用 hash()，它每个进程都变，
    会导致每次重跑都换文件名、留下旧文件）。

    另外：Windows 文件名不能带 \\ / : * ? " < > |，也不能叫 CON 这类保留名。
    """
    s = _BAD.sub("_", (name or "").strip())
    s = s.rstrip(". ")
    if len(s) > NAME_MAX:
        s = s[:NAME_MAX].rstrip(". ")
    if not s:
        s = UNKNOWN
    if s.upper() in _RESERVED:
        s = s + "_"
    key = s.lower()
    if key in taken and taken[key] != name:
        c = zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF
        s = "%s~%08x" % (s, c)
        key = s.lower()
        n = 0
        while key in taken and taken[key] != name:
            n += 1
            s = "%s~%08x_%d" % (s.split("~")[0], c, n)
            key = s.lower()
    taken[key] = name
    return s


class Writers:
    """按需打开、LRU 关掉。两万本刊不可能同时持有句柄（Windows 默认上限远低于此）。

    **重开必须用 append**：一本刊的行在 JSONL 里不连续，被 LRU 挤掉后还会再遇到。
    第二次用 "w" 打开会把之前写的内容截断（09-28 实测 Zootaxa 853→762 字节）。
    第一次打开才用 "w"，用来清掉上一轮的旧产物，这样重复跑同一份数据不会重复追加。
    """

    def __init__(self, root, dry=False, max_open=MAX_OPEN):
        self.root = root
        self.dry = dry
        self.max_open = max_open
        self.h = collections.OrderedDict()
        self.seen = set()
        self.path_of = {}

    def write(self, safe, line):
        if self.dry:
            return
        f = self.h.get(safe)
        if f is None:
            if not os.path.isdir(self.root):
                os.makedirs(self.root, exist_ok=True)
            p = os.path.join(self.root, safe + ".jsonl")
            self.path_of[safe] = p
            try:
                f = io.open(p, "a" if safe in self.seen else "w",
                            encoding="utf-8", newline="\n")
            except OSError:
                # 句柄开太多时先全关一遍再试，别让一次 OSError 打断整轮。
                self.close()
                f = io.open(p, "a" if safe in self.seen else "w",
                            encoding="utf-8", newline="\n")
            self.seen.add(safe)
            self.h[safe] = f
            if len(self.h) > self.max_open:
                _k, old = self.h.popitem(last=False)
                old.close()
        else:
            self.h.move_to_end(safe)
        f.write(line)

    def close(self):
        for f in self.h.values():
            try:
                f.close()
            except Exception:
                pass
        self.h.clear()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", nargs="*", help="只做这些月份，如 2026-09")
    ap.add_argument("--dry", action="store_true", help="只统计，不写文件")
    ap.add_argument("--only", nargs="*", help="只重做这些刊名，其余条目沿用原清单（局部修修补）")
    ap.add_argument("--clean", action="store_true", help="先删掉输出目录里的旧 .jsonl")
    a = ap.parse_args()

    only = set(a.only or [])
    if a.clean and not a.dry and os.path.isdir(OUT_ROOT):
        n = 0
        for e in os.scandir(OUT_ROOT):
            if e.name.endswith(".jsonl"):
                try:
                    os.remove(e.path)
                    n += 1
                except OSError:
                    pass
        log("[split] --clean 删除旧文件", n, "个")

    files = []
    for name in sorted(os.listdir(sc.OUT_DIR)):
        m = MONTH_RE.match(name)
        if not m:
            continue
        if a.months and m.group(1) not in a.months:
            continue
        files.append(os.path.join(sc.OUT_DIR, name))
    if not files:
        log("[split] 没有找到 scopus_YYYY-MM.jsonl")
        return 1

    taken = {}
    safe_of = {}
    w = Writers(OUT_ROOT, dry=a.dry)
    counts = {}
    bad = 0
    n_all = 0
    t0 = time.time()
    for path in files:
        n_in = 0
        with io.open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                n_in += 1
                n_all += 1
                if n_all % 50000 == 0:
                    log("[split] 进度 %d 行 / 刊名 %d 本 / 已开 %d 个文件 / %.0fs"
                        % (n_all, len(safe_of), len(w.seen), time.time() - t0))
                try:
                    r = json.loads(line)
                except Exception:
                    bad += 1
                    continue
                src = (r.get("source") or "").strip()
                if src.lower() in _JUNK:
                    src = UNKNOWN
                if only and src not in only:
                    continue
                c = counts.get(src)
                if c is None:
                    c = counts[src] = {"n": 0, "eids": set(), "first": "", "last": ""}
                c["n"] += 1
                eid = (r.get("eid") or "").strip()
                if eid:
                    c["eids"].add(eid)
                ld = (r.get("load_date") or "").strip()
                if ld:
                    if not c["first"] or ld < c["first"]:
                        c["first"] = ld
                    if not c["last"] or ld > c["last"]:
                        c["last"] = ld
                safe = safe_of.get(src)
                if safe is None:
                    safe = safe_name(src, taken)
                    safe_of[src] = safe
                w.write(safe, line if line.endswith("\n") else line + "\n")
        log("[split]", os.path.basename(path), "行", n_in)
    w.close()

    out = {}
    if only:
        # 局部重做时把原清单整份带上，只替换这次做的条目，别把别人的记录抹掉。
        try:
            with io.open(COUNTS_PATH, encoding="utf-8") as f:
                out = json.load(f) or {}
            log("[split] --only 载入原清单", len(out), "条")
        except Exception as e:
            log("[split] --only 读原清单失败，将只写本次结果:", str(e)[:80])
            out = {}
    for src, c in counts.items():
        safe = safe_of.get(src)
        out[src] = {
            "n": c["n"],
            "n_eid": len(c["eids"]),
            "first": c["first"],
            "last": c["last"],
            "file": os.path.join("by_journal", safe + ".jsonl") if safe else "",
        }
    if not a.dry:
        tmp = COUNTS_PATH + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, COUNTS_PATH)

    tot_n = sum(x["n"] for x in out.values())
    tot_e = sum(x["n_eid"] for x in out.values())
    log("[split] 完成 刊名 %d 本 | 记录 %d 行 | 唯一 eid 合计 %d | 坏行 %d | 耗时 %.0fs"
        % (len(out), tot_n, tot_e, bad, time.time() - t0))
    log("[split] 输出目录", OUT_ROOT, "（--dry 未落盘）" if a.dry else "")
    log("[split] 计数清单", COUNTS_PATH)
    for src, x in sorted(out.items(), key=lambda kv: -kv[1]["n"])[:20]:
        log("   %-52s %7d 行  eid %7d  %s ~ %s" % (src[:52], x["n"], x["n_eid"],
                                                   x["first"] or "-", x["last"] or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
