# -*- coding: utf-8 -*-
"""按 eid 回读补字段：把 source / year / authors 从网关补回已有列表记录。

为什么要回读
    09-24 之前 scopus_pipeline.parse_docs 有三个映射 bug：
      source  只在 e["source"] 是 dict 时取，字符串形态整条丢 → 20% 记录无刊名
      year    查的是 publicationYear/year，网关字段实际叫 pubYear → 100% 空
      authors 不认 preferredName.{first,full,last} → 100% 空
    修好解析器只能让**新采**的记录正确；已经落盘的记录得回读一遍。

为什么用 EID 批量查
    EID(...) 是合法查询符（引擎在用 NOT EID(...)）。按 12000 字符上限一次能塞
    约 400 个 eid，一条查询拿回 400 条完整记录 —— 比按天重走整棵树便宜两个数量级。

做法
    1. 扫月档，挑出需要补的记录（source/year/authors 任一为空），记 eid -> 行号
    2. 按批查 EID(e1 OR e2 ...)，收集补到的字段
    3. 一次性重写月档，把字段合并进去（不改其它内容）
    4. 断点：已补的 eid 记进 _backfill_done.json，可反复跑
"""
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

os.environ.setdefault("BOOKNOTE_DATA", r"F:\pubmed")
os.environ.setdefault("BOOKNOTE_LOG", r"F:\pubmed\logs")
os.environ.setdefault("SCOPUS_OUT", r"F:\pubmed\scopus")

import scopus_le1000 as L  # noqa: E402
import scopus_pipeline as sc  # noqa: E402

DONE_PATH = os.path.join(sc.OUT_DIR, "scopus_backfill_done.json")
BATCH = int(os.environ.get("BOOKNOTE_SCOPUS_BACKFILL_BATCH") or 400)
MAX_Q = 12000
BUDGET_MIN = float(os.environ.get("BOOKNOTE_SCOPUS_BACKFILL_BUDGET") or 60)

_T0 = [0.0]
_STATS = {"scanned": 0, "need": 0, "queries": 0, "filled": 0, "miss": 0, "err": 0}


def log(*a):
    sc.log("[backfill]", *a)


def budget_left():
    return BUDGET_MIN - (time.time() - _T0[0]) / 60.0


def done_load():
    try:
        with open(DONE_PATH, encoding="utf-8") as f:
            return set(json.load(f) or [])
    except Exception:
        return set()


def done_save(s):
    try:
        tmp = DONE_PATH + ".tmp"
        json.dump(sorted(s)[-400000:], open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, DONE_PATH)
    except Exception as e:
        log("[done-save-err]", str(e)[:80])


def month_files():
    out = []
    try:
        for n in sorted(os.listdir(sc.OUT_DIR)):
            if n.startswith("scopus_") and n.endswith(".jsonl") and len(n) == len("scopus_YYYY-MM.jsonl"):
                out.append(os.path.join(sc.OUT_DIR, n))
    except Exception as e:
        log("[list-err]", str(e)[:80])
    return out


def needs_fix(r):
    return (not (r.get("source") or "").strip()
            or not (r.get("year") or "").strip()
            or not (r.get("authors") or "").strip())


def scan(path, done):
    """返回 [(lineno, eid)]，只挑需要补且没补过的。"""
    todo = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            s = line.strip()
            if not s:
                continue
            _STATS["scanned"] += 1
            try:
                r = json.loads(s)
            except Exception:
                continue
            eid = (r.get("eid") or "").strip()
            if not eid or eid in done:
                continue
            if needs_fix(r):
                todo.append((i, eid))
    return todo


def batch_query(pg, eids):
    """EID(a OR b ...) 一次拿一批。返回 {eid: rec}。"""
    head = "EID("
    body = ""
    used = []
    for e in eids:
        piece = e if not body else " OR " + e
        if len(head) + len(body) + len(piece) + 1 > MAX_Q:
            break
        body += piece
        used.append(e)
    if not used:
        return {}, 0
    q = head + body + ")"
    recs, tot, err = L.query(pg, q)
    _STATS["queries"] += 1
    if err:
        _STATS["err"] += 1
        return {}, len(used)
    out = {}
    for r in recs or []:
        e = (r.get("eid") or "").strip()
        if e:
            out[e] = r
    return out, len(used)


def drop_hold_lock(path):
    try:
        d = json.load(open(path, encoding="utf-8"))
        if int(d.get("pid") or 0) == os.getpid():
            os.remove(path)
            log("[hold-lock]", "已释放")
    except Exception:
        pass


def main():
    _T0[0] = time.time()
    dry = "--dry" in sys.argv
    hold = "--hold-lock" in sys.argv
    lock_path = os.path.join(os.environ.get("BOOKNOTE_LOG") or r"F:\pubmed\logs",
                             "booknote_3h.lock")
    if hold:
        try:
            json.dump({"pid": os.getpid(), "t": time.time(),
                       "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "by": "scopus_backfill"},
                      open(lock_path, "w", encoding="utf-8"))
            log("[hold-lock]", "已占锁 pid", os.getpid())
        except Exception as e:
            log("[hold-lock-err]", str(e)[:80])
    done = done_load()
    log("[start]", "done-so-far", len(done), "batch", BATCH, "budget", BUDGET_MIN, "dry", dry)

    files = month_files()
    log("[files]", [os.path.basename(x) for x in files])
    plan = {}
    for p in files:
        todo = scan(p, done)
        if todo:
            plan[p] = todo
        log("[scan]", os.path.basename(p), "need", len(todo))
    _STATS["need"] = sum(len(v) for v in plan.values())
    log("[need-total]", _STATS["need"])
    if dry or not plan:
        log("[SUMMARY]", json.dumps(_STATS, ensure_ascii=False), "rc 0 (dry)")
        if hold:
            drop_hold_lock(lock_path)
        return 0

    cam, br, app = L.land()
    if app is None:
        if hold:
            drop_hold_lock(lock_path)
        return 3
    rc = 0
    try:
        for path, todo in plan.items():
            log("[file-start]", os.path.basename(path), "need", len(todo))
            # 1) 分批查，攒 eid -> 补到的字段
            patch = {}
            i = 0
            while i < len(todo):
                if budget_left() <= 1.5:
                    log("[budget-stop]", "剩 %.1f 分，已补 %d" % (budget_left(), _STATS["filled"]))
                    rc = 5
                    break
                chunk = [e for _ln, e in todo[i:i + BATCH]]
                got, used = batch_query(app, chunk)
                if not used:
                    break
                # 查过的 eid 一律记进断点：有些记录（比如无作者的条目）本来就补不上，
                # 不记的话每轮都会被重新挑出来重查，永远跑不完。
                for e in chunk[:used]:
                    done.add(e)
                i += used
                for e, r in got.items():
                    if not needs_fix(r):
                        patch[e] = r
                    elif (r.get("source") or "").strip() or (r.get("year") or "").strip():
                        patch[e] = r
                _STATS["filled"] += len(patch)
                log("[batch]", i, "/", len(todo), "got", len(got),
                    "patch", len(patch), "剩 %.1f 分" % budget_left())
            if not patch:
                log("[file-nopatch]", os.path.basename(path))
                continue
            # 2) 一次性重写，把补到的字段合并进去
            tmp = path + ".bf"
            n_fix = 0
            with open(path, encoding="utf-8", errors="replace") as f, \
                    open(tmp, "w", encoding="utf-8") as g:
                for i, line in enumerate(f):
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        r = json.loads(s)
                    except Exception:
                        g.write(line)
                        continue
                    e = (r.get("eid") or "").strip()
                    nr = patch.get(e)
                    if nr:
                        for k in ("source", "year", "authors", "issn", "source_id"):
                            v = nr.get(k)
                            if v not in (None, "") and not (r.get(k) or "").strip():
                                r[k] = v
                        n_fix += 1
                    g.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, path)
            log("[rewrite]", os.path.basename(path), "fixed", n_fix)
            for e in patch:
                done.add(e)
            done_save(done)
    finally:
        try:
            br.close()
        except Exception:
            pass
        done_save(done)
        if hold:
            drop_hold_lock(lock_path)
    log("[SUMMARY]", json.dumps(_STATS, ensure_ascii=False), "rc", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
