# -*- coding: utf-8 -*-
"""Scopus JSONL -> MySQL 两张表（列表 + 详情）。

用法:
    python harvest/scopus_to_mysql.py                      # 两张表都同步
    python harvest/scopus_to_mysql.py --mode=records       # 只同步列表
    python harvest/scopus_to_mysql.py --mode=details       # 只同步详情
    python harvest/scopus_to_mysql.py --dsn=mysql://... --records-table=... --details-table=...

DSN 形如 mysql://user:pwd@host:3306/db，默认读环境 MYSQL_DSN。
密码可以含 @（从**最后一个** @ 切开 host）。
建表 DDL 内嵌在本脚本里（CREATE TABLE IF NOT EXISTS），首次跑自动建，不用手工执行 SQL。

两条线的同步策略不一样，理由在代码里：
    列表  每次**全量 upsert**。月档 13 万行 / 几分钟，一轮一次可以承受；
          而且全量重写能顺带刷新 citedby（Scopus 会变），也不怕去重重写过文件。
          旧版把文件名当第一个位置参数传；现在自己扫 SCOPUS_OUT，不用传。
    详情  按**字节偏移增量**。详情档只 append（append_detail 用 "a" 打开），
          字节偏移稳定；全量重读不行——它会涨到 30 万行 / 2GB。

同步台账写 sync_run_log / sync_state，group_name='scopus-jsonl'，与 WoS 的
'wos-jsonl' 同构。甲方下游的 qiyans_sync_* 是他们自己的链路，本脚本不碰。
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)


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
os.environ.setdefault("SCOPUS_OUT", r"F:\pubmed\scopus")

OUT = os.environ["SCOPUS_OUT"]
DETAIL_STATE = os.path.join(OUT, ".scopus_detail_synced.json")

GROUP = "scopus-jsonl"
BATCH = 1000

# ------------------------------------------------------------------ DDL
DDL_RECORDS = """
CREATE TABLE IF NOT EXISTS `%s` (
  `eid`          VARCHAR(64)  NOT NULL COMMENT 'EID:Scopus唯一号，主键',
  `doi`          VARCHAR(256) NULL COMMENT 'DOI（已去 https://doi.org/ 前缀）',
  `title`        TEXT         NULL COMMENT '标题',
  `source_title` VARCHAR(512) NULL COMMENT '刊名/来源（列表 source 字段）',
  `source_id`    VARCHAR(32)  NULL COMMENT '来源ID',
  `issn`         VARCHAR(16)  NULL COMMENT 'ISSN（会议/书常无）',
  `year`         SMALLINT     NULL COMMENT '出版年',
  `citedby`      INT          NULL COMMENT '被引次数',
  `subtype`      VARCHAR(128) NULL COMMENT '文献子类型，ar=article',
  `authors`      TEXT         NULL COMMENT '作者缩写，分号分隔',
  `load_date`    DATE         NULL COMMENT '采集归属日（ORIG-LOAD-DATE 窗口）',
  `updated_at`   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                 ON UPDATE CURRENT_TIMESTAMP COMMENT '入库更新时间',
  PRIMARY KEY (`eid`),
  KEY `idx_doi` (`doi`),
  KEY `idx_year` (`year`),
  KEY `idx_source` (`source_title`(191)),
  KEY `idx_load_date` (`load_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Scopus 题录列表：一篇一行，来源=按刊线/历史按天线采到的检索结果'
"""

DDL_DETAILS = """
CREATE TABLE IF NOT EXISTS `%s` (
  `eid`                   VARCHAR(64)   NOT NULL COMMENT 'EID:Scopus唯一号，主键',
  `doi`                   VARCHAR(256)  NULL,
  `title`                 TEXT          NULL,
  `source_title`          VARCHAR(512)  NULL COMMENT '从 source.title 抽出',
  `source_id`             VARCHAR(32)   NULL COMMENT '从 source.id 抽出',
  `issn`                  VARCHAR(16)   NULL,
  `publication_year`      SMALLINT      NULL,
  `publication_stage`     VARCHAR(32)   NULL COMMENT 'FINAL / AIP 等',
  `language`              VARCHAR(64)   NULL,
  `open_access`           TINYINT       NULL COMMENT '1=开放获取',
  `volume`                VARCHAR(32)   NULL,
  `issue`                 VARCHAR(32)   NULL,
  `pages`                 VARCHAR(64)   NULL,
  `article_number`        VARCHAR(64)   NULL,
  `publisher`             VARCHAR(512)  NULL,
  `document_type`         VARCHAR(32)   NULL COMMENT 'type 字段，ar=article',
  `cited_by`              INT           NULL,
  `pubmed_id`             VARCHAR(32)   NULL COMMENT '从 pubmedInfo.id 抽出',
  `abstract_text`         MEDIUMTEXT    NULL COMMENT '摘要全文',
  `author_keywords`       TEXT          NULL COMMENT '作者关键词（JSON 数组）',
  `authors_json`          MEDIUMTEXT    NULL COMMENT '作者结构化',
  `affiliations_json`     MEDIUMTEXT    NULL COMMENT '机构结构化',
  `indexed_keywords_json` MEDIUMTEXT    NULL COMMENT '索引关键词（MSH/GDE）',
  `funding_json`          MEDIUMTEXT    NULL COMMENT '资助信息',
  `correspondences_json`  MEDIUMTEXT    NULL COMMENT '通讯作者及其机构',
  `source_json`           TEXT          NULL COMMENT 'source 原始对象',
  `is_missing`            TINYINT       NOT NULL DEFAULT 0 COMMENT '1=网关404，无详情',
  `load_date`             DATE          NULL COMMENT '采集归属日（与列表表对齐）',
  `fetched_at`            DATETIME      NULL COMMENT '详情抓取时间',
  `updated_at`            TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                          ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`eid`),
  KEY `idx_doi` (`doi`),
  KEY `idx_source` (`source_title`(191)),
  KEY `idx_load_date` (`load_date`),
  KEY `idx_pubmed` (`pubmed_id`),
  KEY `idx_year` (`publication_year`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Scopus 详情：摘要/机构/关键词/基金/卷期页；按 eid 与列表表一一对应'
"""

REC_COLS = ("eid,doi,title,source_title,source_id,issn,year,citedby,subtype,"
            "authors,load_date").split(",")
DET_COLS = ("eid,doi,title,source_title,source_id,issn,publication_year,"
            "publication_stage,language,open_access,volume,issue,pages,article_number,"
            "publisher,document_type,cited_by,pubmed_id,abstract_text,author_keywords,"
            "authors_json,affiliations_json,indexed_keywords_json,funding_json,"
            "correspondences_json,source_json,is_missing,load_date,fetched_at").split(",")


def parse_dsn(dsn):
    """mysql://user:password@host:port/db。密码可以含 @，从最后一个 @ 切开 host。"""
    assert dsn.startswith("mysql://"), dsn
    body = dsn[len("mysql://"):]
    at = body.rfind("@")
    if at < 0:
        raise ValueError("dsn missing @host")
    auth, hostdb = body[:at], body[at + 1:]
    user, _, pwd = auth.partition(":")
    hostport, _, db = hostdb.partition("/")
    host, _, port = hostport.partition(":")
    try:
        from urllib.parse import unquote
        user, pwd = unquote(user), unquote(pwd)
    except Exception:
        pass
    return {"host": host or "127.0.0.1", "port": int(port or 3306),
            "user": user, "password": pwd,
            "database": db or "qiyan_literature_collection"}


def to_int(v):
    try:
        if v in (None, ""):
            return None
        if isinstance(v, bool):
            return 1 if v else 0
        if isinstance(v, float):
            return int(v)
        return int(str(v).strip().split(".")[0])
    except Exception:
        return None


def jdump(v):
    if v in (None, "", [], {}):
        return None
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def s(v, n=None):
    if v in (None, ""):
        return None
    t = str(v).strip()
    if not t:
        return None
    return t[:n] if n else t


def day(v):
    t = str(v or "").strip()[:10]
    return t or None


def dt(v):
    t = str(v or "").strip()[:19]
    return t or None


def mark_sync(conn, source_table, status, read_n, write_n, skip_n, err=""):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sync_run_log (group_name, source_table, started_at, "
                "finished_at, status, read_count, write_count, skip_count, error) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (GROUP, source_table, now, now, status, int(read_n), int(write_n),
                 int(skip_n), (err or "")[:2000] or None))
            cur.execute(
                "INSERT INTO sync_state (source_table, group_name, last_run_at, "
                "last_status, last_message, read_count, write_count) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE last_run_at=VALUES(last_run_at), "
                "last_status=VALUES(last_status), last_message=VALUES(last_message), "
                "read_count=VALUES(read_count), write_count=VALUES(write_count)",
                ("jsonl:" + source_table, GROUP, now, status,
                 ("read=%s write=%s skip=%s" % (read_n, write_n, skip_n))[:1000],
                 int(read_n), int(write_n)))
        conn.commit()
    except Exception as e:
        print("sync-log-skip", str(e)[:120], flush=True)


# ------------------------------------------------------------------ 列表
def month_files():
    out = []
    try:
        names = sorted(os.listdir(OUT))
    except Exception:
        return out
    for n in names:
        if (n.startswith("scopus_") and n.endswith(".jsonl")
                and len(n) == len("scopus_YYYY-MM.jsonl")):
            out.append(os.path.join(OUT, n))
    return out


def row_records(r):
    src = r.get("source")
    if isinstance(src, dict):          # 详情档的 source 是对象，取 title
        src = src.get("title") or ""
    return [s(r.get("eid"), 64), s(r.get("doi"), 256), s(r.get("title")),
            s(src, 512), s(r.get("source_id"), 32), s(r.get("issn"), 16),
            to_int(r.get("year")), to_int(r.get("citedby")),
            s(r.get("subtype"), 128), s(r.get("authors")), day(r.get("load_date"))]


def sync_records(conn, table, identity):
    cols = REC_COLS
    sql = ("INSERT INTO %s (%s) VALUES (%s) ON DUPLICATE KEY UPDATE "
           "doi=VALUES(doi),title=VALUES(title),source_title=VALUES(source_title),"
           "source_id=VALUES(source_id),issn=VALUES(issn),year=VALUES(year),"
           "citedby=VALUES(citedby),subtype=VALUES(subtype),authors=VALUES(authors),"
           "load_date=VALUES(load_date)" % (identity, ",".join(cols),
                                            ",".join(["%s"] * len(cols))))
    n = skip = 0
    batch = []
    for path in month_files():
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    skip += 1
                    continue
                row = row_records(r)
                if not row[0]:
                    skip += 1
                    continue
                batch.append(tuple(row))
                if len(batch) >= BATCH:
                    with conn.cursor() as cur:
                        cur.executemany(sql, batch)
                    conn.commit()
                    n += len(batch)
                    batch = []
                    print("records loaded", n, flush=True)
    if batch:
        with conn.cursor() as cur:
            cur.executemany(sql, batch)
        conn.commit()
        n += len(batch)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM %s" % identity)
        dbn = cur.fetchone()[0]
    print("RECORDS DONE loaded=%d skipped=%d db_count=%d" % (n, skip, dbn), flush=True)
    mark_sync(conn, table, "ok", n + skip, n, skip)
    return n, skip


# ------------------------------------------------------------------ 详情
def row_details(r):
    so = r.get("source")
    so = so if isinstance(so, dict) else {}
    pi = r.get("pubmedInfo")
    pi = pi if isinstance(pi, dict) else {}
    return [s(r.get("eid"), 64), s(r.get("doi"), 256), s(r.get("title")),
            s(so.get("title"), 512), s(so.get("id"), 32), s(r.get("issn"), 16),
            to_int(r.get("publicationYear")), s(r.get("publicationStage"), 32),
            s(r.get("language"), 64), to_int(r.get("openAccess")),
            s(r.get("volume"), 32), s(r.get("issue"), 32), s(r.get("pages"), 64),
            s(r.get("articleNumber"), 64), s(r.get("publisher"), 512),
            s(r.get("type"), 32), to_int(r.get("citedBy")), s(pi.get("id"), 32),
            s(r.get("abstract")), jdump(r.get("authorKeywords")),
            jdump(r.get("authors")), jdump(r.get("affiliations")),
            jdump(r.get("indexedKeywords")), jdump(r.get("fundingDetails")),
            jdump(r.get("correspondences")), jdump(so),
            1 if r.get("missing") else 0, day(r.get("load_date")), dt(r.get("ts"))]


def detail_files():
    out = []
    try:
        names = sorted(os.listdir(OUT))
    except Exception:
        return out
    for n in names:
        if (n.startswith("scopus_detail_") and n.endswith(".jsonl")
                and len(n) == len("scopus_detail_YYYY-MM.jsonl")):
            out.append(os.path.join(OUT, n))
    return out


def load_detail_state():
    try:
        with open(DETAIL_STATE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_detail_state(st):
    try:
        tmp = DETAIL_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, DETAIL_STATE)
    except Exception as e:
        print("detail-state-save-skip", str(e)[:100], flush=True)


def sync_details(conn, table, identity):
    """从上次的字节偏移继续读。详情档只 append，偏移稳定。

    偏移失效的两种情况都会自动回退全量重读：
      * 文件被截断/替换（当前大小 < 记录的偏移）
      * 记录的偏移处不是行首（说明别处重写过这个文件）
    重复 upsert 是幂等的，所以回退只会慢一点，不会写脏。
    """
    st = load_detail_state()
    cols = DET_COLS
    sql = ("INSERT INTO %s (%s) VALUES (%s) ON DUPLICATE KEY UPDATE "
           % (identity, ",".join(cols), ",".join(["%s"] * len(cols)))
           + ",".join("%s=VALUES(%s)" % (c, c) for c in cols if c != "eid"))
    n = skip = 0
    for path in detail_files():
        key = os.path.basename(path)
        rec = st.get(key) or {}
        off = int(rec.get("offset") or 0)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if off > size:
            print("details offset stale (%d > %d) 全量重读 %s" % (off, size, key), flush=True)
            off = 0
        batch = []
        added = 0
        with open(path, "rb") as f:
            if off:
                f.seek(off - 1)
                if f.read(1) != b"\n":
                    print("details offset 不在行首，全量重读", key, flush=True)
                    off = 0
            f.seek(off)
            while True:
                raw = f.readline()
                if not raw:
                    break
                off += len(raw)
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    skip += 1
                    continue
                row = row_details(r)
                if not row[0]:
                    skip += 1
                    continue
                batch.append(tuple(row))
                if len(batch) >= BATCH:
                    with conn.cursor() as cur:
                        cur.executemany(sql, batch)
                    conn.commit()
                    n += len(batch)
                    added += len(batch)
                    batch = []
                    # 每批落一次偏移：中途挂了也不会从头重读
                    save_detail_state(dict(st, **{key: {
                        "offset": off,
                        "lines": int(rec.get("lines") or 0) + added,
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S")}}))
                    print("details loaded", n, flush=True)
        if batch:
            with conn.cursor() as cur:
                cur.executemany(sql, batch)
            conn.commit()
            n += len(batch)
            added += len(batch)
            batch = []
        st[key] = {"offset": off,
                   "lines": int(rec.get("lines") or 0) + added,
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        save_detail_state(st)
        print("details file %s 本次新增 %d 条，偏移推进到 %d" % (key, added, off), flush=True)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM %s" % identity)
        dbn = cur.fetchone()[0]
    print("DETAILS DONE loaded=%d skipped=%d db_count=%d" % (n, skip, dbn), flush=True)
    mark_sync(conn, table, "ok", n + skip, n, skip)
    return n, skip


def main():
    import pymysql
    argv = sys.argv[1:]

    def opt(name, default=""):
        return next((a.split("=", 1)[1] for a in argv if a.startswith("--%s=" % name)), default)

    mode = opt("mode", "both")
    dsn = opt("dsn") or os.environ.get("MYSQL_DSN", "")
    rec_t = (opt("records-table") or os.environ.get("MYSQL_SCOPUS_TABLE")
             or "2026_scopus_records")
    det_t = (opt("details-table") or os.environ.get("MYSQL_SCOPUS_DETAIL_TABLE")
             or "2026_scopus_details")
    if not dsn:
        print("need --dsn=mysql://user:pwd@host:3306/db or MYSQL_DSN env")
        return 2
    kw = parse_dsn(dsn)
    print("dsn host", kw["host"], "db", kw["database"], "user", kw["user"],
          "| records", rec_t, "| details", det_t, "| mode", mode, flush=True)
    conn = pymysql.connect(charset="utf8mb4", autocommit=False, **kw)
    rec_i = "`%s`" % rec_t.replace("`", "")
    det_i = "`%s`" % det_t.replace("`", "")
    rc = 0
    try:
        with conn.cursor() as cur:
            # DDL 模板里已经带反引号了，这里传**裸表名**；rec_i/det_i 是给 DML 用的。
            cur.execute(DDL_RECORDS % rec_t)
            cur.execute(DDL_DETAILS % det_t)
        conn.commit()
        print("tables ready", flush=True)
        if mode in ("both", "records"):
            try:
                sync_records(conn, rec_t, rec_i)
            except Exception as e:
                rc = 1
                print("RECORDS FAIL", str(e)[:400], flush=True)
                try:
                    mark_sync(conn, rec_t, "error", 0, 0, 0, str(e)[:400])
                except Exception:
                    pass
        if mode in ("both", "details"):
            try:
                sync_details(conn, det_t, det_i)
            except Exception as e:
                rc = 1
                print("DETAILS FAIL", str(e)[:400], flush=True)
                try:
                    mark_sync(conn, det_t, "error", 0, 0, 0, str(e)[:400])
                except Exception:
                    pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
