# Scopus 5000 条上限：成因、绕法、逐日对账口径

结论先说：这个网关**没有分页**，翻页必然漏。要「一天几万条一条不漏」，只有一条路——
把一天切成互斥且完备的小格子，每格只取第一页；再用 `totalCount` 做逐日对账。

## 1. 上限到底是什么

采集走的是 scihuber → 机构镜像 → Elsevier 页面网关：

```
POST {gateway}/gateway/documents/search
body: {"query": "...", "resultSet": {"offset": 0, "itemCount": 1000}, "sortBy": [datesort]}
```

实测约束（`harvest/scopus_pipeline.py: API_WINDOW = 5000`）：

- `resultSet.offset + itemCount > 5000` 直接被拒；
- **`offset` 被网关忽略**：`offset=1000` 返回的是与第一页重叠的结果，不是第 2 页；
- 可排序字段只有 `datesort`；`eid` / `doi` / `title` 排序一律 `BAD_REQUEST`；
- 没有 cursor（游标只在官方 Elsevier Search API 上，且机构版要付费 `insttoken`）；
- facet 端点只回 7 个 source title，做不了期刊级切分。

所以「把 offset 一页页翻完」在本网关上是**不存在**的路径。历史上一度靠
`offset` 翻页，结果是每天 unique eid 都小于 `totalCount`（例：cp 1837→1814、
`aip/10.1109*` 2073→1985），差额就是被忽略的 offset 造成的重叠丢失。

`metadata.totalCount` 本身是准的，**即使 >5000 也是真实当日总量**，这是对账的基准。

## 2. 绕法：互斥完备切分 + 每格只取第一页

核心不变式：**每个格子的 `totalCount ≤ 1000`，且每格只发一次 `api_first`（不传 offset）**。

切分器按顺序试，第一个「通过校验」的生效（`scopus_le1000.py: SPLITTERS`）：

| 切分器 | 查询写法 | 实测完备性 |
| --- | --- | --- |
| doctype | `DOCTYPE(ar)` … + `NOT DOCTYPE(ar OR cp …)` | 各格之和 == 父格（25276 精确吻合） |
| pubyear | `PUBYEAR = 2026` … + `NOT PUBYEAR = …` | 同上 |
| language | `LANGUAGE(english)` … + `NOT LANGUAGE(…)` | 同上 |
| pubstage | `PUBSTAGE(aip)` / `PUBSTAGE(final)` / 其余 | aip+final == 父格（16203 精确吻合） |
| doi | `DOI(10.1016*)` … + `NOT DOI(…)` | 前缀互斥，和 == 父格 |
| issn | 离线 ISSN 全集分桶 400 个/桶 | 来自官方 source list，覆盖全 |
| doi-suf | 已按 `10.xxxx*` 收窄后再切下一字符 | 同上 |
| issn-half | 单个 ISSN 桶二分 | 同上 |
| jpage | `EXACTSRCTITLE("…")` 逐刊 + 余项 | 同上 |

校验函数 `parts_ok()` 要求：**子格之和 ≥ 父格 85%**、且**不能有一个子格几乎等于父格**
（说明这一刀没切开）。不满足就换下一个切分器。递归到 `totalCount ≤ 1000` 就是叶子，
取第一页即全量。

注意：`EID(*)`、`ISSN(0*)`、`DOI(*)` 这类通配在网关上是 0 结果；`DOI(10.1016*)` 这类
**前缀通配可用**。`NOT EID(2-s2.0-xxx)` 精确匹配可用（排除 N 个 eid，总量正好减 N），
但**不能用来翻页**：同一查询仍然返回剩余集合的前 5000 条，排完 516 个之后旧记录又填满窗口，
第 5 轮 `new=0`，滑不动。这就是「NOT 滑不动」的原因。

## 3. 判定「采全」的唯一口径

```python
ok = 当天本地 unique eid 数 >= 当天 metadata.totalCount
```

三个必须守住的点：

1. **不能用行数判定**。历史上有多个临时脚本各自追加，同一 eid 会有多行。
   `sched_3h.py` 每轮开头按主键去重一次。
2. **不能只靠 `last_done` 游标**。`totalCount` 会随时间增长，昨天的「已采全」今天可能又短了。
   所以每轮都**整月重扫**：每天先花 1 次查询取 `totalCount`，只有 `uniq < total` 的天才补采。
   全月都齐时，一轮只要 ~17 次查询（约 2 分钟）。
3. **失败不能伪装成 0 条**。401/403/429/5xx 时 `api_first` 返回空，与「这天真的没数据」
   长得一样。现在 `scopus_pipeline.bad_count()` 记录非 200 响应，任何一次查询出错就
   **立刻停手**（不烧配额），这天标 `http:` 交给下一轮。

### 3.1 实测抓到的「假采全」：目录里的探针文件把 uniq 抬高了

这是 09-16 差 239 条却一直显示「接近采全」的真正原因。

`load_have()` 原来用 `name.startswith("scopus_") and name.endswith(".jsonl")` 收集文件，
把目录里的探针产物 `scopus_eid_peel_test.jsonl` 也算了进去。那个文件是 NOT-EID
剥离实验留下的，里面 5518 条记录的 `load_date` 正是 2026-09-16：

| 文件 | 09-16 行数 | 09-16 unique eid |
| --- | --- | --- |
| `scopus_2026-09.jsonl`（正式月档） | 25037 | 25037 |
| `scopus_eid_peel_test.jsonl`（探针残留） | 5518 | 5518 |
| 旧 `load_have` 合计 | — | **30555** |

当天 `totalCount` 是 25276，于是 `30555 >= 25276` 判定为「已采全」——
缺口 239 条被永久掩盖，而且每轮都跳过补采。

修法：`load_have()` 只认严格匹配 `^scopus_(\d{4}-\d{2})\.jsonl$` 的正式月档，
并且只读本次要扫描的月份；探针文件已挪到 `F:\pubmed\scopus\_probe\`。

修完复验：09-16 = 25037 lines / 25037 uniq，与正式月档一致。

> 教训：**对账口径必须建立在「正式产物」上**。同一目录里只要混进任何带
> `load_date` 的旁路文件，`uniq >= totalCount` 这个判据就会被污染，
> 而且表现为「看起来快采全了」而不是报错——最难发现的一类漏。

产物：
- `F:\pubmed\logs\scopus_day_verify.json` — 逐日 `{total, uniq, ok, note}`
- `F:\pubmed\scopus\scopus_state.json` — `last_done` = 最大的 `uniq >= total` 的天
- `[LEAK]` 日志 — 所有切分器都失败、仍 >1000 的格子（正常应为空）

## 4. 代价

一天 ~25,000 条大约需要 150～250 次网关调用（切分探针 + 叶子取数）。
网关节流在代码里（`GAP 3.5s + jitter 1.8s`，每 18 次插 16s 停顿，429/418/503 指数退避），
所以一天约 15～25 分钟。这也是为什么用「每 3 小时一轮、多轮续采」而不是一口气跑完。

## 5. 定时任务

| 任务 | 时间 | 干什么 |
| --- | --- | --- |
| `booknote-3h` | 每 3 小时（00:05 起） | WoS 补新天 + 复采近 3 天 + 账本缺口；Scopus 整月对账并补不足的天 |

单实例锁 `F:\pubmed\logs\booknote_3h.lock`（PID + 存活检查 + 200 分钟上限），
避免和上一轮抢同一个 scihuber 帐号（站点限制「同一帐号只能一人在线」）。
原先 07:00 / 07:20 的两个每日任务已停用，避免与 3 小时轮冲突。

状态文件：

- `F:\pubmed\logs\booknote_3h_status.json` — 每轮总览（双库 rc、耗时、缺哪些天）
- `F:\pubmed\logs\scopus_day_verify.json` — Scopus 逐日 `{total, uniq, ok}`
- `F:\pubmed\wos\wos_day_ledger.json` — WoS 逐日 `{total, total_max, rows, new, fails}`
- `F:\pubmed\scopus\scopus_entry_health.json` — 入口健康度（连续失败 3 次的入口搁置 6 小时）

### 5.1 Scopus 入口现状（2026-09-18 实测）

入口列表在 `https://www.scidownload.com/e/action/ListInfo/?classid=225`，
实测该页只剩三个，代码里旧的 `Kr1(5841)`、`NCU(6112)` 已经不在列表上：

| 入口 | 镜像 | 状态 |
| --- | --- | --- |
| `Scopus(mlkd)` 5460 | 202.38.93.14 | **唯一可用**，但会撞日配额 `403 当日下载量已超标，请24小时后再试` |
| `Scopus(No1)` 6065 | sjk.x.scihub.top/nosj | 镜像 `404 Not Found nginx` |
| `Scopus(No2)` 6086 | scopus.x.scihub.top | `Session establish failed, please retry.` |

配额是**校园帐号的日下载量**（mlkd 侧帐号 2020102040014），提示「24小时后再试」，
不是跨零点重置。配额耗尽期间 3 小时轮会每轮探一次，一旦恢复就自动续采。

## 6. WoS 的口径（对照）

WoS 不走 `ORIG-LOAD-DATE`，而是按 **DOP（出版日期）** `day/day` 检索，导出 xls 后按
`ut_unique_wos_id` 去重追加。判定口径：

```python
ok = 本次导出行数 rows >= 本次检索命中数 total
```

原实现只按 `max-batch` 判定，**导出行数少于 total 也会推进 `last_done`**——这是 WoS 侧的漏点，
已修。WoS 记录要与官方导出列 1:1，所以不往记录里塞 `load_date`，逐日账本单独落
`F:\pubmed\wos\wos_day_ledger.json`。复采某天时 `new == 0` 即说明该天已齐
（UT 全局唯一，去重是精确的）。

注意 WoS 的 DOP 天会持续增长（新 indexed 的文献不断补进历史出版日），
所以近几天要反复复采，这也是 3 小时一轮的必要性之一。

### 6.1 本轮查出的两个 WoS 漏点

**漏点一：短窗口被当成「没有更多」。** 原导出循环里写了
`if nrows < BATCH: break`，只要某一批返回不足 1000 行就认为该天导出完了。
实测 09-01（`WOS(nju)`，命中 37626）第 6 批只回了 925 行，循环就此中断，
**只拿到 5925 / 37626 条**。WoS 端偶发少给几行是正常的，不能当终止条件。
现在改成：把 `[1..total]` 每个千条窗口都请求一遍，短窗口记进 `short` 列表，
账本里 `complete` 额外要求 `short == 0`。

**漏点二：导出超时被当成失败。** 页面默认超时 90s，大命中数生成 xls 更慢，
报 `Page.evaluate: The operation was aborted`。现在导出单独用
`WOS_EXPORT_TIMEOUT_MS`（默认 300s），导出前后临时切换，不影响其它调用。

### 6.2 各机构镜像的 WoS 口径不一样

同一个 DOP 天，不同镜像给出的命中数不同（实测 09-01）：
`WOS(shu)=45311`、`WOS(nju)=37626`。差别来自各校 WoS 订阅范围不同。
所以账本里记 `total_max`（该天在所有入口里见过的最大值），
`complete` 要求 `rows >= total_max`，否则标注 `scope: entry total X < max Y`。
只按小口径判定会把大口径独有的记录当成「已采全」。

### 6.3 WoS 导出的硬墙：passiveVerificationRequired

`WOS(nju)` 连续导出约 6 批之后开始返回：

```
500 {"errc":"Server.passiveVerificationRequired",
     "errm":"Verifiable request limit exceeded [verified=false personalized=false limit=0 used=1 ...]"}
```

即该镜像对批量导出要求人工验证（reCAPTCHA）。`WOS(shu)` 走上海图书馆代理，
大命中数的导出直接 `The operation was aborted`。

结论：**WoS 的「新天增量」是可靠的**（09-15 是 2274 行 / 3 批、09-16 是 75 行 / 1 批，
都能采全），但**单天几万条的大天批量导出会被验证墙挡住**。
遇到这种天的处理：账本标 `fails`，连续失败 3 次后 3 小时轮跳过，
只在整月复核（默认 24 小时一次）时用大口径入口再试一次，避免把每轮预算烧光。

