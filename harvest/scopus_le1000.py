# -*- coding: utf-8 -*-
"""Scopus 日采集引擎：绕开网关 5000 条窗口，逐日精确采全。

网关限制
    POST /gateway/documents/search 的 resultSet.offset+itemCount 上限 5000，
    且 offset 分页被网关忽略（offset=1000 会返回与第一页重叠的结果）。
    所以「翻页」这条路在本网关不存在。格子只按三条规则收：
        ≤1000        取第一页
        1001～1500   第一页 + NOT EID(本页) + 再取一页
        >1500        便宜切分（类型/年/语种/阶段/DOI/刊名），禁止 ISSN 187 刀

本文件负责
    1. 落地 scihuber 会话（多入口按序回退）；
    2. 扫描当月每一天（含上月末几天）取 totalCount；
    3. 与本地 JSONL 的 unique eid 对账，只补不足的天；
    4. 遇到 401/403/429/5xx 立即停手（不烧配额），留待下一轮；
    5. 输出 F:\\pubmed\\logs\\scopus_day_verify.json 逐日对账结果；
    6. 列表某天采全后，用本轮剩余预算拉该天详情（同一会话 GET doc-details）。

判定口径
    某天「当前窗口采全」= 该天标签下 unique eid >= **本轮查到的** totalCount。
    绝不用行数判定（历史文件里同一 eid 可能有多行）。
    绝不用「今天缩水后的 totalCount」去否定昨天已经收下的记录：Scopus 会改
    ORIG-LOAD-DATE，同一 eid 会换天。那种「本地比今天 total 多很多」叫
    window-moved，不是多采，也不当缺口。
    入库去重按 (eid, load_date)，不按全局 eid——否则一篇从 09-09 挪到 09-17
    时，09-17 会永远少、09-09 会永远多。
"""
import datetime
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "harvest"))
os.environ.setdefault("BOOKNOTE_DATA", r"F:\pubmed")
os.environ.setdefault("BOOKNOTE_LOG", r"F:\pubmed\logs")
os.environ.setdefault("SCOPUS_OUT", r"F:\pubmed\scopus")

from camoufox.sync_api import Camoufox  # noqa: E402

import scopus_pipeline as sc  # noqa: E402
from wos_pipeline import load_pw_cookies, cookies_still_vip, inject_cookie_list  # noqa: E402

try:
    import issn_buckets
except Exception:  # pragma: no cover
    issn_buckets = None

try:
    import scopus_details as det
except Exception:  # pragma: no cover
    det = None

STATE = sc.STATE
LOG_DIR = os.environ.get("BOOKNOTE_LOG") or os.path.join(sc.DATA, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
VERIFY = os.path.join(LOG_DIR, "scopus_day_verify.json")
LEDGER = os.path.join(sc.OUT_DIR, "scopus_day_ledger.json")
HEALTH = os.path.join(sc.OUT_DIR, "scopus_entry_health.json")
# 本地 unique 比本轮 totalCount 多出这么多，才标 window-moved（不是多采）。
WINDOW_MOVED_MIN = int(os.environ.get("BOOKNOTE_SCOPUS_WINDOW_MOVED") or 50)
# 小缺口「啃不动就降级」：连续这么多轮 gap 一点没缩小，就先搁置一段时间，
# 把 30 分钟单天预算让给大缺口。实测 09-02 差 8 条、09-16 差 342 条各烧了
# 三轮 30 分钟、每轮 new=0，而 09-17/09-18 各有上万条在后面排队。
# 只对小缺口生效（gap <= STUCK_GAP_MAX），大缺口永远不会被搁置。
STUCK_MIN = int(os.environ.get("BOOKNOTE_SCOPUS_STUCK_MIN") or 2)
# 兜底穷尽阈值：断点被清过（没有 pass_done）的天，连续这么多轮零进展也算穷尽。
# 10 轮 = 30 小时没有任何缩小，够硬。
EXHAUST_STUCK_HARD = int(os.environ.get("BOOKNOTE_SCOPUS_EXHAUST_HARD") or 10)
STUCK_GAP_MAX = int(os.environ.get("BOOKNOTE_SCOPUS_STUCK_GAP_MAX") or 1000)
STUCK_MUTE_HOURS = float(os.environ.get("BOOKNOTE_SCOPUS_STUCK_MUTE_HOURS") or 6)
# 同一轮里最多扫几遍（每遍给每个还没齐的天一个 DAY_BUDGET_MIN 窗口）。
MAX_PASS = int(os.environ.get("BOOKNOTE_SCOPUS_MAX_PASS") or 6)
# 入口连续失败多少次后先搁置，避免每轮都在已死的镜像上白等。
ENTRY_FAIL_MUTE = int(os.environ.get("BOOKNOTE_SCOPUS_ENTRY_MUTE") or 3)
ENTRY_MUTE_HOURS = float(os.environ.get("BOOKNOTE_SCOPUS_ENTRY_MUTE_HOURS") or 6)

LEAF = 1000
PEEL_MAX = int(os.environ.get("BOOKNOTE_SCOPUS_PEEL_MAX") or 1500)
MAX_Q = 12000
# DOI 前缀字典树最多允许多少个分支。首页 1000 条里 Elsevier 的 10.1016* 实测有
# 184 个不同扩展（/j.jc、/j.jp …），旧上限 64 会让它整格放弃 → 上层只能给出
# 一个 2630 条的余项格，最终只取 1000 条、静默丢 1630（09-20 实测）。
DOI_PFX_MAX_KEYS = int(os.environ.get("BOOKNOTE_SCOPUS_DOI_PFX_MAX") or 256)
# 切不动的格重试间隔（小时）：记录下来，到点后自动放开重切，避免原地每轮空转。
LEAK_RETRY_HOURS = float(os.environ.get("BOOKNOTE_SCOPUS_LEAK_RETRY_H") or 3)
MAX_DEPTH = 14
START = datetime.date(2026, 9, 1)
SKIP_ENTRY = ("NCU", "南大", "武大")

# 每轮最多跑多久（分钟），到点收手，剩下的交给下一个 3 小时周期。
BUDGET_MIN = float(os.environ.get("BOOKNOTE_SCOPUS_BUDGET_MIN") or 150)
# 列表缺口把整轮吃光时，已齐的天永远轮不到详情。列表补采提前这么多分钟收手，
# 把尾巴留给详情。没有已齐的天时不预留。
DETAIL_RESERVE_MIN = float(os.environ.get("BOOKNOTE_SCOPUS_DETAIL_RESERVE") or 20)
# 单天预算放宽：ISSN 已禁用，1001～1500 两刀就能收完，不必再 30 分钟掐掉大缺口。
DAY_BUDGET_MIN = float(os.environ.get("BOOKNOTE_SCOPUS_DAY_BUDGET_MIN") or 90)
# 单轮允许出现的非 200 响应次数上限，超过即认定会话/配额坏了。
BAD_LIMIT = int(os.environ.get("BOOKNOTE_SCOPUS_BAD_LIMIT") or 6)
# 瞬时故障（5xx / 网络异常 / 429 / 418）每刀重试几次。一次就废掉一整天太脆：
# 09-21 15:07 那轮网关抖动，4 个缺口天全被单个 500 打断，整轮 new=0。
TRANSIENT_RETRY = int(os.environ.get("BOOKNOTE_SCOPUS_TRANSIENT_RETRY") or 3)
TRANSIENT_WAIT_S = float(os.environ.get("BOOKNOTE_SCOPUS_TRANSIENT_WAIT") or 6)
# 天级余项扫尾：一次 NOT EID(...) 按 12000 字符上限约装 500 个 eid，所以按窗口滑。
# 每轮把当天已有 eid 全扫一遍；扫完没新增就收手。
REMAINDER_WIN = int(os.environ.get("BOOKNOTE_SCOPUS_REST_WIN") or 600)
REMAINDER_ROUNDS = int(os.environ.get("BOOKNOTE_SCOPUS_REST_ROUNDS") or 3)
# --fresh：点名补的天先清掉断点与切分缓存，整树重走一遍再扫尾。
FRESH = "--fresh" in sys.argv
# --rewalk：点名补的天只清断点、**保留切分缓存**，让树沿已有切分直达叶子重取一遍。
# 余项格缺失是「旧代码 rest>3 没建余项格」造成的，重取叶子就能补回来；
# 切分结果本身没坏，重探一遍要上千次查询，纯浪费。比 --fresh 便宜一个数量级。
REWALK = "--rewalk" in sys.argv

SEED_DOI = [
    "10.1016*", "10.1007*", "10.1109*", "10.1021*", "10.3389*", "10.1080*",
    "10.3390*", "10.1177*", "10.1111*", "10.1186*", "10.1038*", "10.1371*",
    "10.1093*", "10.1136*", "10.1039*", "10.1126*", "10.1029*", "10.1155*",
    "10.2147*", "10.7759*", "10.7150*", "10.1162*", "10.1097*", "10.1200*",
    "10.2514*", "10.1063*", "10.1145*", "10.1088*", "10.1128*", "10.1530*",
    "10.3322*", "10.3233*", "10.1098*", "10.1103*", "10.1073*", "10.1117*",
    "10.1515*", "10.3394*", "10.1002*", "10.1108*", "10.1017*", "10.1556*",
    "10.1057*", "10.1142*", "10.1163*", "10.4103*", "10.1557*", "10.1161*",
    "10.1681*", "10.1086*", "10.1680*", "10.2174*", "10.1053*", "10.1182*",
    "10.1071*", "10.3934*", "10.1140*", "10.1353*", "10.1152*", "10.2738*",
]

LEAKS = []          # 切不动的格子（>1000 且所有切分器都失败）
BADQ = []           # 被网关 400 拒掉的查询（切分器写错时会在这里现形）
_STARTED = time.time()


def log(*a):
    sc.log("[le1000]", *a)


def budget_left():
    return BUDGET_MIN - (time.time() - _STARTED) / 60.0


def out_for(day):
    return os.path.join(sc.OUT_DIR, "scopus_%s.jsonl" % day[:7])


def day_q(day):
    d = datetime.date.fromisoformat(day)
    prev = (d - datetime.timedelta(days=1)).strftime("%Y%m%d")
    nxt = (d + datetime.timedelta(days=1)).strftime("%Y%m%d")
    return "ORIG-LOAD-DATE AFT %s AND ORIG-LOAD-DATE BEF %s" % (prev, nxt)


def doi_pref(doi):
    m = re.match(r"(10\.\d{4,})", (doi or "").strip().lower())
    return (m.group(1) + "*") if m else None


def day_status(uniq, tot):
    """(ok, note, gap, extra)。ok=当前窗口已覆盖；window-moved 仍 ok。"""
    tot = int(tot or 0)
    uniq = int(uniq or 0)
    if tot <= 0:
        return False, "upstream-empty", 0, uniq
    extra = max(0, uniq - tot)
    gap = max(0, tot - uniq)
    if uniq >= tot:
        if extra >= WINDOW_MOVED_MIN:
            return True, "window-moved", 0, extra
        return True, "", 0, extra
    return False, "short", gap, extra


def ledger_load():
    try:
        with open(LEDGER, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def stuck_muted(day, gap):
    """小缺口连续多轮没进展 -> 本轮先不碰，把预算让给大缺口。

    只对 gap <= STUCK_GAP_MAX 生效；大缺口永远照常处理。
    搁置满 STUCK_MUTE_HOURS 后自动放回来再试一次（缺口缩小即清零）。
    人工点名补的天（ONLY_DAYS）不套用静音：点名的就是要现在补。
    """
    if day in ONLY_DAYS:
        return False
    g = int(gap or 0)
    if g <= 0 or g > STUCK_GAP_MAX:
        return False
    v = ledger_load().get(day) or {}
    if int(v.get("stuck") or 0) < STUCK_MIN:
        return False
    age_h = (time.time() - float(v.get("stuck_ts") or 0)) / 3600.0
    return age_h < STUCK_MUTE_HOURS


def ledger_put(day, tot, uniq, ok, note, extra=0, gap=0, exhausted=False):
    led = ledger_load()
    prev = led.get(day) or {}
    rec = {
        "total_now": int(tot or 0),
        "total_max": max(int(prev.get("total_max") or 0), int(tot or 0)),
        "uniq": int(uniq or 0),
        "ok": bool(ok),
        "note": note or "",
        "gap": int(gap or 0),
        "extra": int(extra or 0),
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if prev.get("total_first") is None and int(tot or 0) > 0:
        rec["total_first"] = int(tot)
    else:
        rec["total_first"] = prev.get("total_first")
    # 缺口没缩小就累加 stuck；一旦缩小立刻清零。stuck_ts 记「最后一次无进展」的时刻。
    pg = int(prev.get("gap") or 0)
    g = int(gap or 0)
    if g > 0 and pg > 0 and g >= pg:
        rec["stuck"] = int(prev.get("stuck") or 0) + 1
        rec["stuck_ts"] = time.time()
    else:
        rec["stuck"] = 0
        rec["stuck_ts"] = 0
    # 已穷尽：整遍切分树走完（pass_done）且连续 STUCK_MIN 轮 0 新增。gap 照实保留，
    # 只多一个状态位，让巡检把「上游 totalCount 虚高」和「真缺口」分开报。
    # 这里算而不是只在日循环里算：被静音的天根本进不了日循环，那样永远标不上。
    # 第二条兜底给「断点被 --fresh/--rewalk 清过、整遍没走完」的天（09-02 就是）：
    # 连续 EXHAUST_STUCK_HARD 轮零进展同样是穷尽的实据。
    rec["exhausted"] = bool(
        exhausted
        or (int(gap or 0) > 0
            and bool((_DONE.get(day) or {}).get("pass_done"))
            and int(rec.get("stuck") or 0) >= STUCK_MIN)
        or (int(gap or 0) > 0 and int(rec.get("stuck") or 0) >= EXHAUST_STUCK_HARD))
    led[day] = rec
    tmp = LEDGER + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(led, f, ensure_ascii=False, indent=1)
    os.replace(tmp, LEDGER)
    return rec


# ---------------------------------------------------------------- 会话落地

def health_load():
    try:
        with open(HEALTH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def health_save(h):
    try:
        json.dump(h, open(HEALTH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass


def health_note(eid, ok, note=""):
    h = health_load()
    v = h.get(str(eid)) or {}
    if ok:
        v = {"fails": 0, "note": "", "last_ok": time.strftime("%Y-%m-%d %H:%M:%S")}
    else:
        v["fails"] = int(v.get("fails") or 0) + 1
        v["note"] = note[:120]
        v["t"] = time.time()
        v["last_fail"] = time.strftime("%Y-%m-%d %H:%M:%S")
    h[str(eid)] = v
    health_save(h)


def entry_usable(eid, h):
    v = h.get(str(eid)) or {}
    if int(v.get("fails") or 0) < ENTRY_FAIL_MUTE:
        return True
    return (time.time() - float(v.get("t") or 0)) > ENTRY_MUTE_HOURS * 3600


def land():
    saved = load_pw_cookies()
    ck = sc.sci_cookies(saved)
    log("reuse vip=", bool(saved and cookies_still_vip(saved)), "ck=", len(ck or []))
    cam = Camoufox(headless=False, exclude_addons=["ublock-origin"])
    br = cam.__enter__()
    pg = br.new_page()
    pg.set_default_timeout(90000)
    try:
        pg.context.add_init_script(sc.ANTI_DEBUG)
    except Exception:
        pass
    pg.on("dialog", lambda d: d.accept())
    inject_cookie_list(pg.context, ck)
    try:
        pg.goto(sc.SITE + "e/member/cp/", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("[cp]", str(e)[:80])
    time.sleep(2)
    if not sc.is_logged(pg):
        # 09-19 实测：会话失效时站点先给一个提示页
        #   「信息提示 同一帐号同一时刻只能一人在线! 如果您的浏览器没有自动跳转，请点击这里」
        # 约 3~5 秒后才自动跳到 /e/member/login/ 的登录表单。
        # 只等 2 秒就会把提示页误判成「有人在别处在线」，还去点它的跳转链接，
        # 白折腾一轮并报错方向。所以先等它自己跳完再判。
        for _ in range(4):
            if sc.is_logged(pg):
                break
            time.sleep(2)
            t0 = sc.page_text(pg) or ""
            if "用户名称" in t0 or "您的密码" in t0 or "会员登录" in t0:
                break
        u = (pg.url or "").lower()
        t0 = sc.page_text(pg) or ""
        login_form = ("member/login" in u
                      or "用户名称" in t0 or "您的密码" in t0 or "会员登录" in t0)
        if not login_form and ("只能一人在线" in t0 or "请点击这里" in t0):
            # 站点自己提供「请点击这里」踢掉另一个在线会话。上一轮被预算 kill 时
            # 服务端会话往往没释放，下一轮就会撞上这个——直接接管，别白丢一整轮。
            log("BUSY_SINGLE_SESSION 尝试接管")
            try:
                sc.takeover_single_session(pg)
            except Exception as e:
                log("[takeover-ex]", str(e)[:80])
            time.sleep(2)
            try:
                pg.goto(sc.SITE + "e/member/cp/", wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log("[cp-retry]", str(e)[:80])
            time.sleep(5)
        if not sc.is_logged(pg):
            u = (pg.url or "").lower()
            t0 = sc.page_text(pg) or ""
            if ("member/login" in u or "用户名称" in t0 or "您的密码" in t0
                    or "会员登录" in t0):
                log("SESSION_EXPIRED cookie 已失效，需要人工登录一次："
                    "python harvest/wos_pipeline.py --wait-login")
            elif "只能一人在线" in t0 or "请点击这里" in t0:
                log("BUSY_SINGLE_SESSION 接管后仍未登录")
            else:
                log("NOT_LOGGED")
            _close(br)
            return None, None, None
    sc.only_one_tab(pg.context, pg)
    box = {}
    sc.attach(pg, box)
    pg.context.on("page", lambda p: sc.attach(p, box))
    # 入口按序回退：一个入口维护/限额/镜像挂掉就换下一个。
    # 已连续失败多次的入口先搁置，避免每轮都白等（过一阵自动恢复尝试）。
    h = health_load()
    cand = [(e, n) for e, n in sc.ENTRIES if any(s in n for s in SKIP_ENTRY) is False]
    live = [x for x in cand if entry_usable(x[0], h)]
    if not live:
        log("[entry-all-muted] 全部入口被搁置，本轮仍然全试一遍")
        live = cand
    for eid, name in live:
        app = None
        try:
            app = sc.open_entry(pg, eid, name)
        except Exception as e:
            log("[open-ex]", name, str(e)[:80])
        if app is not None and app is not sc.NEED_LOGIN:
            log("[using]", name)
            health_note(eid, True)
            try:
                sc.save_sci_only(app.context)
            except Exception:
                pass
            return cam, br, app
        note = "land-fail"
        try:
            note = (sc.page_text(pg) or "").replace("\n", " ")[:100] or note
        except Exception:
            pass
        health_note(eid, False, note)
        log("[entry-fail]", name, note[:80])
    log("FAIL_LAND")
    _close(br)
    return None, None, None


def _close(br):
    try:
        br.close()
    except Exception:
        pass


# ---------------------------------------------------------------- 查询包装

def query(pg, q):
    """返回 (recs, total, err)。

    err=True 只表示会话/配额故障（401/403/429/5xx），由调用方中断当天。
    400 BAD_REQUEST 是「这一刀查询语句不合法」，不是会话坏了：返回空结果，
    让切分器自然切不动、引擎换下一个切分器，绝不能升级成整天中断。

    瞬时故障必须重试。实测 09-21 15:07 那轮：网关先抖了 6 次 NetworkError
    （旧代码把它当空结果吞掉），第 7 次才回 500；而「一次非 200 就废掉当天」
    让 4 个缺口天全部 new=0，整轮白跑。现在 5xx / 网络异常 / 429 / 418 各自
    退避重试 TRANSIENT_RETRY 次；401/403（真没权限）和 400（语法错）不重试。
    """
    # 单天预算也要管住**切分探针**：split_issn 一轮 187 次、split_doi 约 60 次，
    # 它们直接调 query()，不走 harvest()，所以原来 harvest 顶部那道预算检查
    # 根本拦不住。实测 09-19 的窗口跑了 43 分钟仍未收手（30 分钟上限形同虚设），
    # 整轮也被拖到被调度器 kill。把检查放进 query() 本体，每一刀都算数。
    if _day_t0[0] and (time.time() - _day_t0[0]) / 60.0 > DAY_BUDGET_MIN:
        raise DayBudget((q or "")[:40])
    last = ""
    for attempt in range(TRANSIENT_RETRY + 1):
        b0 = sc.bad_count()
        q0 = sc.bad_query_count()
        try:
            recs, tot = sc.api_first(pg, q)
        except Exception as e:
            log("[query-ex]", str(e)[:100], q[:60])
            last = "ex " + str(e)[:60]
            recs, tot = [], None
        else:
            if sc.bad_query_count() > q0:
                BADQ.append(q[:200])
                log("[query-400]", sc.bad_query_kind()[:110], "|", q[:70])
                return [], None, False
            if sc.bad_count() <= b0:
                tot = int(tot) if tot is not None else None
                return list(recs or []), tot, False
            last = sc.bad_kind() or "?"
        if last.startswith(("401", "403")):
            log("[query-auth]", last[:70], q[:60])
            return [], None, True
        if attempt < TRANSIENT_RETRY:
            wait = TRANSIENT_WAIT_S * (attempt + 1)
            log("[query-retry]", attempt + 1, "of", TRANSIENT_RETRY,
                "wait %.0fs" % wait, last[:60])
            time.sleep(wait)
            continue
        log("[query-http]", last[:70], q[:60])
        return [], None, True
    return [], None, True


# ---------------------------------------------------------------- 本地账本

MONTH_FILE_RE = re.compile(r"^scopus_(\d{4}-\d{2})\.jsonl$")


def load_have(months=None):
    """返回 (当天 eid 集合字典, 每日行数, 每日 unique eid 数)。

    只认正式月档 scopus_YYYY-MM.jsonl。目录里还留着探针产物
    （scopus_eid_peel_test.jsonl 等），它们也带 load_date，被 glob 进来会把
    当天的 uniq 抬高——实测 09-16 因此从 25037 虚高到 30555，
    直接越过 totalCount 25276 被误判成「已采全」，239 条缺口就这么留下来了。

    去重键是 (eid, load_date)，不是全局 eid。Scopus 会改 ORIG-LOAD-DATE，
    同一篇从 09-09 挪到 09-17 时，09-17 必须还能收下；用全局 eid 会让
    09-17 永远少、09-09 永远多。
    """
    have_by_day = {}
    by_day = {}
    by_day_eid = {}
    paths = []
    try:
        for name in os.listdir(sc.OUT_DIR):
            m = MONTH_FILE_RE.match(name)
            if not m:
                continue
            if months and m.group(1) not in months:
                continue
            paths.append(os.path.join(sc.OUT_DIR, name))
    except Exception:
        paths = [out_for(datetime.date.today().isoformat())]
    for p in paths:
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    v = json.loads(line)
                except Exception:
                    continue
                k = (v.get("eid") or "").strip()
                d = (v.get("load_date") or "")[:10]
                if d:
                    by_day[d] = by_day.get(d, 0) + 1
                    if k:
                        by_day_eid.setdefault(d, set()).add(k)
                        have_by_day.setdefault(d, set()).add(k)
    uniq = dict((d, len(s)) for d, s in by_day_eid.items())
    return have_by_day, by_day, uniq


def append(recs, have_by_day, day):
    """写入当天月档。have_by_day[day] 是当天已有 eid；跨天同 eid 仍写入。"""
    n = 0
    seen = have_by_day.setdefault(day, set())
    with open(out_for(day), "a", encoding="utf-8") as f:
        for rec in recs:
            k = (rec.get("eid") or "").strip()
            if not k or k in seen:
                continue
            seen.add(k)
            rec["load_date"] = day
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    return n


# ---------------------------------------------------------------- 切分器

def parts_ok(parts, parent):
    """判定一刀是否可用。

    **必须是近乎完备的划分**：早先只要求「子格之和 >= 父格 85%」，那会让
    没有余项子格的切分器（典型是 ISSN 桶——它列不出补集）静默丢掉最多 15%
    的记录，而且表现为「这天总差一点点」，最难查。现在要求近乎精确，
    不够精确就换下一个切分器；都切不动就记 [LEAK]（可见），绝不清默丢。
    """
    if not parts:
        return False
    parent = int(parent or 0)
    tots = [int(p[2] or 0) for p in parts]
    sm = sum(tots)
    mx = max(tots) if tots else 0
    if sm < 1:
        return False
    if parent and sm < parent - 5:
        return False
    if mx >= parent - 2 and parent > LEAF:
        return False
    return True


def split_accesstype(pg, q, tot):
    """开放获取 / 非开放获取。09-22 实测 09-02：11629 + 17260 = 28889 精确二分。

    它和类型/年/语种/阶段四条正交，多一条通路就多一次绕开「字段为空」缝隙的机会。
    只要两刀，比 DOI 前缀树便宜两个数量级。
    """
    if "ACCESSTYPE(" in q:
        return []
    parts, sm = [], 0
    for tag, sq in (("OA", q + " AND ACCESSTYPE(OA)"),
                    ("nOA", q + " AND NOT ACCESSTYPE(OA)")):
        recs, n, err = query(pg, sq)
        if err:
            raise HttpFail("http")
        n = int(n or 0)
        log("[access]", tag, n)
        if n:
            parts.append(("A:" + tag, sq, n))
            sm += n
    log("[access-sum]", sm, "vs", tot)
    return parts


def split_srctype(pg, q, tot):
    """来源类型 j(期刊)/p(会议录)/其余。09-22 实测 09-02：18965+8014+1910 精确。

    与 DOCTYPE 不同轴：同一类型里既有期刊也有会议录，所以它能切到类型切不动的格子。
    """
    if "SRCTYPE(" in q:
        return []
    parts, sm = [], 0
    for t in ("j", "p"):
        sq = "%s AND SRCTYPE(%s)" % (q, t)
        recs, n, err = query(pg, sq)
        if err:
            raise HttpFail("http")
        n = int(n or 0)
        log("[srctype]", t, n)
        if n:
            parts.append(("T:" + t, sq, n))
            sm += n
    rest = (tot or 0) - sm
    if rest > 0:
        sq = q + " AND NOT SRCTYPE(j) AND NOT SRCTYPE(p)"
        recs, n, err = query(pg, sq)
        if err:
            raise HttpFail("http")
        n = int(n or rest)
        log("[srctype] rest", n)
        if n:
            parts.append(("T:rest", sq, n))
    log("[srctype-sum]", sum(p[2] for p in parts), "vs", tot)
    return parts


def split_doctype(pg, q, tot):
    if "DOCTYPE(" in q:
        return []
    parts, sm = [], 0
    for t in sc.DOCTYPES:
        sq = "%s AND DOCTYPE(%s)" % (q, t)
        recs, n, err = query(pg, sq)
        n = int(n or 0)
        log("[dt]", t, n)
        if err:
            raise HttpFail("http")
        if n:
            parts.append(("D:" + t, sq, n))
            sm += n
    sq = "%s AND NOT DOCTYPE(%s)" % (q, " OR ".join(sc.DOCTYPES))
    recs, n, err = query(pg, sq)
    n = int(n or 0)
    log("[dt] rest", n)
    if n:
        parts.append(("D:rest", sq, n))
        sm += n
    log("[dt-sum]", sm, "vs", tot)
    return parts


def split_year(pg, q, tot):
    if "PUBYEAR" in q:
        return []
    y0 = datetime.date.today().year + 1
    years = list(range(y0, y0 - 14, -1))
    parts, used, sm = [], [], 0
    for y in years:
        sq = "%s AND PUBYEAR = %d" % (q, y)
        recs, n, err = query(pg, sq)
        if err:
            raise HttpFail("http")
        n = int(n or 0)
        used.append(y)
        log("[year]", y, n)
        if n:
            parts.append(("Y:%d" % y, sq, n))
            sm += n
        if tot and sm >= tot:
            break
    rest = (tot or 0) - sm
    # rest > 0 就必须建余项格。旧代码写 rest > 3，于是每个年切分都静默丢 1~3 条；
    # 实测 09-02 的 pubyear 求和 9679 vs 父格 9682，差的 3 条正是被这条丢掉的。
    # 余项子句只排除**已经查过的**年份：提前 break 时未查的年份必须留在余项里。
    if rest > 0:
        sq = "%s AND %s" % (q, " AND ".join("NOT PUBYEAR = %d" % y for y in used))
        recs, n, err = query(pg, sq)
        if err:
            raise HttpFail("http")
        n = int(n or rest)
        log("[year] rest", n)
        parts.append(("Y:rest", sq, n))
    log("[year-sum]", sum(p[2] for p in parts), "vs", tot)
    return parts


def split_lang(pg, q, tot):
    if "LANGUAGE(" in q:
        return []
    langs = ["english", "chinese", "japanese", "german", "french",
             "spanish", "russian", "portuguese", "korean", "italian"]
    parts, used, sm = [], [], 0
    for lang in langs:
        sq = "%s AND LANGUAGE(%s)" % (q, lang)
        recs, n, err = query(pg, sq)
        if err:
            raise HttpFail("http")
        n = int(n or 0)
        used.append(lang)
        log("[lang]", lang, n)
        if n:
            parts.append(("L:" + lang[:2], sq, n))
            sm += n
        if tot and sm >= tot:
            break
    rest = (tot or 0) - sm
    # 同 split_year：余项一律要建（rest > 0），且只排除查过的语种。
    if rest > 0:
        sq = "%s AND NOT LANGUAGE(%s)" % (q, " OR ".join(used))
        recs, n, err = query(pg, sq)
        if err:
            raise HttpFail("http")
        n = int(n or rest)
        log("[lang] rest", n)
        parts.append(("L:rest", sq, n))
    log("[lang-sum]", sum(p[2] for p in parts), "vs", tot)
    return parts


def split_stage(pg, q, tot):
    if "PUBSTAGE(" in q:
        return []
    parts, sm = [], 0
    for st in ("aip", "final"):
        sq = "%s AND PUBSTAGE(%s)" % (q, st)
        recs, n, err = query(pg, sq)
        if err:
            raise HttpFail("http")
        n = int(n or 0)
        log("[stage]", st, n)
        if n:
            parts.append(("S:" + st, sq, n))
            sm += n
    rest = (tot or 0) - sm
    if rest > 0:
        sq = "%s AND NOT PUBSTAGE(aip) AND NOT PUBSTAGE(final)" % q
        recs, n, err = query(pg, sq)
        if err:
            raise HttpFail("http")
        n = int(n or rest)
        log("[stage] rest", n)
        if n:
            parts.append(("S:rest", sq, n))
    log("[stage-sum]", sum(p[2] for p in parts), "vs", tot)
    return parts


def split_doi(pg, q, tot):
    if re.search(r"AND DOI\(10\.", q):
        return []
    recs0, _, err = query(pg, q)
    if err:
        raise HttpFail("http")
    prefs = []
    for p in SEED_DOI:
        if p not in prefs:
            prefs.append(p)
    # 先从首页里发现真实前缀，再兜底种子表，能省不少探针。
    for r in recs0 or []:
        p = doi_pref(r.get("doi"))
        if p and p not in prefs:
            prefs.insert(0, p)
    parts, used, sm = [], [], 0
    for pr in prefs:
        sq = q + " AND DOI(%s)" % pr
        recs, n, err = query(pg, sq)
        if err:
            raise HttpFail("http")
        n = int(n or 0)
        if not n:
            continue
        log("[doi]", pr, n)
        parts.append(("DOI:" + pr, sq, n))
        used.append(pr)
        sm += n
        if tot and sm >= tot - 5:
            break
    extra = "".join(" AND NOT DOI(%s)" % pr for pr in used)
    sq = q + extra
    recs, n, err = query(pg, sq)
    if err:
        raise HttpFail("http")
    n = int(n or 0)
    log("[doi-rest]", n, "used", len(used), "sum", sm, "vs", tot)
    if n:
        parts.append(("DOI:rest", sq, n))
    return parts


def _tail_len(p):
    """DOI 值里最后一个 / 或 . 之后的字符数。网关按 . 和 / 切词。"""
    return len(re.split(r"[/.]", p)[-1])


def _min_ext(rest):
    """取 rest 的最短前缀，使 pref+该前缀 的末段有 >=2 个字符。

    网关规则（09-19 实测）：DOI 值的最后一个 / 或 . 之后必须至少 2 个实字符，
    否则 400 —— `DOI(10.1109/ti*)` 可以，`DOI(10.1109/t*)` 报
    "single-character wildcard"，`DOI(10.1109/tii.*)` 报 "only contains wildcards"。
    """
    for i in range(1, len(rest) + 1):
        if _tail_len(rest[:i]) >= 2:
            return rest[:i]
    return rest


def _next_keys(recs, pref):
    """结果页里 pref 之后、满足末段 >=2 字符的最短扩展串。"""
    out, seen = [], set()
    for r in recs or []:
        d = (r.get("doi") or "").strip().lower()
        if not d.startswith(pref):
            continue
        rest = d[len(pref):]
        if not rest:
            continue
        ext = _min_ext(rest)
        if ext and ext not in seen:
            seen.add(ext)
            out.append(ext)
    return out


def split_doi_prefix(pg, q, tot):
    """在已有的 DOI(前缀*) 上再往下切一段，把 DOI 前缀做成字典树。

    比 ISSN 桶（187 次探针）便宜一个数量级，所以排在 ISSN 前面。
    例：10.1109* → 10.1109/ac* / 10.1109/ti* / 10.1109/5.30* …
    """
    m = None
    for m2 in re.finditer(r"AND DOI\(([^)]*?)\*\)", q):
        m = m2
    if m is None:
        return []
    pref = m.group(1).strip().lower()
    if not pref or _tail_len(pref) < 1:
        return []
    recs0, _, err = query(pg, q)
    if err:
        raise HttpFail("http")
    keys = _next_keys(recs0, pref)
    # 整格同一本刊时只有一个分支，等于没切开：继续往后吃字符，但每步都用一次
    # 计数查询确认扩展后仍覆盖整格——首页只有 1000 条，不能凭它断定没出现在
    # 首页的记录也同前缀，否则会漏采。
    ext = 0
    while len(keys) == 1 and ext < 6:
        cand = pref + keys[0]
        sq = q[:m.start()] + "AND DOI(%s*)" % cand + q[m.end():]
        recs1, n1, err = query(pg, sq)
        if err:
            raise HttpFail("http")
        if not n1 or int(n1) != int(tot or 0):
            break
        pref, recs0, ext = cand, recs1, ext + 1
        log("[doi-pfx-ext]", pref, n1)
        keys = _next_keys(recs0, pref)
    if len(keys) < 2:
        return []
    if len(keys) > DOI_PFX_MAX_KEYS:
        log("[doi-pfx-cap]", pref, "keys", len(keys), ">", DOI_PFX_MAX_KEYS, "放弃")
        return []
    parts, used, sm = [], [], 0
    for k in keys:
        pr = pref + k + "*"
        if _tail_len(pr[:-1]) < 2:      # 兜底：宁可不切，也不要发非法查询
            log("[doi-pfx-invalid]", pr)
            continue
        sq = q[:m.start()] + "AND DOI(%s)" % pr + q[m.end():]
        recs, n, err = query(pg, sq)
        if err:
            raise HttpFail("http")
        n = int(n or 0)
        log("[doi-pfx]", pr, n)
        if not n:
            continue
        parts.append(("DOIp:" + pr[-10:], sq, n))
        used.append(pr)
        sm += n
    if not parts:
        return []
    extra = "".join(" AND NOT DOI(%s)" % pr for pr in used)
    sq = q + extra
    recs, n, err = query(pg, sq)
    if err:
        raise HttpFail("http")
    n = int(n or 0)
    log("[doi-pfx-rest]", n, "sum", sm, "vs", tot, "nkey", len(keys))
    if n:
        parts.append(("DOIp:rest", sq, n))
    return parts


def split_issn_half(pg, q, tot):
    m = re.search(r"AND ISSN\((.+)\)\s*$", q)
    if not m:
        return []
    codes = [x.strip() for x in m.group(1).split(" OR ") if x.strip()]
    if len(codes) < 2:
        return []
    mid = max(1, len(codes) // 2)
    parts = []
    for i, grp in enumerate((codes[:mid], codes[mid:])):
        sq = re.sub(r"AND ISSN\(.+\)\s*$", "AND ISSN(%s)" % " OR ".join(grp), q)
        recs, n, err = query(pg, sq)
        if err:
            raise HttpFail("http")
        n = int(n or 0)
        log("[issn-half]", i, "nissn", len(grp), n)
        if n:
            parts.append(("Ih:%d" % i, sq, n))
    log("[issn-half-sum]", sum(p[2] for p in parts), "vs", tot)
    return parts


def split_journals_page(pg, q, tot):
    recs0, _, err = query(pg, q)
    if err:
        raise HttpFail("http")
    names, seen = [], set()
    for r in recs0 or []:
        name = (r.get("source") or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    if len(names) < 2:
        return []
    parts, used, sm = [], [], 0
    for name in names[:30]:
        sq = sc.source_query(q, name)
        recs, n, err = query(pg, sq)
        if err:
            raise HttpFail("http")
        n = int(n or 0)
        log("[jpage]", name[:40], n)
        if not n:
            continue
        parts.append(("j", sq, n))
        used.append(name)
        sm += n
        if tot and sm >= tot - 5:
            break
    # strip=False：必须保留父格已有的 NOT EXACTSRCTITLE，否则余项不是补集。
    # 实测 09-20 的 jrem 格：strip=True 时余项算出 2666 > 父格 2630，
    # parts_ok 正确拒掉，该格再也切不动 → 丢 1630 条。
    qrem, _ = sc.remainder_query(q, used, strip=False)
    recs, n, err = query(pg, qrem)
    if err:
        raise HttpFail("http")
    n = int(n or 0)
    log("[jpage-rest]", n, "sum-j", sm, "vs", tot)
    if n:
        parts.append(("jrem", qrem, n))
    return parts


def split_issn(pg, q, tot):
    if issn_buckets is None or "ISSN(" in q:
        return []
    try:
        groups = issn_buckets.load()
    except Exception as e:
        log("[issn-skip]", str(e)[:80])
        return []
    parts, sm = [], 0
    for gi, grp in enumerate(groups):
        sq = q + " AND ISSN(%s)" % " OR ".join(grp)
        if len(sq) > 12000:
            log("[issn-toolong]", gi, len(sq))
            continue
        recs, n, err = query(pg, sq)
        if err:
            raise HttpFail("http")
        n = int(n or 0)
        if gi % 20 == 0 or n:
            log("[issn]", gi, n)
        if not n:
            continue
        parts.append(("I:%d" % gi, sq, n))
        sm += n
    log("[issn-sum]", sm, "vs", tot)
    return parts


# 只留便宜切分。ISSN 187 刀已禁用：刚过 1000 的格走 NOT-EID 两页，不探桶。
SPLITTERS = [
    ("doctype", split_doctype),        # ~14
    ("accesstype", split_accesstype),  # 2（09-22 新增，精确二分）
    ("srctype", split_srctype),        # 3（09-22 新增，与类型正交）
    ("pubyear", split_year),           # ~3
    ("language", split_lang),          # ~11
    ("pubstage", split_stage),         # ~3
    ("doi", split_doi),                # ~60（种子出版商前缀）
    ("doi-prefix", split_doi_prefix),  # ~10~40（DOI 前缀字典树）
    ("jpage", split_journals_page),    # ~30
]


class HttpFail(RuntimeError):
    pass


class DayBudget(RuntimeError):
    """单天预算到点：不是错误，收尾换下一天，断点已落盘。"""


_day_t0 = [0.0]


# ---------------------------------------------------------------- 断点续采
# 一天两万多条要上千次查询，单轮 125 分钟预算吃不完，会被 kill。没有断点时
# 下一轮又从这天开头重来，永远做不完。这里把「已完整处理过的格子 tag」落盘，
# 下一轮直接跳过，进度才能跨轮累积。
DONE_PATH = os.path.join(sc.OUT_DIR, "scopus_done_cells.json")
_DONE = {}
_DONE_SET = {}
_DONE_DIRTY = [0]


def done_load():
    global _DONE, _DONE_SET
    try:
        with open(DONE_PATH, encoding="utf-8") as f:
            _DONE = json.load(f) or {}
    except Exception:
        _DONE = {}
    _DONE_SET = {}
    return _DONE


def done_save(force=False):
    if not force and _DONE_DIRTY[0] < 5:
        return
    try:
        tmp = DONE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_DONE, f, ensure_ascii=False)
        os.replace(tmp, DONE_PATH)
        _DONE_DIRTY[0] = 0
    except Exception as e:
        log("[done-save-err]", str(e)[:80])


def done_tags(day):
    """按天缓存成 set：一天上千个格子，每次重建 set 会变成 O(n^2)。"""
    s = _DONE_SET.get(day)
    if s is None:
        v = _DONE.get(day) or {}
        s = set(v.get("tags") or [])
        _DONE_SET[day] = s
    return s


def done_mark(day, tag):
    v = _DONE.setdefault(day, {"total": 0, "tags": [], "pass": 0})
    v["tags"].append(tag)
    done_tags(day).add(tag)
    _DONE_DIRTY[0] += 1
    done_save()


def done_reset(day, total):
    """开始一轮新的全量遍历（上一轮跑完了但当天仍不齐，说明窗口变了）。"""
    v = _DONE.get(day) or {}
    _DONE[day] = {"total": int(total or 0), "tags": [], "pass": int(v.get("pass") or 0) + 1}
    _DONE_SET[day] = set()
    done_save(force=True)


def done_pass_done(day):
    v = _DONE.get(day) or {}
    v["pass_done"] = True
    _DONE[day] = v
    done_save(force=True)


# ---------------------------------------------------------------- 切分结果缓存
# 断点只记「已完成的叶子」，不记「已经探过的切分结果」。于是每轮/每遍都要从根
# 重新走一遍 doctype→年→语种→阶段→DOI→刊名→ISSN，约 250 次探针 ≈ 15 分钟，
# 30 分钟的单天窗口一半以上花在这里，实测连续四轮停在完全相同的格子
# （`…/S:final/DOI(10.1016*)/jrem/I:0`）且 new=0。
# 这里把每格的切分结果连查询串一起落盘，下一轮直接跳到未完成的分支。
# 安全性：取用前必须用**当前** totalCount 重新过一遍 parts_ok；窗口漂移导致
# 对不上就丢弃重探——绝不让过期缓存悄悄少采。
PARTS_PATH = os.path.join(sc.OUT_DIR, "scopus_day_parts.json")
_PARTS = {}
_PARTS_DIRTY = [0]


def parts_load():
    global _PARTS
    try:
        with open(PARTS_PATH, encoding="utf-8") as f:
            _PARTS = json.load(f) or {}
    except Exception:
        _PARTS = {}
    return _PARTS


def parts_save(force=False):
    if not force and _PARTS_DIRTY[0] < 3:
        return
    try:
        tmp = PARTS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_PARTS, f, ensure_ascii=False)
        os.replace(tmp, PARTS_PATH)
        _PARTS_DIRTY[0] = 0
    except Exception as e:
        log("[parts-save-err]", str(e)[:80])


def parts_get(day, tag, tot):
    """返回 (name, parts) 或 None。parts_ok 用当前 tot 复核，漂移即丢弃。"""
    v = (_PARTS.get(day) or {}).get(tag)
    if not v:
        return None
    parts = [tuple(p) for p in (v.get("parts") or [])]
    if not parts:
        return None
    if not parts_ok(parts, tot):
        log("[parts-stale]", day, tag, "sum", sum(int(p[2] or 0) for p in parts),
            "vs tot", tot, "丢弃重探")
        (_PARTS.get(day) or {}).pop(tag, None)
        _PARTS_DIRTY[0] += 1
        parts_save()
        return None
    # 求和 < 父格的缓存一律丢弃重探：那是旧代码 rest>3 的遗留，余项格没建出来，
    # 少掉的那几条永远不在任何子格里。parts_ok 允许最多差 5，所以这种缓存能一直
    # 通过校验、每轮原样复用，缺口就这么冻结住了。09-22 实测 year 切分曾差 3。
    sm = sum(int(p[2] or 0) for p in parts)
    if int(tot or 0) > 0 and sm < int(tot):
        log("[parts-lossy]", day, tag, "sum", sm, "vs tot", tot,
            "缺", int(tot) - sm, "丢弃重探")
        (_PARTS.get(day) or {}).pop(tag, None)
        _PARTS_DIRTY[0] += 1
        parts_save()
        return None
    return (v.get("name") or "?"), parts


def parts_put(day, tag, name, parts):
    _PARTS.setdefault(day, {})[tag] = {
        "name": name,
        "parts": [[str(p[0]), str(p[1]), int(p[2] or 0)] for p in parts],
    }
    _PARTS_DIRTY[0] += 1
    parts_save()


def parts_drop_day(day):
    if _PARTS.pop(day, None) is not None:
        parts_save(force=True)


def drop_issn_done(day):
    """ISSN 已禁用：丢掉 ISSN 叶子、ISSN 父格、以及它们的祖先断点。

    否则 harvest 一碰到已标记的 DOI:10.1016* 就会整支跳过，1078 那种格
    永远走不到 NOT-EID 两页。兄弟叶子的 done 标记保留，缓存命中后直接跳过。
    """
    day_parts = _PARTS.get(day) or {}
    issn_parents = []
    for tag, info in list(day_parts.items()):
        if (info.get("name") or "") in ("issn", "issn-half"):
            issn_parents.append(tag)
            day_parts.pop(tag, None)
    v = _DONE.get(day)
    if not v and not issn_parents:
        return 0
    tags = list((v or {}).get("tags") or [])
    drop = set()
    for t in tags:
        if "/I:" in t or "/Ih:" in t:
            drop.add(t)
    for p in issn_parents:
        drop.add(p)
        for t in tags:
            if p.startswith(str(t) + "/"):
                drop.add(t)
    if not drop:
        return 0
    keep = [t for t in tags if t not in drop]
    if v is None:
        v = {"total": 0, "tags": [], "pass": 0}
    v["tags"] = keep
    v["pass_done"] = False
    _DONE[day] = v
    _DONE_SET.pop(day, None)
    _DONE_DIRTY[0] += 1
    if issn_parents:
        _PARTS_DIRTY[0] += 1
        parts_save()
    log("[drop-issn-done]", day, "dropped", len(drop), "keep", len(keep))
    done_save(force=True)
    return len(drop)


# ---------------------------------------------------------------- 切不动的格
# 「所有切分器都失败」的格以前只取一页就当完成（09-20 实测丢 1630 条），而且
# 不写 [LEAK]，巡检时完全看不见。现在：记录、可见、下一轮自动放开重切。
LEAK_PATH = os.path.join(sc.OUT_DIR, "scopus_day_leaks.json")
_LEAKS_P = {}


def leak_load():
    global _LEAKS_P
    try:
        with open(LEAK_PATH, encoding="utf-8") as f:
            _LEAKS_P = json.load(f) or {}
    except Exception:
        _LEAKS_P = {}
    return _LEAKS_P


def leak_save(force=False):
    try:
        tmp = LEAK_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_LEAKS_P, f, ensure_ascii=False)
        os.replace(tmp, LEAK_PATH)
    except Exception as e:
        log("[leak-save-err]", str(e)[:80])


def leak_put(day, tag, tot, got, have_day=None):
    rec = {"cell_tot": int(tot or 0), "new": int(got or 0),
           "ts": time.time(), "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if have_day is not None:
        rec["have_day"] = int(have_day)
    _LEAKS_P.setdefault(day, {})[tag] = rec
    leak_save(force=True)


def drop_leak_done(day):
    """把「切不动」的格连同它的祖先断点/切分缓存一起放开，让它用新代码重切。

    只放开这一个格及其祖先链，兄弟格子的 done 标记保留（命中即跳过，不再发查询）。
    """
    v = _LEAKS_P.get(day) or {}
    due = []
    for tag, info in list(v.items()):
        age_h = (time.time() - float(info.get("ts") or 0)) / 3600.0
        if age_h >= LEAK_RETRY_HOURS:
            due.append(tag)
        else:
            log("[leak-wait]", day, tag, "%.1fh 后重试" % (LEAK_RETRY_HOURS - age_h))
    if not due:
        return 0
    d = _DONE.get(day) or {}
    tags = list(d.get("tags") or [])
    drop = set()
    parts_dirty = False
    day_parts = _PARTS.get(day) or {}
    for tag in due:
        drop.add(tag)
        # 祖先链：祖先的切分缓存正是产出这个格的元凶，必须一并丢掉才会重切
        parts = str(tag).split("/")
        for i in range(1, len(parts)):
            anc = "/".join(parts[:i])
            drop.add(anc)
            if day_parts.pop(anc, None) is not None:
                parts_dirty = True
        if day_parts.pop(tag, None) is not None:
            parts_dirty = True
        v.pop(tag, None)
    keep = [t for t in tags if t not in drop]
    d["tags"] = keep
    d["pass_done"] = False
    _DONE[day] = d
    _DONE_SET.pop(day, None)
    _DONE_DIRTY[0] += 1
    if parts_dirty:
        _PARTS_DIRTY[0] += 1
        parts_save(force=True)
    leak_save(force=True)
    log("[drop-leak-done]", day, "dropped", len(drop), "keep", len(keep))
    done_save(force=True)
    return len(drop)


def pack_not_eid(base_q, eids, max_q=MAX_Q):
    """base AND NOT EID(a OR b ...)，长度不超过网关约 12000。"""
    used = []
    head = (base_q or "") + " AND NOT EID("
    tail = ")"
    body = ""
    for e in eids:
        e = (e or "").strip()
        if not e:
            continue
        piece = e if not body else " OR " + e
        if len(head) + len(body) + len(piece) + len(tail) > max_q:
            break
        body += piece
        used.append(e)
    if not used:
        return base_q, 0
    return head + body + tail, len(used)


def take_page(pg, q, have, day, tag):
    """取当前查询第一页并写入。返回 (new, recs, tot, eids)。"""
    recs, tot, err = query(pg, q)
    if err:
        raise HttpFail(tag)
    tot = int(tot or 0)
    new = append(recs, have, day)
    eids = [(r.get("eid") or "").strip() for r in (recs or [])]
    eids = [e for e in eids if e]
    return new, recs or [], tot, eids


def peel_two(pg, q, have, day, tag, max_rounds=8):
    """第一页 + 反复 NOT EID(已取) 再取一页，直到这一页装不满。

    网关一刀最多 1000 条，NOT 串受 12000 字符限制约能塞 860 个 eid。
    终止条件是「本页条数 < LEAF」而不是「new == 0」：本页全是已知记录**不等于**
    没有更多记录，只是说明 NOT 串还没滑过它们。用 new==0 收手会漏掉剩下的
    （按刊引擎实测：1092 条只取到 1000，剩 92 条被吞）。
    """
    added = 0
    excluded = []
    cur = q
    for r in range(max_rounds):
        new, recs, tot2, eids = take_page(pg, cur, have, day, tag)
        added += new
        log("[peel]", tag, "round", r + 1, "tot", tot2, "page", len(recs),
            "new", new, "excl", len(excluded), "have", len(have.get(day) or ()))
        if not eids or len(recs or []) < LEAF:
            break
        for e in eids:
            if e not in excluded:
                excluded.append(e)
        if int(tot2 or 0) <= 0:
            break
        cur, nused = pack_not_eid(q, excluded)
        if not nused:
            break
        if nused < len(excluded):
            # NOT 串装不下全部已取的 eid：只能滑一部分窗口，仍会前进，但拿不满。
            log("[peel-cap]", tag, "not", nused, "of", len(excluded),
                "NOT 串装不下，剩余缺口下轮再补")
    return added


def remainder_sweep(pg, q, have, day, tot, tag="REST"):
    """天级余项扫尾：把本地已有的 eid 切成窗口逐个 NOT 掉，每窗取一页。

    切分树能覆盖的都是「字段有值」的记录。字段为空的记录（没有 DOCTYPE/语种/
    阶段/刊名）会同时落进每个切分器的缝隙里：`NOT LANGUAGE(...)` 之类匹配不到
    它们，于是所有子格之和永远比父格少几条，切分树无论重走多少遍都补不上——
    实测 09-02 差 8、09-16 差 448、09-17 差 73 都是这类，重走整树 new=0。

    这一招不依赖任何字段：只按 eid 排除已经拿到的东西，剩下的就是缺口。
    受 12000 字符限制，一次 NOT 只能装约 750 个 eid，所以按窗口滑动；
    每轮把整份 eid 列表扫一遍，扫完没新增就收手。
    """
    added = 0
    if not tot or int(tot or 0) <= 0:
        return 0
    for rnd in range(REMAINDER_ROUNDS):
        eids = sorted(have.get(day) or ())
        if not eids:
            return added
        progressed = 0
        i = 0
        while i < len(eids):
            if _day_t0[0] and (time.time() - _day_t0[0]) / 60.0 > DAY_BUDGET_MIN:
                raise DayBudget(tag)
            if budget_left() <= 2:
                raise DayBudget(tag)
            nq, nused = pack_not_eid(q, eids[i:i + REMAINDER_WIN])
            if not nused:
                break
            win = i // REMAINDER_WIN
            i += nused
            new, recs, tot2, page_eids = take_page(pg, nq, have, day, tag)
            if new:
                progressed += new
                added += new
                log("[rest]", tag, "round", rnd + 1, "win", win,
                    "not", nused, "page", len(recs), "new", new,
                    "have", len(have.get(day) or ()), "tot", tot)
                if len(have.get(day) or ()) >= int(tot):
                    log("[rest-full]", day, len(have.get(day) or ()), "/", tot)
                    return added
        log("[rest-pass]", day, "round", rnd + 1, "new", progressed,
            "have", len(have.get(day) or ()), "tot", tot)
        if not progressed:
            break
    return added


def harvest(pg, q, tot, have, day, tag, depth=0):
    """返回新增条数。HttpFail 直接上抛，由日循环统一收尾。

    三条规则：
        tot ≤ 1000        取第一页
        1001 ≤ tot ≤ 1500 第一页 + NOT EID(本页) + 再取一页
        tot > 1500        便宜切分；ISSN 禁用
    """
    if tag in done_tags(day):
        return 0
    if _day_t0[0] and (time.time() - _day_t0[0]) / 60.0 > DAY_BUDGET_MIN:
        raise DayBudget(tag)
    tot = int(tot or 0)
    if tot <= 0:
        recs, tot, err = query(pg, q)
        if err:
            raise HttpFail(tag)
        tot = int(tot or 0)
        if tot <= 0:
            log("[empty]", tag)
            done_mark(day, tag)
            return 0
    if tot <= LEAF:
        new, recs, tot2, _eids = take_page(pg, q, have, day, tag)
        if tot2:
            tot = tot2
        log("[leaf]", tag, "tot", tot, "page", len(recs), "new", new,
            "have", len(have.get(day) or ()))
        # 叶子页数少于它自己报的总数：网关的 totalCount 与实际可返回条数不一致。
        # 这就是「怎么重走都差几条」的现场，必须显式记下来，否则永远查不出丢在哪。
        if tot and len(recs or []) < int(tot):
            log("[leaf-short]", tag, "tot", tot, "page", len(recs or []),
                "差", int(tot) - len(recs or []))
            LEAKS.append((day, tag, int(tot) - len(recs or [])))
        done_mark(day, tag)
        return new
    if tot <= PEEL_MAX:
        new = peel_two(pg, q, have, day, tag)
        done_mark(day, tag)
        return new
    if depth >= MAX_DEPTH:
        LEAKS.append((day, tag, tot))
        log("[LEAK]", tag, tot, "深度用尽")
        new = peel_two(pg, q, have, day, tag)
        leak_put(day, tag, tot, new, have_day=len(have.get(day) or ()))
        done_mark(day, tag)
        return new
    log("[split]", tag, "tot", tot, "depth", depth)
    hit = parts_get(day, tag, tot)
    if hit and (hit[0] or "") in ("issn", "issn-half"):
        log("[split-cache-skip]", hit[0], tag, "ISSN 已禁用")
        hit = None
    if hit:
        cname, parts = hit
        log("[split-cache]", cname, tag, "n", len(parts), "parent", tot)
        added = 0
        for ptag, sq, n in parts:
            added += harvest(pg, sq, n, have, day, tag + "/" + ptag, depth + 1)
        done_mark(day, tag)
        return added
    for name, fn in SPLITTERS:
        try:
            parts = fn(pg, q, tot) or []
        except HttpFail:
            raise
        except RuntimeError:
            raise
        except Exception as e:
            log("[split-err]", name, str(e)[:100])
            parts = []
        if not parts_ok(parts, tot):
            log("[split-skip]", name, "n", len(parts),
                "max", max((p[2] for p in parts), default=0),
                "sum", sum(p[2] for p in parts) if parts else 0)
            continue
        log("[split-ok]", name, "n", len(parts), "max", max(p[2] for p in parts),
            "sum", sum(p[2] for p in parts), "parent", tot)
        parts_put(day, tag, name, parts)
        added = 0
        for ptag, sq, n in parts:
            added += harvest(pg, sq, n, have, day, tag + "/" + ptag, depth + 1)
        done_mark(day, tag)
        return added
    # 所有切分器都失败：以前只取一页就当完成（09-20 实测 2630 条只取 1000、
    # 静默丢 1630，且不写 [LEAK]）。现在先做「第一页 + NOT EID(本页) + 再取一页」
    # 尽量多拿，再如实记录缺口，并在 LEAK_RETRY_HOURS 后自动放开重切。
    LEAKS.append((day, tag, tot))
    log("[LEAK]", tag, tot, "所有切分器失败")
    new = peel_two(pg, q, have, day, tag)
    # cell_tot 是「这一格的总数」，new 是「本次新增」。两者相减**不是缺口**：
    # 这一格的记录很可能早先从别的格子收进来了（实测 09-17 S:aip cell_tot 2253
    # 而当天只差 73）。真实缺口只看当天 uniq vs totalCount。
    leak_put(day, tag, tot, new, have_day=len(have.get(day) or ()))
    log("[leak-cell]", tag, "cell_tot", tot, "new", new,
        "have_day", len(have.get(day) or ()), "缺口以当天对账为准")
    done_mark(day, tag)
    return new


# ---------------------------------------------------------------- 日/月调度

def explicit_days():
    """--days d1,d2,... 指定要补的天。给了就只补这些天，且不套用静音/断点。

    小残差（几十到几百条）本来是「连续两轮无进展就静音 6 小时」的处置对象，
    而那正好让它们永远轮不到。人工点名补的时候必须把静音和旧断点都让开，
    否则一天都碰不到。三个以上的位置参数也当成点名（两个仍是区间，兼容旧用法）。
    """
    for i, a in enumerate(sys.argv[1:]):
        if a == "--days" and i + 2 <= len(sys.argv) - 1:
            raw = sys.argv[i + 2]
            return [x.strip() for x in raw.split(",") if re.match(r"\d{4}-\d{2}-\d{2}$", x.strip())]
        if a.startswith("--days="):
            raw = a.split("=", 1)[1]
            return [x.strip() for x in raw.split(",") if re.match(r"\d{4}-\d{2}-\d{2}$", x.strip())]
    pos = [a for a in sys.argv[1:] if re.match(r"\d{4}-\d{2}-\d{2}$", a)]
    if len(pos) >= 3:
        return sorted(set(pos))
    return []


ONLY_DAYS = []


def days_to_scan():
    """当月每一天 + 上月末 5 天，永远重扫，不依赖 last_done 游标。"""
    if ONLY_DAYS:
        return list(ONLY_DAYS)
    today = datetime.date.today()
    end = today - datetime.timedelta(days=1)
    first = today.replace(day=1)
    start = max(START, first - datetime.timedelta(days=5))
    args = [a for a in sys.argv[1:] if re.match(r"\d{4}-\d{2}-\d{2}$", a)]
    if len(args) == 2:
        start, end = (datetime.date.fromisoformat(a) for a in args)
    elif len(args) == 1:
        start = end = datetime.date.fromisoformat(args[0])
    out, d = [], start
    while d <= end:
        out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def verify_save(verify, rc):
    """增量落盘。引擎常被编排器按预算 kill，只在收尾写就永远写不到，
    巡检读到的会是几天前的旧 verify（09-21 实测：文件停在 09-19）。"""
    try:
        json.dump({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "days": verify,
                   "leaks": [list(x) for x in LEAKS], "bad_queries": BADQ[-20:],
                   "rc": rc},
                  open(VERIFY, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception as e:
        log("[verify-write-err]", str(e)[:80])


def write_state(verify, leaks):
    ok_days = [d for d, v in verify.items() if v.get("ok")]
    st = {
        "last_done": max(ok_days) if ok_days else "",
        "days_ok": len(ok_days),
        "days_total": len(verify),
        "leaks": leaks[:20],
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "le1000-verify",
    }
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log("[state]", json.dumps(st, ensure_ascii=False))


def drop_hold_lock(path):
    """只删自己写的那把锁，别把别的进程的锁误删。"""
    try:
        d = json.load(open(path, encoding="utf-8"))
        if int(d.get("pid") or 0) == os.getpid():
            os.remove(path)
            log("[hold-lock]", "已释放")
    except Exception:
        pass


def main():
    global ONLY_DAYS
    ONLY_DAYS = explicit_days()
    verify_only = "--verify-only" in sys.argv
    hold_lock = "--hold-lock" in sys.argv
    lock_path = os.path.join(LOG_DIR, "booknote_3h.lock")
    if hold_lock:
        # 点名补天要长时间占着 scihuber 帐号。站点只允许一个会话，若不占锁，
        # 下一轮 3 小时任务会同时开浏览器，两边都撞「同一帐号同一时刻只能一人在线」。
        # 写一把和 sched_3h 同格式的锁，让那一轮自己 SKIP。
        try:
            json.dump({"pid": os.getpid(), "t": time.time(),
                       "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "by": "scopus_le1000 --days"},
                      open(lock_path, "w", encoding="utf-8"))
            log("[hold-lock]", "已占锁 pid", os.getpid())
        except Exception as e:
            log("[hold-lock-err]", str(e)[:80])
    if ONLY_DAYS:
        log("[only-days]", ",".join(ONLY_DAYS), "fresh", FRESH, "rewalk", REWALK,
            "静音与断点让开")
    days = days_to_scan()
    months = set(d[:7] for d in days)
    have_by_day, by_day, uniq = load_have(months)
    done_load()
    parts_load()
    leak_load()
    log("have_all", sum(len(s) for s in have_by_day.values()),
        "days", len(uniq), "months", sorted(months),
        "budget_min", BUDGET_MIN)

    cam, br, app = land()
    if app is None:
        if hold_lock:
            drop_hold_lock(lock_path)
        return 3
    rc = 0
    auth_dead = False
    verify = {}
    reserve = 0
    try:
        log("[todo]", days)
        work = []
        for day in days:
            if budget_left() <= 2:
                log("[budget-stop]", day, "剩余 %.1f 分钟" % budget_left())
                rc = 5
                break
            q = day_q(day)
            try:
                recs, tot, err = query(app, q)
            except Exception as e:
                log("[day-ex]", day, str(e)[:100])
                err = True
                tot = None
            if err:
                kind = sc.bad_kind() or ""
                log("[day-query-http]", day, kind)
                verify[day] = {"total": None, "uniq": uniq.get(day, 0),
                               "ok": False, "note": "http:" + kind}
                if kind.startswith(("401", "403")):
                    log("[run-abort-auth]", day, kind[:70])
                    rc = 4
                    auth_dead = True
                    break
                rc = rc or 4
                continue
            tot = int(tot or 0)
            have_day = uniq.get(day, 0)
            ok, note, gap, extra = day_status(have_day, tot)
            log("[day]", day, "total", tot, "uniq", have_day,
                "lines", by_day.get(day, 0), "note", note or "ok",
                "gap", gap, "extra", extra)
            ledger_put(day, tot, have_day, ok, note, extra=extra, gap=gap)
            if tot <= 0:
                verify[day] = {"total": 0, "uniq": have_day, "ok": False,
                               "note": "upstream-empty", "gap": 0, "extra": have_day}
                continue
            if ok:
                verify[day] = {"total": tot, "uniq": have_day, "ok": True,
                               "note": note, "gap": 0, "extra": extra}
                log("[day-full]", day, have_day, "/", tot, note or "")
                continue
            verify[day] = {"total": tot, "uniq": have_day, "ok": False,
                           "note": note or "short", "gap": gap, "extra": extra}
            if verify_only:
                continue
            if stuck_muted(day, gap):
                v = ledger_load().get(day) or {}
                log("[day-stuck-muted]", day, "gap", gap, "stuck", v.get("stuck"),
                    "连%d轮无进展，本轮让位给大缺口" % STUCK_MIN)
                verify[day]["note"] = "stuck-muted"
                continue
            work.append((int(gap or 0), day, tot, q))
        work.sort(key=lambda x: -x[0])
        log("[work-order]", [(d, g) for g, d, _t, _q in work])
        verify_save(verify, rc)
        # 已有列表齐的天 → 给详情留尾巴；本轮还没有任何齐的天就不预留。
        reserve = DETAIL_RESERVE_MIN if any(v.get("ok") for v in verify.values()) else 0
        if reserve:
            log("[detail-reserve]", "%.0f 分钟" % reserve)
        for gap, day, tot, q in work:
            if auth_dead or budget_left() <= 2 + reserve:
                log("[budget-stop]", day, "剩余 %.1f 分钟" % budget_left(),
                    "预留详情", reserve)
                rc = rc or 5
                break
            drop_issn_done(day)
            drop_leak_done(day)
            if FRESH and day in ONLY_DAYS:
                log("[fresh]", day, "清断点与切分缓存，整树重走")
                done_reset(day, tot)
                parts_drop_day(day)
                if _LEAKS_P.pop(day, None) is not None:
                    leak_save(force=True)
            elif REWALK and day in ONLY_DAYS:
                log("[rewalk]", day, "清断点、复用切分缓存，沿树重取叶子")
                done_reset(day, tot)
                if _LEAKS_P.pop(day, None) is not None:
                    leak_save(force=True)
            st_done = _DONE.get(day) or {}
            if not st_done.get("tags") or st_done.get("pass_done"):
                if st_done.get("pass_done"):
                    log("[done-reset]", day, "上轮整遍跑完仍不齐，重开一遍")
                done_reset(day, tot)
            log("[resume]", day, "已完成格子", len(done_tags(day)), "gap", gap)
            added = 0
            _day_t0[0] = time.time()
            try:
                added = harvest(app, q, tot, have_by_day, day, day, 0)
            except DayBudget as e:
                log("[day-budget-stop]", day, str(e)[:60], "断点已存，换下一天")
                done_save(force=True)
            except HttpFail as e:
                kind = sc.bad_kind() or ""
                log("[day-abort-http]", day, str(e)[:60], kind)
                if kind.startswith(("401", "403")):
                    log("[run-abort-auth]", day, kind[:70])
                    rc = 4
                    auth_dead = True
                else:
                    log("[day-stop-transient]", day, kind[:70], "存断点换下一天")
                    rc = rc or 4
            except Exception as e:
                log("[day-err]", day, str(e)[:120])
                rc = 1
            # 切分树走完仍差 -> 天级余项扫尾。字段为空（没有类型/语种/阶段/刊名）
            # 的记录不属于任何子格，重走多少遍整树都补不到，只能按 eid 排除来捞。
            if day in ONLY_DAYS and not auth_dead:
                try:
                    rest = remainder_sweep(app, q, have_by_day, day, tot, "REST")
                    added += rest
                    log("[rest-sum]", day, "new", rest,
                        "have", len(have_by_day.get(day) or ()), "/", tot)
                except DayBudget as e:
                    log("[rest-budget-stop]", day, str(e)[:60])
                except HttpFail as e:
                    kind = sc.bad_kind() or ""
                    log("[rest-http]", day, str(e)[:60], kind)
                    if kind.startswith(("401", "403")):
                        log("[run-abort-auth]", day, kind[:70])
                        rc = 4
                        auth_dead = True
                    else:
                        rc = rc or 4
                except Exception as e:
                    log("[rest-err]", day, str(e)[:120])
            _day_t0[0] = 0.0
            done_save(force=True)
            have_by_day, by_day, uniq = load_have(months)
            n = uniq.get(day, 0)
            ok, note, gap, extra = day_status(n, tot)
            exhausted = False
            if not ok and day in done_tags(day):
                done_pass_done(day)
                # 整遍切分树走完仍差 = 手段已穷尽。连续 STUCK_MIN 轮无进展才标
                # exhausted（gap 照实保留），巡检据此把「上游 totalCount 虚高」
                # 和「真没采到」分开报。
                exhausted = int((ledger_load().get(day) or {}).get("stuck") or 0) >= STUCK_MIN
                log("[pass-done-short]", day, n, "/", tot, "exhausted", exhausted)
            ledger_put(day, tot, n, ok, note, extra=extra, gap=gap, exhausted=exhausted)
            verify[day] = {"total": tot, "uniq": n, "ok": ok, "new": added,
                           "note": note, "gap": gap, "extra": extra}
            log("[day-done]", day, "uniq", n, "/", tot, "new", added,
                "ok", ok, "note", note or "", "gap", gap, "extra", extra)
            verify_save(verify, rc)
            if auth_dead:
                break
        # 同轮多遍：单天有 30 分钟上限、而整轮有 125 分钟，单遍扫完只用掉 60 分钟
        # （两个大缺口各 30 分钟），剩下 65 分钟白扔。这里在第一遍之后再跑几遍，
        # 每遍只啃「还没齐、且缺口真的在缩小」的天，靠断点续采接着上次的格子做。
        # 一整遍 0 新增就收手，不会空转。
        if not verify_only and not auth_dead:
            for pno in range(1, MAX_PASS):
                if budget_left() <= 2 + reserve:
                    log("[pass-stop]", "剩余 %.1f 分钟" % budget_left(),
                        "预留详情", reserve)
                    break
                short = []
                for d in days:
                    v = verify.get(d) or {}
                    g = int(v.get("gap") or 0)
                    if v.get("ok") or not v.get("total") or g <= 0:
                        continue
                    if (_DONE.get(d) or {}).get("pass_done"):
                        continue
                    if stuck_muted(d, g):
                        continue
                    short.append((g, d))
                short.sort(key=lambda x: -x[0])
                if not short:
                    log("[pass%d-done]" % pno, "没有可续采的天")
                    break
                log("[pass%d-start]" % pno, "续采", [d for _g, d in short],
                    "剩余 %.1f 分钟" % budget_left())
                gained = 0
                for _g, day in short:
                    if budget_left() <= 2 + reserve or auth_dead:
                        break
                    drop_issn_done(day)
                    drop_leak_done(day)
                    q = day_q(day)
                    _, tot, err = query(app, q)
                    if err or not tot:
                        continue
                    _day_t0[0] = time.time()
                    try:
                        added = harvest(app, q, int(tot), have_by_day, day, day, 0)
                    except DayBudget as e:
                        log("[pass%d-budget-stop]" % pno, day, str(e)[:50])
                        added = 0
                    except HttpFail as e:
                        kind = sc.bad_kind() or ""
                        log("[pass%d-http]" % pno, day, str(e)[:50], kind)
                        added = 0
                        if kind.startswith(("401", "403")):
                            log("[run-abort-auth]", day, kind[:70])
                            rc = 4
                            auth_dead = True
                    except Exception as e:
                        log("[pass%d-err]" % pno, day, str(e)[:100])
                        added = 0
                    _day_t0[0] = 0.0
                    done_save(force=True)
                    have_by_day, by_day, uniq = load_have(months)
                    n = uniq.get(day, 0)
                    ok, note, gap, extra = day_status(n, int(tot))
                    ledger_put(day, int(tot), n, ok, note, extra=extra, gap=gap)
                    verify[day] = {"total": int(tot), "uniq": n, "ok": ok,
                                   "new": added, "note": note, "gap": gap,
                                   "extra": extra}
                    log("[pass%d]" % pno, day, "uniq", n, "/", tot,
                        "new", added, "ok", ok, "gap", gap)
                    gained += added
                    verify_save(verify, rc)
                log("[pass%d-sum]" % pno, "new", gained)
                if gained <= 0:
                    log("[pass%d-none]" % pno, "整遍 0 新增，收手")
                    break
        # 重采一轮当日缺失：totalCount 会随时间增长，收尾再对一次账
        if not verify_only and not auth_dead:
            for day in days:
                v = verify.get(day) or {}
                if v.get("ok") or not v.get("total"):
                    continue
                if budget_left() <= 2 + reserve:
                    break
                q = day_q(day)
                recs, tot, err = query(app, q)
                if err or not tot:
                    continue
                n = uniq.get(day, 0)
                ok, note, gap, extra = day_status(n, int(tot))
                if ok:
                    ledger_put(day, int(tot), n, True, note, extra=extra, gap=0)
                    verify[day] = {"total": int(tot), "uniq": n, "ok": True,
                                   "note": note, "gap": 0, "extra": extra}
                    log("[recheck-full]", day, n, "/", tot, note or "")
        # 列表优先。缺口还在就不抢详情预算。本轮列表做完、还有 ≥5 分钟，
        # 才对「列表已齐」的天拉详情（同一会话，不另开浏览器）。
        # 点名补天的运行一律不拉详情：那些天还没齐，预算该全花在列表缺口上。
        if (not verify_only and not auth_dead and det is not None
                and not ONLY_DAYS and budget_left() >= 5):
            just = [d for d, v in verify.items()
                    if v.get("ok") and int(v.get("new") or 0) > 0]
            try:
                dstat = det.run(app, verify, budget_left, just_completed=just)
                for d, st in (dstat or {}).items():
                    v = verify.setdefault(d, {})
                    v["detail_new"] = st.get("new")
                    v["detail_left"] = st.get("left")
                    v["detail_stop"] = st.get("stop")
                log("[detail-sum]", {d: st.get("new") for d, st in (dstat or {}).items()})
            except Exception as e:
                log("[detail-err]", str(e)[:120])
                rc = rc or 1
    finally:
        _close(br)
        parts_save(force=True)
        leak_save(force=True)
        if hold_lock:
            drop_hold_lock(lock_path)

    try:
        verify_save(verify, rc)
    except Exception as e:
        log("[verify-write-err]", str(e)[:80])
    write_state(verify, [list(x) for x in LEAKS])
    short = {d: v for d, v in verify.items() if not v.get("ok")}
    moved = sorted(d for d, v in verify.items() if v.get("note") == "window-moved")
    log("[SUMMARY] ok", len(verify) - len(short), "/", len(verify),
        "short", sorted(short), "window-moved", moved)
    log("[LEAKS]", LEAKS[:10])
    return rc


if __name__ == "__main__":
    sys.exit(main())
