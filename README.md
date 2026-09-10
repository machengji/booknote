# booknote

WoS / Scopus 每日题录采集，以及各库全文 PDF 下载。仓库只放代码，不放账号、cookie、JSONL、PDF。

浏览器只用 **Camoufox**，有头窗口，复用 scihuber cookie。不要 OCR 刷登录，不要多开同一帐号。

## 准备

```bat
pip install -r requirements.txt
python -m camoufox fetch
copy .env.example .env
```

在 `.env` 里填 `SCIDOWNLOAD_USER` / `SCIDOWNLOAD_PWD`。题录默认写到 `E:\pubmed\wos` 和 `E:\pubmed\scopus`，可用 `BOOKNOTE_DATA` 改路径。

## 第一次登录（只需一次）

```bat
scripts\wait_login.bat
```

或：

```bat
python harvest/wos_pipeline.py --wait-login
```

弹出窗口后只填验证码并点登录。Cookie 写到 `%BOOKNOTE_DATA%\wos\scihuber_session.json`，当天不必再登。站点限制同帐号同时在线，登录前关掉其它 scihuber 窗口。

## 采集

指定一天：

```bat
python harvest/wos_pipeline.py 2026-09-08
python harvest/scopus_pipeline.py 2026-09-08
```

补采 `last_done` 之后到昨天：

```bat
python harvest/daily.py
```

Scopus 检索用：

```
ORIG-LOAD-DATE AFT <昨天> AND ORIG-LOAD-DATE BEF <明天>
```

不要用 `LOAD-DATE IS YYYYMMDD`（部分入口 500 / 0 结果）。南昌大学、武汉大学入口跳过。命中 0 条就停，不循环重搜。

## 每天自动跑

本机已登录 Windows 时，07:00 开有头窗口：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_daily_task.ps1
```

cookie 过期时任务会停在登录页。再跑一次 `--wait-login` 即可。不要加 `--auto-login`。

## 输出（本地，不提交）

| 库 | JSONL | 状态 |
| --- | --- | --- |
| WoS | `BOOKNOTE_DATA/wos/wos_YYYY-MM.jsonl` | `scihuber_state.json` |
| Scopus | `BOOKNOTE_DATA/scopus/scopus_YYYY-MM.jsonl` | `scopus_state.json` |

## 全文 PDF

流程和分库入口见 [`fulltext/FLOWS.md`](fulltext/FLOWS.md)。

公开 PDF：

```bat
python fulltext/generic/download_all.py --only-feasible
```

机构订阅（CSV 列 `id,pdf_url`）：

```bat
python fulltext/generic/real_crawl.py --map data\map.csv --limit 5
```

分库先铸会话再批量，例如 SAGE / T&F / IEEE / Wiley，脚本在 `fulltext/scid`、`fulltext/chaos`、`fulltext/zju`、`fulltext/zx`、`fulltext/wytsg`。

## 约束

- 账号不要写进代码、不要提交 `.env`
- 每天登录次数有限，优先复用 cookie
- 只开一个 Camoufox，不附加本机 Chrome
- JSONL / xls / 截图已在 `.gitignore`
