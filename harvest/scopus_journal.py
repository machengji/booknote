# -*- coding: utf-8 -*-
"""Scopus 按刊采集：以期刊为主维度，日期只当窗口过滤。

为什么换轴
    日期轴不稳定：Scopus 会改 ORIG-LOAD-DATE，同一篇记录会在天之间搬家，
    按天对账永远追一个移动的窗口。实测缺口一直涨：09-16 133→175、
    09-17 64→102、09-19 60→99、09-20 159→214。
    期刊轴稳定：一篇记录的所属刊不会变。某刊在窗口内拉完即收敛，
    之后只剩新增，不会因为上游改日期而重新开洞。

窗口
    只做 WINDOW_START（默认 2026-09-01）之后入库的记录。
    某刊在窗口内的分母 = EXACTSRCTITLE("刊名") AND ORIG-LOAD-DATE AFT <START-1>。

    注意：这个 totalCount **不是精确分母**。09-28 实测 EXACTSRCTITLE 是「整词匹配」
    而不是整名匹配 —— EXACTSRCTITLE("AIDS") 返回的 299 条里，source 真的是 "AIDS"
    的只有 16 条，其余属于 Mental Health Practitioner S Guide to HIV AIDS(96)、
    AIDS Care...(55)、International Journal of STD and AIDS(14) 等别的刊，
    而它们各有各的键、各自在收敛。这类键的 local 永远追不上 tot，
    表现为「假缺口」。真正的收敛判据是**整遍走完 0 新增**（state 里的 settled），
    不是 local >= tot。追不上的键会被标成 note="tot-inflated" 并给 14 天长复检
    （RECHECK_DAYS_STUCK），免得每 3 天把整窗白走一遍。

与按天引擎的关系
    两条线写同一个 JSONL，去重键同为 (eid, load_date)，互不冲突。
    按刊负责「收敛」，按天负责「发现新刊」（它每天扫全量，能碰到新出现的刊名）。

状态
    scopus_journal_state.json: {刊名: {tot, new, done, ts, note}}
    刊名表用 --seed 从已有 JSONL 的 source 字段播种，采集中发现新刊自动加入。

用法
    python harvest/scopus_journal.py --seed        # 只播种刊名表，不采集
    python harvest/scopus_journal.py               # 按预算采集一批
    python harvest/scopus_journal.py --limit 50    # 本轮最多处理 50 本刊
    python harvest/scopus_journal.py --one "Plos One"   # 只采一本，调试用
"""
import datetime
import json
import os
import re
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

STATE_PATH = os.path.join(sc.OUT_DIR, "scopus_journal_state.json")
INDEX_PATH = os.path.join(sc.OUT_DIR, "scopus_journal_index.json")
WINDOW_START = os.environ.get("BOOKNOTE_SCOPUS_JOURNAL_START") or "2026-09-01"
BUDGET_MIN = float(os.environ.get("BOOKNOTE_SCOPUS_JOURNAL_BUDGET") or 35)
LIMIT = int(os.environ.get("BOOKNOTE_SCOPUS_JOURNAL_LIMIT") or 0)
MAX_DEPTH = int(os.environ.get("BOOKNOTE_SCOPUS_JOURNAL_DEPTH") or 8)
LEAF = 1000
MAX_Q = 12000
PER_JOURNAL_MIN = float(os.environ.get("BOOKNOTE_SCOPUS_JOURNAL_PER") or 6)
# 已完成的刊过这么多天复检一次：窗口是活的，新记录会持续进来。
RECHECK_DAYS_DONE = float(os.environ.get("BOOKNOTE_SCOPUS_JOURNAL_RECHECK") or 3)
# 窗口内 0 条的刊，隔更久再看一次（可能是这本刊这段时间没发文）。
RECHECK_DAYS_EMPTY = float(os.environ.get("BOOKNOTE_SCOPUS_JOURNAL_RECHECK_EMPTY") or 7)
# 「整遍走完 0 新增但 local < tot」的刊（网关分母虚高，见 main 里的说明）。
# 它们已经收敛，只是分母永远追不上；给长复检，别每 3 天把整窗重走一遍。
RECHECK_DAYS_STUCK = float(os.environ.get("BOOKNOTE_SCOPUS_JOURNAL_RECHECK_STUCK") or 14)
# 切分器切不开导致的缺口（note="leak"）：这是**真缺失**，不是网关分母虚高。
# 给最短的复检，让它尽快重试 —— 切分缓存/网关状态变了可能就切开了，
# 也可以让人工介入。绝不拿长复检把它掩盖掉。
RECHECK_DAYS_LEAK = float(os.environ.get("BOOKNOTE_SCOPUS_JOURNAL_RECHECK_LEAK") or 1)
# 有缺口的刊，先扫「最近 N 天」。新记录几乎都在最近几天，扫完若计数之和已等于
# 母格就证明其余天没有记录，一本只要 4 次查询（而不是 30 次）。
# 设 0 就退回「逐天扫全部窗口」的旧行为。
RECENT_DAYS = int(os.environ.get("BOOKNOTE_SCOPUS_RECENT_DAYS") or 3)
# 详情：挂在本线里复用同一个浏览器会话，最后 N 分钟让给详情。
# 绝不另开一次 land() —— 实测两次 land() 相隔 1~2 分钟就把站点顶成
# 「同一帐号同一时刻只能一人在线」（带 cookie 988 字节），详情页就成批拉不到了。
DETAIL_ON = os.environ.get("BOOKNOTE_SCOPUS_DETAIL", "1") == "1"
DETAIL_RESERVE_MIN = float(os.environ.get("BOOKNOTE_SCOPUS_JOURNAL_DETAIL_RESERVE") or 20)
DETAIL_STATE = os.path.join(sc.OUT_DIR, "scopus_detail_state.json")
# 官方来源清单（由 scopus_seed_official.py / 手工放到这里）。每轮开头并进刊名表。
OFFICIAL_TSV = os.path.join(sc.OUT_DIR, "_待探来源.tsv")
# 官方全量清单（49,009 行）。补 ISSN 用 —— 待探清单只有 11,276 行，
# 已有的两万本不在里面，只靠它补不到。
OFFICIAL_FULL = os.path.join(sc.OUT_DIR, "scopus_sources_official.tsv")

_T0 = [0.0]
_JT0 = [0.0]
# 本刊本轮发生 LEAK / hard-limit 的次数。用来区分两类「有缺口但 new==0」：
#   * 网关分母虚高（真假缺口）→ 标 tot-inflated，14 天复检，别再白跑
#   * 切分器切不开导致收不全（真缺口）→ 绝不能标假缺口，否则缺口被永久掩盖
# harvest_cell 走 LEAK 路径时返回的 complete 恒为 True，光看 complete 分不出来，
# 所以必须单独数这个。
_JLEAK = [0]
# {ISSN: set(eid)}，由 local_index() 填充。官方来源清单用 ISSN，精确、不串刊。
_BY_ISSN = {}
# {刊名: source_id}，同样由 local_index() 填。官方清单里的 ISSN 按 source_id 查，
# 比按刊名匹配可靠 —— 实测 17,525 个交集里只有 14,180 个刊名完全一致，
# 剩下 3,169 个有大小写/标点差异（`Plos One` vs `PLOS One`、
# `Business Theory and Practice` vs `Business: Theory and Practice`）。
_BY_SRC_SID = {}
_STATS = {"journals": 0, "done": 0, "empty": 0, "partial": 0, "settled": 0,
          "new": 0, "leaks": 0, "err": 0}


def log(*a):
    sc.log("[journal]", *a)


def budget_left():
    return BUDGET_MIN - (time.time() - _T0[0]) / 60.0


class HttpFail(RuntimeError):
    pass


# ------------------------------------------------------------------ 状态

def state_load():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def state_save(st):
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log("[state-save-err]", str(e)[:80])


def window_clause():
    d = datetime.date.fromisoformat(WINDOW_START) - datetime.timedelta(days=1)
    return "ORIG-LOAD-DATE AFT %s" % d.strftime("%Y%m%d")


def is_junk(name):
    """播种时从记录里带出来的脏值，问网关纯属浪费查询。"""
    n = (name or "").strip().lower()
    return (not n) or n in ("undefined", "null", "none", "nan", "-", "n/a", "na", "unknown")


def journal_due(v):
    """这本刊现在该不该跑。已完成的按 recheck_after 定期复检——窗口是活的，
    新记录会持续进来，一次 done 不能永久跳过。"""
    if not v.get("done"):
        return True
    return time.time() >= float(v.get("recheck_after") or 0)


def seed_from_jsonl(months=None):
    """从已有 JSONL 的 source 字段播种刊名表。一次性操作。"""
    st = state_load()
    added = 0
    paths = []
    try:
        for name in os.listdir(sc.OUT_DIR):
            m = re.match(r"^scopus_(\d{4}-\d{2})\.jsonl$", name)
            if not m:
                continue
            if months and m.group(1) not in months:
                continue
            paths.append(os.path.join(sc.OUT_DIR, name))
    except Exception as e:
        log("[seed-list-err]", str(e)[:80])
    for p in sorted(paths):
        n0 = added
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                s = (r.get("source") or "").strip()
                if s and not is_junk(s) and s not in st:
                    st[s] = {"tot": None, "new": 0, "done": False, "ts": "", "note": "seeded"}
                    added += 1
        log("[seed-file]", os.path.basename(p), "new", added - n0)
    state_save(st)
    log("[seed]", "files", len(paths), "new", added, "total", len(st))
    return added


# ------------------------------------------------------------------ 切分

def parts_ok(parts, tot):
    if not parts:
        return False
    tots = [int(p[2] or 0) for p in parts]
    sm = sum(tots)
    if sm < 1:
        return False
    if tot and sm < int(tot) - 5:
        return False
    if tots and max(tots) >= int(tot) - 2 and int(tot) > LEAF:
        return False
    return True


def _q(pg, q):
    recs, tot, err = L.query(pg, q)
    if err:
        raise HttpFail(q[:60])
    return recs, int(tot or 0)


def _exact_parts(pg, q, tot, tag, specs):
    """specs = [(value, clause)]，clause 是接在 q 后面的条件（不含 q）。

    先逐个取值计数，再用**正向子句的补集**兜余项。带 NOT 的子句不进补集
    （否则会拼出 `AND NOT NOT DOCTYPE(...)` 这种非法查询）。
    """
    parts, pos, sm = [], [], 0
    for v, clause in specs:
        sq = "%s AND %s" % (q, clause)
        recs, n = _q(pg, sq)
        log("[%s]" % tag, v, n)
        if n:
            parts.append(("%s:%s" % (tag[0].upper(), v), sq, n))
            sm += n
            if not clause.strip().upper().startswith("NOT "):
                pos.append(clause)
        if tot and sm >= tot:
            break
    rest = int(tot or 0) - sm
    if rest > 0 and pos:
        rest_q = q
        for c in pos:
            rest_q += " AND NOT " + c
        if len(rest_q) <= MAX_Q:
            recs, n = _q(pg, rest_q)
            n = int(n or rest)
            log("[%s] rest" % tag, n)
            parts.append(("%s:rest" % tag[0].upper(), rest_q, n))
    log("[%s-sum]" % tag, sum(p[2] for p in parts), "vs", tot)
    return parts


def _year_specs():
    y0 = datetime.date.today().year + 1
    return [(str(y), "PUBYEAR = %d" % y) for y in range(y0, y0 - 14, -1)]


def _doctype_specs():
    out = [(t, "DOCTYPE(%s)" % t) for t in sc.DOCTYPES]
    out.append(("rest", "NOT DOCTYPE(%s)" % " OR ".join(sc.DOCTYPES)))
    return out


def _access_specs():
    return [("OA", "ACCESSTYPE(OA)"), ("nOA", "NOT ACCESSTYPE(OA)")]


def _srctype_specs():
    return [("j", "SRCTYPE(j)"), ("p", "SRCTYPE(p)"),
            ("rest", "NOT SRCTYPE(j) AND NOT SRCTYPE(p)")]


def _lang_specs():
    langs = ["english", "chinese", "japanese", "german", "french",
             "spanish", "russian", "portuguese", "korean", "italian"]
    out = [(x[:2], "LANGUAGE(%s)" % x) for x in langs]
    out.append(("rest", "NOT LANGUAGE(%s)" % " OR ".join(langs)))
    return out


def _stage_specs():
    return [("aip", "PUBSTAGE(aip)"), ("final", "PUBSTAGE(final)"),
            ("rest", "NOT PUBSTAGE(aip) AND NOT PUBSTAGE(final)")]


def _firstauth_specs():
    """按首作者姓氏**两字符**前缀分片。默认关闭，见 FA_ON。

    实测（09-29，格子 = Advances in Neural Information Processing Systems @ 09-02，
    5,822 条）：
      * **单字符通配被网关全局拒绝**：`FIRSTAUTH(a*)` → 400 BAD_REQUEST
        ("The query term 'a*' in field FIRSTAUTH ...")。`AUTHLASTNAME(a*)` 同样。
        这解释了早先「FIRSTAUTH 通配不通」的结论 —— 只对单字符成立。
      * **两字符可用**：`FIRSTAUTH(li*)` → 542 条，与本地算出的 541 吻合。
      * 但代价高：该格子有 **231 个不同的两字符前缀 → 231 次查询 ≈ 21 分钟**，
        而「退化到按天」只要 50~100 次 ≈ 5~9 分钟。而且单刊每轮上限
        PER_JOURNAL_MIN=6 分钟，231 次要跨 4 轮，退化只要 1~2 轮。
        所以**默认走退化，这个轴关着**；确实需要时再开 BOOKNOTE_SCOPUS_FIRSTAUTH=1。

    余项片交给 _exact_parts 自己补（它按「已查过的正向子句」拼 NOT 串），
    不要在这里显式给 676 项的大 OR —— 会超过 MAX_Q。
    """
    az = [chr(c) for c in range(ord("a"), ord("z") + 1)]
    return [("%s%s" % (a, b), "FIRSTAUTH(%s%s*)" % (a, b))
            for a in az for b in az]


# 默认关：实测它比「退化到按天」贵 3~4 倍（见 _firstauth_specs 的注释）。
FA_ON = os.environ.get("BOOKNOTE_SCOPUS_FIRSTAUTH", "0") == "1"

AXES = [
    ("pubyear", "PUBYEAR", _year_specs),
    ("doctype", "DOCTYPE(", _doctype_specs),
    ("access", "ACCESSTYPE(", _access_specs),
    ("srctype", "SRCTYPE(", _srctype_specs),
    ("language", "LANGUAGE(", _lang_specs),
    ("pubstage", "PUBSTAGE(", _stage_specs),
]
if FA_ON:
    AXES.append(("firstauth", "FIRSTAUTH(", _firstauth_specs))


def parts_for(pg, q, tot):
    """把一个 >1000 的格子切成近乎完备的几块；都切不动返回 []。

    两级切分：
      1. **便宜轴**（年/文档类型/OA/来源类型/语种/出版阶段）—— 每个轴几次探针
      2. **重武器**：DOI 种子前缀 → DOI 前缀字典树下钻

    为什么要补第 2 级：按刊线原来只有第 1 级，而实测 1,114 次 [split-skip]、
    0 次 [split-ok] —— 说明光靠便宜轴切不开大格子。按天线（scopus_le1000）
    有完整的 9 个切分器，其中 DOI 前缀字典树能一路下钻（`10.1109*` →
    `10.1109/ac*` → …），是切开「同一刊同一天几千条」那种会议录格子的关键。
    这里**直接复用按天线的实现**（它们内部调的就是同一个 L.query），不复制代码，
    免得两边漂移。

    单查询只能看见前 5000 条（offset 被网关忽略），所以 >5000 的格子**必须**
    切开，切不开就是真丢数据 —— 这种情况会单独打 [hard-limit]。
    """
    for tag, marker, mk in AXES:
        if marker in q:
            continue
        bad0 = sc.bad_query_count()
        try:
            parts = _exact_parts(pg, q, tot, tag, mk())
        except HttpFail:
            # 400 说明「这一刀不合法」（例如网关不认 FIRSTAUTH(x*) 这种通配），
            # 不是会话坏了 —— 换下一个轴，别把整本刊带崩。
            if sc.bad_query_count() > bad0:
                log("[split-badq]", tag, "查询不合法，换下一个轴")
                continue
            raise
        if parts_ok(parts, tot):
            return parts
        log("[split-skip]", tag, "n", len(parts),
            "sum", sum(p[2] for p in parts) if parts else 0, "parent", tot)
    for tag, fn, need_pfx in (("doi", L.split_doi, False),
                              ("doi-prefix", L.split_doi_prefix, True)):
        # doi 只切没有 DOI 子句的格子；doi-prefix 只在已有 DOI(xxx*) 上继续下钻
        if need_pfx and not re.search(r"AND DOI\([^)]*\*\)", q):
            continue
        if tag == "doi" and re.search(r"AND DOI\(10\.", q):
            continue
        try:
            parts = fn(pg, q, tot)
        except L.HttpFail:
            raise HttpFail(q[:60])
        except Exception as e:
            log("[split-err]", tag, str(e)[:70])
            continue
        if parts_ok(parts, tot):
            return parts
        log("[split-skip]", tag, "n", len(parts),
            "sum", sum(p[2] for p in parts) if parts else 0, "parent", tot)
    return []


def pack_not_eid(base_q, eids, max_q=MAX_Q):
    used = []
    head = (base_q or "") + " AND NOT EID("
    body = ""
    for e in eids:
        e = (e or "").strip()
        if not e:
            continue
        piece = e if not body else " OR " + e
        if len(head) + len(body) + len(piece) + 1 > max_q:
            break
        body += piece
        used.append(e)
    if not used:
        return base_q, 0
    return head + body + ")", len(used)


def peel(pg, q, have, day, name, rounds=8):
    """第一页 + 反复 NOT EID(已取) 再取一页，直到这一页装不满。

    终止条件是「本页条数 < LEAF」而不是「new == 0」：本页全是已知记录**不等于**
    没有更多记录，只是说明 NOT 串还没滑过它们。用 new==0 收手会漏掉剩下的。
    实测 Plos One/D:ar 1092 条：round 1 拿满 1000、new 0（都是已知），
    旧逻辑在这里 break，剩下 92 条永远拿不到。
    """
    added, excluded, cur = 0, [], q
    for r in range(rounds):
        recs, tot = _q(pg, cur)
        new = L.append(recs, have, day)
        added += new
        log("[peel]", name[:40], "round", r + 1, "tot", tot, "page", len(recs),
            "new", new, "excl", len(excluded))
        eids = [(x.get("eid") or "").strip() for x in (recs or [])]
        eids = [e for e in eids if e]
        if not eids or len(recs or []) < LEAF:
            break
        for e in eids:
            if e not in excluded:
                excluded.append(e)
        cur, nused = pack_not_eid(q, excluded)
        if not nused:
            break
    return added


def harvest_cell(pg, q, tot, have, day, name, depth=0, day_q=None):
    """day_q：不含刊名选择器的「这一天」查询。格子切不开时用它退化。

    为什么需要退化：按刊线的格子是「(刊, 天)」，可能**太单一**而切不动。
    实测 09-02 的 Advances in Neural Information Processing Systems：5,822 条，
    0% 有 DOI、99.98% 是 cp、100% 是 2025 年 —— 加什么条件都还是那 5,822 条，
    所有轴全无效。而**同样这些记录放在「这一天」的格子里就切得开**：
    09-02 全天 28,879 条，里面几百本刊、各种类型/年份/语种，轴都有区分度。
    所以切不开时别硬啃，把格子换成「这一天」—— 多拉到的记录本来也是我们要的，
    不算浪费（L.append 会按 have 去重，已有的一条不会重复写）。
    """
    tot = int(tot or 0)
    if tot <= 0:
        return 0, True
    if tot <= LEAF:
        recs, tot2 = _q(pg, q)
        new = L.append(recs, have, day)
        log("[leaf]", name[:44], "tot", tot, "page", len(recs), "new", new, "d", depth)
        if len(recs or []) < tot:
            _STATS["leaks"] += 1
            log("[leaf-short]", name[:44], "tot", tot, "got", len(recs or []))
        return new, True
    if depth >= MAX_DEPTH:
        _STATS["leaks"] += 1
        _JLEAK[0] += 1
        log("[LEAK]", name[:44], "tot", tot, "深度用尽")
        return peel(pg, q, have, day, name), True
    if tot > 5000:
        # 单查询只能看见前 5000 条（offset 被网关忽略），所以 >5000 的格子
        # 必须切开，切不开就是真丢数据。单独标出来便于巡检时一眼看到。
        log("[bigcell]", name[:44], "tot", tot, ">5000 必须切开")
    parts = parts_for(pg, q, tot)
    if not parts and day_q and tot > LEAF:
        # 退化：格子太单一切不开 → 改把「这一天」当格子（天的格子含很多刊，切得开）
        try:
            _recs, dtot = _q(pg, day_q)
        except HttpFail:
            raise
        dtot = int(dtot or 0)
        if dtot > 0:
            log("[fallback-day]", name[:40], "格子", tot, "切不开 → 改按天采，全天", dtot)
            _STATS["fallback_day"] = _STATS.get("fallback_day", 0) + 1
            return harvest_cell(pg, day_q, dtot, have, day, name + "/DAY", depth + 1)
    if not parts:
        _STATS["leaks"] += 1
        _JLEAK[0] += 1
        if tot > 5000:
            _STATS["hard_limit"] = _STATS.get("hard_limit", 0) + 1
            log("[hard-limit]", name[:44], "tot", tot,
                "所有切分器失败且 >5000 —— 会丢数据，需人工看")
        else:
            log("[LEAK]", name[:44], "tot", tot, "所有切分器失败")
        return peel(pg, q, have, day, name), True
    log("[split-ok]", name[:44], "n", len(parts), "parent", tot,
        "sum", sum(p[2] for p in parts), "d", depth)
    added, complete = 0, True
    for ptag, sq, n in parts:
        if budget_left() <= 1:
            complete = False
            break
        # 单刊时间上限：一本大刊不许把整轮预算吃光，没走完的下轮接着来。
        if _JT0[0] and (time.time() - _JT0[0]) / 60.0 > PER_JOURNAL_MIN:
            log("[per-journal-stop]", name[:40], "%.0f 分钟到" % PER_JOURNAL_MIN)
            complete = False
            break
        sub, sub_ok = harvest_cell(pg, sq, n, have, day, name + "/" + ptag, depth + 1, day_q)
        added += sub
        complete = complete and sub_ok
    return added, complete


def local_index():
    """{刊名: set(窗口内已有 eid)}，从月档扫一次，落盘缓存。

    网关的 documents/search 响应**不含 ORIG-LOAD-DATE**（parse_docs 也不解析它），
    所以按刊线拿到记录后不知道往哪天写 —— 这就是按刊线一条都写不进去的原因。
    有了这个索引才能先对账：本地数 >= 网关数的刊直接收敛，一页都不取。

    同时建 {ISSN: set(eid)} 放进模块级 _BY_ISSN。官方来源清单给的是 ISSN，
    用它查/对账比刊名精确：EXACTSRCTITLE 是整词匹配，会把别的刊也算进来
    （实测 EXACTSRCTITLE("AIDS") 的 299 条里只有 16 条 source 真是 AIDS）。
    """
    files = []
    try:
        for n in sorted(os.listdir(sc.OUT_DIR)):
            if (n.startswith("scopus_") and n.endswith(".jsonl")
                    and len(n) == len("scopus_YYYY-MM.jsonl")):
                files.append(os.path.join(sc.OUT_DIR, n))
    except Exception as e:
        log("[index-list-err]", str(e)[:80])
    stamp = {os.path.basename(p): int(os.path.getmtime(p)) for p in files}
    try:
        old = json.load(open(INDEX_PATH, encoding="utf-8"))
    except Exception:
        old = {}
    if old.get("stamp") == stamp and old.get("idx"):
        _BY_ISSN.clear()
        _BY_ISSN.update({k: set(v) for k, v in (old.get("by_issn") or {}).items()})
        _BY_SRC_SID.clear()
        _BY_SRC_SID.update(old.get("src_sid") or {})
        log("[index-cache]", "journals", len(old["idx"]), "issn", len(_BY_ISSN),
            "src_sid", len(_BY_SRC_SID))
        return {k: set(v) for k, v in old["idx"].items()}

    idx = {}
    by_issn = {}
    src_sid = {}
    n = 0
    for p in files:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                n += 1
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                src = (r.get("source") or "").strip()
                eid = (r.get("eid") or "").strip()
                if not src or not eid:
                    continue
                idx.setdefault(src, set()).add(eid)
                iss = (r.get("issn") or "").strip().replace("-", "").upper()
                if iss:
                    by_issn.setdefault(iss, set()).add(eid)
                sid = (r.get("source_id") or "").strip()
                if sid and src not in src_sid:
                    src_sid[src] = sid
    _BY_ISSN.clear()
    _BY_ISSN.update(by_issn)
    _BY_SRC_SID.clear()
    _BY_SRC_SID.update(src_sid)
    tmp = INDEX_PATH + ".tmp"
    json.dump({"stamp": stamp, "idx": {k: sorted(v) for k, v in idx.items()},
               "by_issn": {k: sorted(v) for k, v in by_issn.items()},
               "src_sid": src_sid},
              open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, INDEX_PATH)
    log("[index-build]", "records", n, "journals", len(idx))
    return idx


def window_days():
    d = datetime.date.fromisoformat(WINDOW_START)
    end = datetime.date.today()
    out = []
    while d <= end:
        out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def day_clause(day):
    d = datetime.date.fromisoformat(day)
    prev = (d - datetime.timedelta(days=1)).strftime("%Y%m%d")
    nxt = (d + datetime.timedelta(days=1)).strftime("%Y%m%d")
    return "ORIG-LOAD-DATE AFT %s AND ORIG-LOAD-DATE BEF %s" % (prev, nxt)


def _scan_days(pg, name, sel, days, have):
    """扫这些天，返回 (added, complete, covered)。

    covered = 这些天的网关计数之和。调用方用它判断「其余天还有没有记录」——
    窗口的每一天是互斥的、合起来正好等于母格，所以只要「已扫天的计数之和 == tot」
    就证明没扫的天一条记录都没有，不用再花查询去扫。
    """
    added, covered, complete = 0, 0, True
    for d in days:
        if budget_left() <= 1:
            return added, False, covered
        dc = day_clause(d)
        q = '%s AND %s' % (dc, sel)
        try:
            recs, td = _q(pg, q)
        except HttpFail:
            raise
        covered += int(td or 0)
        if td <= 0:
            continue
        _JT0[0] = time.time()
        try:
            # day_q = 不含刊名的「这一天」查询，喂给 harvest_cell 做切不开时的退化
            sub, ok = harvest_cell(pg, q, td, have, d, "%s@%s" % (name[:30], d), 0, dc)
        except HttpFail:
            raise
        if sub:
            added += sub
            log("[fill-day]", name[:34], d, "tot", td, "new", sub)
        if not ok:
            return added, False, covered
    return added, True, covered


def fill_short_journal(pg, name, have, tot, local_n, days, issn=None):
    """本地少于网关的刊：定位缺的那几天并补。

    **先扫最近 RECENT_DAYS 天，再按需要扫其余天。** 为什么这么改：
    原来逐天扫全部 29 天，一本有缺口的刊要 30 次查询，这是按刊线最大的成本 ——
    实测平均 19 次/本，一轮 3.5 万本要 174 天，**跟不上一万一千条的日增**
    （实测 09-25 之后每天只采到几百条，累计缺了约 5 万条）。

    而记录的分布是极偏的：新记录几乎都落在最近几天，历史缺口在前几轮已经补过。
    所以：

        1. 窗口计数                             1 次
        2. 扫最近 RECENT_DAYS 天                 3 次
        3. covered == tot ? → 完事（4 次搞定）   ← 绝大多数情况
        4. 还缺 → 扫其余的天（缺口在历史某天）     +26 次

    第 3 步的判据是精确的，不是估算：窗口的每一天互斥且合起来 == 母格，
    所以「已扫天的计数之和 == tot」等价于「没扫的天全是 0」。
    """
    esc = name.replace("\\", "\\\\").replace('"', '\\"')
    sel = "ISSN(%s)" % issn if issn else 'EXACTSRCTITLE("%s")' % esc
    n_recent = max(0, int(RECENT_DAYS))
    if n_recent and len(days) > n_recent:
        recent, older = days[-n_recent:], days[:-n_recent]
    else:
        recent, older = list(days), []
    # 最近几天按**倒序**扫（最新的先做）——预算中途用尽时，最要紧的新记录已经拿到
    added, ok, covered = _scan_days(pg, name, sel, list(reversed(recent)), have)
    if not ok:
        return added, False
    if older and covered < tot:
        log("[scan-older]", name[:34], "最近 %d 天 %d < tot %d，再扫其余 %d 天"
            % (len(recent), covered, tot, len(older)))
        a2, ok2, _ = _scan_days(pg, name, sel, older, have)
        added += a2
        ok = ok and ok2
    return added, ok


def harvest_journal(pg, name, have, idx, issn=None):
    """采一本刊在窗口内的记录。返回 (tot, new, complete)。

    三步：
        1. 窗口计数（1 次查询）
        2. 本地 unique eid 数 >= 网关数 → 收敛，不取页
        3. 真的少 → 按天定位并补

    issn 非空时用 ISSN 查（官方来源清单给的就是 ISSN，精确且不会像
    EXACTSRCTITLE 那样把别的刊连带算进来），本地计数也走 ISSN 索引。
    """
    esc = name.replace("\\", "\\\\").replace('"', '\\"')
    if issn:
        q = "%s AND ISSN(%s)" % (window_clause(), issn)
    else:
        q = '%s AND EXACTSRCTITLE("%s")' % (window_clause(), esc)
    recs, tot = _q(pg, q)
    if tot <= 0:
        return 0, 0, True, 0
    local_n = len(_BY_ISSN.get(issn) or ()) if issn else len(idx.get(name) or ())
    if local_n >= tot:
        log("[settled]", name[:40], "local", local_n, ">= tot", tot)
        return tot, 0, True, 0
    log("[short]", name[:40], "local", local_n, "< tot", tot, "缺", tot - local_n)
    _JLEAK[0] = 0
    new, complete = fill_short_journal(pg, name, have, tot, local_n, window_days(), issn)
    return tot, new, complete, _JLEAK[0]


def merge_official(st):
    """把官方来源清单并进刊名表。幂等，返回新增数。

    为什么要在引擎里做：按刊线的刊名表原来只从「采到的记录」播种（--seed 读
    JSONL 的 source 字段），这是**循环依赖** —— 从未采到的刊永远发现不了。
    官方 ext_list（49,009 个来源，其中 28,370 个覆盖到 2026）是权威全集，
    每轮开头并一次，新刊 tot=None 在 main() 的排序里天然排最前面，
    所以不需要另外做优先级队列。

    两件事：
      1. **补 ISSN**：已有条目按 `_BY_SRC_SID`（刊名 -> source_id，来自记录）
         去官方表里查 ISSN。**必须走 source_id 不能走刊名** —— 实测 17,525 个
         交集里只有 14,180 个刊名完全一致，剩下 3,169 个有大小写/标点差异
         （`Plos One` vs `PLOS One`、`Business Theory and Practice` vs
         `Business: Theory and Practice`）。补上 ISSN 后查询就走 ISSN，
         不再是 EXACTSRCTITLE 的整词匹配，假缺口问题跟着消失。
      2. **加新刊**：待探清单里不在表里的，带 ISSN 加进去（不动已有状态）。
    """
    # 1) 官方表：source_id -> issn
    off_issn = {}
    try:
        with open(OFFICIAL_FULL, encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) < 10 or p[0] == "source_id":
                    continue
                iss = (p[2] or p[3]).replace("-", "").strip().upper()
                if iss:
                    off_issn[p[0]] = iss
    except Exception as e:
        log("[official-full-err]", str(e)[:100])
    patched = 0
    if off_issn:
        for k, v in st.items():
            if v.get("issn"):
                continue
            sid = _BY_SRC_SID.get(k)
            if sid and off_issn.get(sid):
                v["issn"] = off_issn[sid]
                patched += 1

    # 2) 待探清单 -> 新刊
    added = 0
    if os.path.isfile(OFFICIAL_TSV):
        try:
            with open(OFFICIAL_TSV, encoding="utf-8") as f:
                for line in f:
                    p = line.rstrip("\n").split("\t")
                    if len(p) < 6 or p[0] == "source_id":
                        continue
                    sid, title = p[0], p[1].strip()
                    issn = (p[2] or p[3]).replace("-", "").strip().upper()
                    if not title:
                        continue
                    cur = st.get(title)
                    if cur is None:
                        st[title] = {"tot": None, "new": 0, "done": False, "ts": "",
                                     "note": "official-seed", "issn": issn,
                                     "src_id": sid, "src_type": p[5],
                                     "src_active": p[4]}
                        added += 1
                    elif issn and not cur.get("issn"):
                        cur["issn"] = issn
                        patched += 1
        except Exception as e:
            log("[official-merge-err]", str(e)[:100])
    if added or patched:
        log("[official-merge]", "新增", added, "补issn", patched, "共", len(st))
    return added


def run_details(app, left):
    """用**同一个**浏览器会话跑一轮详情。天从列表月档自己算，不读按天线的产物。"""
    import scopus_details as det
    verify = det.verify_from_list()
    days = det.complete_days(verify)
    if not days:
        log("[detail-skip]", "列表月档里没有可拉的天")
        return
    log("[detail-start] 天数", len(days), "小天优先", days[:6], "剩余 %.1f 分" % left())
    out = det.run(app, verify, left, max_n=int(os.environ.get(
        "BOOKNOTE_SCOPUS_DETAIL_MAX") or det.DETAIL_MAX))
    new = sum(int(s.get("new") or 0) for s in (out or {}).values())
    fail = sum(int(s.get("fail") or 0) for s in (out or {}).values())
    left_n = sum(int(s.get("left") or 0) for s in (out or {}).values())
    _STATS["detail_new"] = new
    _STATS["detail_fail"] = fail
    log("[detail-summary] new", new, "fail", fail, "本批剩余", left_n,
        "剩余 %.1f 分" % left())
    try:
        with open(DETAIL_STATE, "w", encoding="utf-8") as f:
            json.dump({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "new": new,
                       "fail": fail, "left_in_batch": left_n,
                       "days": {k: v for k, v in (out or {}).items()}},
                      f, ensure_ascii=False, indent=1)
    except Exception as e:
        log("[detail-state-err]", str(e)[:80])


# ------------------------------------------------------------------ 主流程

def main():
    _T0[0] = time.time()
    if "--seed" in sys.argv:
        seed_from_jsonl({datetime.date.today().strftime("%Y-%m")})
        return 0

    one = None
    top = 0
    for i, a in enumerate(sys.argv):
        if a == "--one" and i + 1 < len(sys.argv):
            one = sys.argv[i + 1]
        if a == "--top" and i + 1 < len(sys.argv):
            try:
                top = int(sys.argv[i + 1])
            except Exception:
                top = 0

    st = state_load()
    if one:
        st.setdefault(one, {"tot": None, "new": 0, "done": False, "ts": "", "note": "manual"})
    if not st:
        log("[no-state]", "先跑 --seed 播种刊名表")
        return 2

    # 窗口可能跨月：从 WINDOW_START 到本月都要加载，否则跨月时去重会漏。
    months = set()
    d0 = datetime.date.fromisoformat(WINDOW_START)
    d = d0
    today = datetime.date.today()
    while d <= today:
        months.add(d.strftime("%Y-%m"))
        d = (d.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    have_by_day, _by_day, _uniq = L.load_have(months)
    idx = local_index()
    # 官方来源清单自动并入（幂等）。**必须放在 local_index 之后** ——
    # 补 ISSN 要按 _BY_SRC_SID（刊名->source_id）查，那个映射是 local_index 建的。
    # 新刊 tot=None，排序里天然排最前。
    if not one:
        merge_official(st)
    log("[have]", {k: len(v) for k, v in sorted(have_by_day.items())[:3]},
        "days", len(have_by_day), "months", sorted(months))

    cam, br, app = L.land()
    if app is None:
        return 3
    rc = 0
    try:
        if one:
            order = [one]
        elif top:
            # 挑本地记录最多的 N 本：那是最难验证的一批（缺口最可能藏在这里）
            order = sorted(st.keys(), key=lambda k: -len(idx.get(k) or ()))[:top]
        else:
            order = sorted(st.keys(), key=lambda k: (
                not journal_due(st[k]), -(st[k].get("tot") or 0), k))
        n_due = sum(1 for k in order if journal_due(st.get(k) or {}))
        log("[todo]", "journals", len(order), "due", n_due, "window", WINDOW_START,
            "budget_min", BUDGET_MIN, "limit", LIMIT,
            "detail", DETAIL_ON, "reserve", DETAIL_RESERVE_MIN)
        # 开关打开时给详情留出尾巴；关掉就几乎不留，全给列表。
        reserve = DETAIL_RESERVE_MIN if DETAIL_ON else 2.0
        for name in order:
            if budget_left() <= reserve:
                log("[budget-stop]", "剩余 %.1f 分钟（详情预留 %.0f）" % (
                    budget_left(), reserve))
                break
            if LIMIT and _STATS["journals"] >= LIMIT:
                log("[limit-stop]", LIMIT)
                break
            v = st.get(name) or {}
            if is_junk(name):
                v["note"] = "junk"
                v["done"] = True
                v["recheck_after"] = time.time() + 365 * 86400
                st[name] = v
                continue
            if not one and not journal_due(v):
                continue
            _STATS["journals"] += 1
            t0 = time.time()
            issn = (v.get("issn") or "").strip()
            try:
                tot, new, complete, leaked = harvest_journal(
                    app, name, have_by_day, idx, issn)
            except HttpFail as e:
                kind = sc.bad_kind() or ""
                log("[http-fail]", name[:40], str(e)[:40], kind)
                if kind.startswith(("401", "403")):
                    rc = 4
                    break
                v["note"] = "http:" + kind[:40]
                _STATS["err"] += 1
                st[name] = v
                state_save(st)
                continue
            except Exception as e:
                log("[err]", name[:40], str(e)[:100])
                _STATS["err"] += 1
                rc = rc or 1
                continue
            dt = time.time() - t0
            # 网关的 totalCount **不是精确值**：EXACTSRCTITLE 是「整词匹配」，不是整名匹配。
            # 09-28 实测 EXACTSRCTITLE("AIDS") 返回 299 条，其中 source 真的是 "AIDS" 的
            # 只有 16 条，其余属于 Mental Health Practitioner S Guide to HIV AIDS(96)、
            # AIDS Care...(55)、International Journal of STD and AIDS(14) 等别的刊 ——
            # 它们各有各的键、各自在收敛。所以这个键的 local 永远追不上 tot。
            # 判据「整遍走完 0 新增」已经证明它收敛了，用长复检把它从空跑里摘出去
            # （实测 AIDS 空走一次要 274 秒）。
            local_n = (len(_BY_ISSN.get(issn) or ()) if issn
                       else len(idx.get(name) or ()))
            # **切不开导致的缺口绝不能标成假缺口**：harvest_cell 走 LEAK 路径时
            # 返回的 complete 恒为 True，光看 complete 分不出「网关分母虚高」和
            # 「切分器切不开」。后者是真缺失，标成 tot-inflated 会拿 14 天长复检
            # 把它永久掩盖（实测 5,822 条的会议录格子：0% DOI、99.98% cp、
            # 100% year 2025，所有轴都无效）。所以 leaked>0 时单独标 leak + 短复检。
            stuck = bool(complete and tot > 0 and new == 0 and local_n < tot and not leaked)
            days = RECHECK_DAYS_DONE if tot > 0 else RECHECK_DAYS_EMPTY
            if stuck:
                days = RECHECK_DAYS_STUCK
            elif leaked and tot > local_n:
                days = RECHECK_DAYS_LEAK
                log("[leak-retry]", name[:40], "leaked", leaked,
                    "gap", tot - local_n, "短复检 %.0f 天" % RECHECK_DAYS_LEAK)
            prev_new = v.get("new")
            v.update({"tot": tot, "new": new, "local_n": local_n,
                      "settled": bool(tot > 0 and new == 0),
                      "prev_new": prev_new,
                      "done": bool(complete),
                      "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "note": ("leak" if (leaked and tot > local_n)
                               else "tot-inflated" if stuck
                               else ("ok" if tot > 0 else "empty")),
                      "recheck_after": time.time() + days * 86400})
            st[name] = v
            state_save(st)
            if not complete:
                _STATS["partial"] += 1
            elif tot <= 0:
                _STATS["empty"] += 1
            else:
                _STATS["done"] += 1
                if new == 0:
                    _STATS["settled"] += 1
            _STATS["new"] += new
            log("[journal-done]", name[:46], "tot", tot, "new", new,
                "complete", complete, "%.0fs" % dt, "剩 %.1f 分" % budget_left())

        # ---- 详情：复用同一个浏览器会话。绝不在这里再 land() 一次。
        if DETAIL_ON and budget_left() > 3:
            try:
                run_details(app, budget_left)
            except Exception as e:
                log("[detail-err]", str(e)[:140])
        elif DETAIL_ON:
            log("[detail-skip] 剩余 %.1f 分钟不足" % budget_left())
    finally:
        try:
            br.close()
        except Exception:
            pass
        state_save(st)
    log("[SUMMARY]", json.dumps(_STATS, ensure_ascii=False), "rc", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
