# -*- coding: utf-8 -*-
"""每 3 小时一轮的 WoS / Scopus 采集编排器。

一轮做什么
    1. 单实例锁（避免和上一轮、和手工运行抢同一个 scihuber 帐号）；
    2. JSONL 去重（按主键，历史上有脚本重复追加过）；
    3. WoS：补 last_done+1..昨天的新天 + 复采最近 3 天（DOP 天会持续增长）；
       每天 09 点那一轮额外做一次整月全量复核；
    4. Scopus：按**期刊**维度逐本收敛（窗口内 local unique eid >= 网关数即算齐），
       尾部预留若干分钟拉**详情**（复用同一个浏览器会话，不另开进程）；
    5. Scopus -> RDS 两张表：`2026_scopus_records`（列表，每轮全量 upsert）+
       `2026_scopus_details`（详情，按字节偏移增量）；
    6. WoS 采完后把当月 JSONL upsert 进 RDS `2026_wos_records`（MYSQL_DSN）；
    7. 写 F:\\pubmed\\logs\\booknote_3h_status.json 与 .log。

判定口径（这是「不漏」的唯一依据）
    WoS    : 导出行数 rows >= 检索命中数 total，且复采时 new==0
    Scopus : 这本刊在窗口内的 local unique eid >= 网关给的 totalCount。
             曾用「按天」对账，2026-09-28 已整段移除：Scopus 会改
             ORIG-LOAD-DATE，同一篇记录在天之间搬家，按天永远追一个移动的窗口
             （实测缺口 133→175 一路涨）。期刊轴稳定，拉完即收敛。
             scopus_le1000.py 模块本身保留（它提供 query/切分器，按刊线在 import）。

用法
    python harvest/sched_3h.py                # 正常一轮
    python harvest/sched_3h.py --scopus-only
    python harvest/sched_3h.py --wos-only
    python harvest/sched_3h.py --audit        # 强制整月复核（不补采，只对账）
"""
import datetime
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_dotenv():
    path = os.path.join(ROOT, ".env")
    if not os.path.isfile(path):
        return
    for line in open(path, encoding="utf-8"):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()
os.environ.setdefault("BOOKNOTE_DATA", r"F:\pubmed")
os.environ.setdefault("BOOKNOTE_LOG", r"F:\pubmed\logs")
os.environ.setdefault("SCOPUS_OUT", r"F:\pubmed\scopus")
os.environ.setdefault("WOS_OUT", r"F:\pubmed\wos")
os.environ.setdefault("BOOKNOTE_ISSN_BUCKETS", r"F:\pubmed\scopus_issn_buckets.json")

DATA = os.environ["BOOKNOTE_DATA"]
LOG_DIR = os.environ["BOOKNOTE_LOG"]
os.makedirs(LOG_DIR, exist_ok=True)
WOS_DIR = os.environ["WOS_OUT"]
SCP_DIR = os.environ["SCOPUS_OUT"]

PY = sys.executable or r"E:\py311\python.exe"
LOCK = os.path.join(LOG_DIR, "booknote_3h.lock")
STATUS = os.path.join(LOG_DIR, "booknote_3h_status.json")
AUDIT_STAMP = os.path.join(LOG_DIR, "booknote_3h_last_audit.json")
WOS_LEDGER = os.path.join(WOS_DIR, "wos_day_ledger.json")
WOS_STATE = os.path.join(WOS_DIR, "scihuber_state.json")
SCP_JOURNAL_STATE = os.path.join(SCP_DIR, "scopus_journal_state.json")
SCP_DETAIL_STATE = os.path.join(SCP_DIR, "scopus_detail_state.json")
SCP_MYSQL_STAMP = os.path.join(SCP_DIR, ".scopus_mysql_last.json")
# 按刊收敛的每轮预算。日期轴会因上游改 ORIG-LOAD-DATE 反复开洞，期刊轴稳定。
# 55 = 约 35 分钟列表 + 20 分钟详情（详情在 scopus_journal.py 里复用同一个浏览器，
# 不另开进程 —— 两次 land() 相隔 1~2 分钟就会被站点顶成「同一帐号只能一人在线」）。
SCP_JOURNAL_BUDGET_MIN = float(os.environ.get("BOOKNOTE_SCOPUS_JOURNAL_BUDGET") or 55)
# Scopus -> RDS 两步同步的预算。列表表全量 upsert（实测 31.8 万行约 40 秒），
# 详情表按字节偏移增量，所以这两步开销基本恒定。
SCP_MYSQL_BUDGET_MIN = float(os.environ.get("BOOKNOTE_SCOPUS_MYSQL_BUDGET") or 10)
# Scopus -> RDS **一天只同步一次**（用户要求，不要每 3 小时一次）。
# 含义：从这天 SCP_MYSQL_HOUR 点那一轮开始，第一个跑到这一步的轮次做同步；
# 做完写日期戳，当天其余轮次跳过。用「>=」而不是「==」是为了兜底——
# 指定那轮要是失败或被跳过，后面的轮会补做，不会整天不同步。
SCP_MYSQL_HOUR = int(os.environ.get("BOOKNOTE_SCOPUS_MYSQL_HOUR") or 6)

WOS_BUDGET_MIN = float(os.environ.get("BOOKNOTE_WOS_BUDGET_MIN") or 45)
WOS_REVERIFY_DAYS = int(os.environ.get("BOOKNOTE_WOS_REVERIFY_DAYS") or 3)
WOS_FAIL_SKIP = int(os.environ.get("BOOKNOTE_WOS_FAIL_SKIP") or 3)
AUDIT_HOURS = float(os.environ.get("BOOKNOTE_AUDIT_HOURS") or 24)
# 必须大于 WOS_BUDGET + SCP_BUDGET + SCP_JOURNAL_BUDGET + 余量，否则正常长跑会被当成陈旧锁而被接管。
# 加了 35 分钟的按刊步骤后单轮最长 45+160+35=240 分钟，所以抬到 330。
LOCK_MAX_MIN = 330.0
MONTH_START = datetime.date(2026, 9, 1)


def log(*a):
    line = "%s [3h] %s" % (time.strftime("%H:%M:%S"), " ".join(str(x) for x in a))
    print(line, flush=True)
    try:
        with open(os.path.join(LOG_DIR, "booknote_sched3h.log"), "a",
                  encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass


def jload(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def jdump(p, obj):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


# ------------------------------------------------------------------ 单实例锁

def pid_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                             capture_output=True, text=True, timeout=20).stdout
        return str(pid) in out
    except Exception:
        return False


def take_lock():
    cur = jload(LOCK, None)
    if cur:
        pid = int(cur.get("pid") or 0)
        age = (time.time() - float(cur.get("t") or 0)) / 60.0
        if pid and pid != os.getpid() and pid_alive(pid) and age < LOCK_MAX_MIN:
            log("SKIP 上一轮还在跑 pid", pid, "已 %.0f 分钟" % age)
            return False
        log("接管陈旧锁 pid", pid, "age %.0f" % age)
    jdump(LOCK, {"pid": os.getpid(), "t": time.time(),
                 "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    return True


def drop_lock():
    cur = jload(LOCK, None)
    if cur and int(cur.get("pid") or 0) == os.getpid():
        try:
            os.remove(LOCK)
        except Exception:
            pass


# ------------------------------------------------------------------ JSONL 去重

def dedupe_jsonl(path, key, enrich=True):
    """按主键去重；同一主键保留字段最全的一行。返回 (原始, 保留, 删除)。"""
    if not os.path.exists(path):
        return 0, 0, 0
    best = {}
    order = []
    raw = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw += 1
            try:
                d = json.loads(line)
            except Exception:
                continue
            k = str(d.get(key) or "").strip()
            if not k:
                k = "__nokey__%08d" % raw
            old = best.get(k)
            if old is None:
                best[k] = d
                order.append(k)
            elif enrich and len(d) > len(old):
                best[k] = d
    if raw == len(order):
        return raw, len(order), 0
    tmp = path + ".dedup"
    with open(tmp, "w", encoding="utf-8") as f:
        for k in order:
            f.write(json.dumps(best[k], ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return raw, len(order), raw - len(order)


# ------------------------------------------------------------------ 子进程

def run_step(name, args, budget_min, logfile):
    t0 = time.time()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    with open(logfile, "a", encoding="utf-8", errors="replace") as lf:
        lf.write("\n===== %s %s START %s =====\n"
                 % (name, " ".join(args), time.strftime("%Y-%m-%d %H:%M:%S")))
        lf.flush()
        p = subprocess.Popen([PY, "-u"] + args, cwd=ROOT, env=env,
                             stdout=lf, stderr=subprocess.STDOUT)
        try:
            rc = p.wait(timeout=budget_min * 60)
        except subprocess.TimeoutExpired:
            log(name, "超时 %.0f 分钟，kill" % budget_min)
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True, timeout=60)
            try:
                p.wait(timeout=60)
            except Exception:
                pass
            # 预算到点被 kill 是设计内行为，不是错误：固定 124，别让 taskkill
            # 回传的 1 冒充真失败（否则计划任务的「上次结果」永远像出错）。
            rc = 124
    dt = (time.time() - t0) / 60.0
    log(name, "rc", rc, "耗时 %.1f 分钟" % dt)
    return rc, dt


def scopus_mysql_due(run_id):
    """Scopus->RDS 今天该不该同步。

    run_id 形如 20260928-060500，取其中的小时。判据两条：
      1. 今天还没同步过（日期戳）
      2. 当前轮的小时 >= SCP_MYSQL_HOUR（默认 6，即 06:05 那轮开始可以同步）
    第 2 条用 >= 是为了兜底：06:05 那轮要是失败或被锁跳过，09:05 那轮会补做。
    """
    today = time.strftime("%Y-%m-%d")
    try:
        if (jload(SCP_MYSQL_STAMP, {}) or {}).get("day") == today:
            return False
    except Exception:
        pass
    try:
        h = int(str(run_id).split("-")[1][:2])
    except Exception:
        h = 99
    return h >= SCP_MYSQL_HOUR


def mark_scopus_mysql_done():
    try:
        jdump(SCP_MYSQL_STAMP, {"day": time.strftime("%Y-%m-%d"),
                                "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    except Exception as e:
        log("scopus-mysql 写日期戳失败", str(e)[:80])


# ------------------------------------------------------------------ WoS 计划

def wos_days(force_month=False):
    today = datetime.date.today()
    yest = today - datetime.timedelta(days=1)
    led = jload(WOS_LEDGER, {})
    st = jload(WOS_STATE, {})
    last = (st.get("last_done") or "").strip()
    if last:
        try:
            start = datetime.date.fromisoformat(last) + datetime.timedelta(days=1)
        except Exception:
            start = yest
    else:
        start = yest
    if start > yest:
        start = yest
    # 顺序很重要：预算有限时先保新数据，再复采近几天，最后才啃历史缺口。
    # 若按日期升序排，09-01 这种几万条的大缺口会排在最前面把 45 分钟吃光，
    # 当天的新增量反而一条都采不到。
    new_days = []
    d = start
    while d <= yest:
        new_days.append(d.isoformat())
        d += datetime.timedelta(days=1)
    # 复采：DOP 天会随时间继续增长，最近几天要反复对账
    reverify = []
    for i in range(WOS_REVERIFY_DAYS):
        dd = (yest - datetime.timedelta(days=i)).isoformat()
        if dd not in new_days:
            reverify.append(dd)
    # 早于 last_done 的历史缺口，每轮也带（受 fails 上限约束）
    gaps = []
    d = max(MONTH_START, today.replace(day=1))
    while d <= yest:
        s = d.isoformat()
        v = led.get(s) or {}
        if not v.get("complete") and s not in new_days and s not in reverify:
            gaps.append(s)
        d += datetime.timedelta(days=1)
    days = new_days + reverify + sorted(gaps)
    seen, out = set(), []
    for s in days:
        if s in seen:
            continue
        seen.add(s)
        v = led.get(s) or {}
        if not force_month and int(v.get("fails") or 0) >= WOS_FAIL_SKIP:
            log("WoS 跳过反复失败的天", s, "fails", v.get("fails"), v.get("note"))
            continue
        out.append(s)
    # 不要在这里再 sorted()：那会把「新天优先」的次序打回日期升序，
    # 09-01 这种几万条的历史缺口又会排到最前把预算吃光。
    return out


def wos_incomplete():
    led = jload(WOS_LEDGER, {})
    bad = []
    for day, v in sorted(led.items()):
        if v.get("complete"):
            continue
        bad.append({"day": day, "total": v.get("total"), "rows": v.get("rows"),
                    "new": v.get("new"), "fails": v.get("fails"),
                    "note": v.get("note")})
    return bad


def audit_due():
    st = jload(AUDIT_STAMP, {})
    last = float(st.get("t") or 0)
    return (time.time() - last) > AUDIT_HOURS * 3600


def mark_audit():
    jdump(AUDIT_STAMP, {"t": time.time(), "ts": time.strftime("%Y-%m-%d %H:%M:%S")})


# ------------------------------------------------------------------ 主流程

def main():
    argv = sys.argv[1:]
    scopus_only = "--scopus-only" in argv
    wos_only = "--wos-only" in argv
    force_audit = "--audit" in argv
    run_id = time.strftime("%Y%m%d-%H%M%S")
    log("run", run_id, "PY", PY)

    if not take_lock():
        return 0
    status = {"run_id": run_id, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
              "host": os.environ.get("COMPUTERNAME", ""), "steps": {}, "rc": 0}
    try:
        # ---- 去重
        scp_file = os.path.join(SCP_DIR, "scopus_%s.jsonl" % time.strftime("%Y-%m"))
        wos_file = os.path.join(WOS_DIR, "wos_%s.jsonl" % time.strftime("%Y-%m"))
        for tag, path, key in (("scopus", scp_file, "eid"),
                               ("wos", wos_file, "ut_unique_wos_id")):
            try:
                raw, keep, drop = dedupe_jsonl(path, key)
                status["steps"]["dedupe-" + tag] = {"raw": raw, "keep": keep, "drop": drop}
                if drop:
                    log("dedupe", tag, "去掉重复", drop, "行")
            except Exception as e:
                log("dedupe-err", tag, str(e)[:100])

        # ---- WoS
        if not scopus_only:
            days = wos_days(force_audit or audit_due())
            log("WoS 待处理", days)
            rc_w, spent = 0, 0.0
            if days:
                rc_w, spent = run_step(
                    "wos", [os.path.join(HERE, "wos_pipeline.py")] + days,
                    WOS_BUDGET_MIN, os.path.join(LOG_DIR, "booknote_sched3h.log"))
            status["steps"]["wos"] = {"days": days, "rc": rc_w,
                                      "minutes": round(spent, 1),
                                      "incomplete": wos_incomplete()}
            if force_audit or audit_due():
                mark_audit()
            if rc_w:
                status["rc"] = status["rc"] or rc_w

            # WoS -> RDS。凭据在仓库根 .env 的 MYSQL_DSN。没配就跳过，不挡采集。
            if os.environ.get("MYSQL_DSN"):
                rc_m, spent_m = run_step(
                    "wos-mysql",
                    [os.path.join(HERE, "wos_to_mysql.py"), wos_file],
                    25, os.path.join(LOG_DIR, "booknote_sched3h.log"))
                status["steps"]["wos-mysql"] = {"rc": rc_m, "minutes": round(spent_m, 1)}
                if rc_m:
                    log("wos-mysql 失败 rc", rc_m, "采集已落盘，下轮再同步")
            else:
                log("wos-mysql 跳过：未设 MYSQL_DSN")

        # ---- Scopus
        # 2026-09-28 起只保留「按刊」一条线，按天那条整段移除。
        # 理由见文件头：日期轴被 Scopus 的 ORIG-LOAD-DATE 改动拖着走，永远收不敛；
        # 期刊轴一篇记录的所属刊不会变，拉完即停。按刊线同时负责发现新刊名
        # （采集中遇到没见过的 source 自动加进刊名表）。
        if not wos_only and os.environ.get("BOOKNOTE_SCOPUS_JOURNAL", "1") == "1":
            rc_j, spent_j = run_step(
                "scopus-journal",
                [os.path.join(HERE, "scopus_journal.py")],
                SCP_JOURNAL_BUDGET_MIN, os.path.join(LOG_DIR, "booknote_sched3h.log"))
            jst = jload(SCP_JOURNAL_STATE, {})
            jdone = sum(1 for x in jst.values() if x.get("done"))
            jtot = sum(int(x.get("tot") or 0) for x in jst.values())
            dst = jload(SCP_DETAIL_STATE, {}) or {}
            status["steps"]["scopus-journal"] = {
                "rc": rc_j, "minutes": round(spent_j, 1),
                "journals_total": len(jst), "journals_done": jdone,
                "records_in_window": jtot,
                "detail": {"ts": dst.get("ts"), "new": dst.get("new"),
                           "fail": dst.get("fail")},
            }
            if rc_j:
                status["rc"] = status["rc"] or rc_j
                log("scopus-journal 失败 rc", rc_j, "下一轮接着做")

        # Scopus -> RDS 两张表：2026_scopus_records（列表）+ 2026_scopus_details（详情）。
        # 建表 DDL 内嵌在 scopus_to_mysql.py 里，首次跑自动建。台账写
        # sync_run_log / sync_state 的 group_name='scopus-jsonl'，与 WoS 的 'wos-jsonl' 同构。
        # **一天只跑一次**（见 SCP_MYSQL_HOUR 的说明）。
        if not wos_only and os.environ.get("MYSQL_DSN"):
            if scopus_mysql_due(run_id):
                rc_s, spent_s = run_step(
                    "scopus-mysql",
                    [os.path.join(HERE, "scopus_to_mysql.py")],
                    SCP_MYSQL_BUDGET_MIN, os.path.join(LOG_DIR, "booknote_sched3h.log"))
                status["steps"]["scopus-mysql"] = {"rc": rc_s,
                                                   "minutes": round(spent_s, 1)}
                if rc_s:
                    log("scopus-mysql 失败 rc", rc_s, "今天还会再试一次（戳没写）")
                else:
                    mark_scopus_mysql_done()
            else:
                st = jload(SCP_MYSQL_STAMP, {}) or {}
                log("scopus-mysql 跳过：今天已同步过（%s）" % st.get("ts"))
                status["steps"]["scopus-mysql"] = {"rc": 0, "skipped": "done-today",
                                                   "at": st.get("ts")}
        elif not wos_only:
            log("scopus-mysql 跳过：未设 MYSQL_DSN")

        jdump(STATUS, status)
        w = status.get("steps", {}).get("wos", {})
        j = status.get("steps", {}).get("scopus-journal", {})
        log("[SUMMARY] rc", status["rc"],
            "| journal %s/%s" % (j.get("journals_done"), j.get("journals_total")),
            "窗口内记录", j.get("records_in_window"),
            "| detail", (j.get("detail") or {}).get("new"),
            "| scopus-mysql", (status["steps"].get("scopus-mysql") or {}).get("rc"),
            "| wos 未齐", [x["day"] for x in (w.get("incomplete") or [])])
        return status["rc"]
    finally:
        drop_lock()


if __name__ == "__main__":
    sys.exit(main())
