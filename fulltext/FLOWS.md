# 各库全文 PDF 下载流程

仓库只放代码。DOI 清单、cookie、PDF 都在本机 `BOOKNOTE_PDFS` / `BOOKNOTE_FULLTEXT`。

账号全部走 `.env`，不要写进脚本。浏览器只用 Camoufox，有头；人机验证需要时你点一次，之后复用会话。

## 通道

| 通道 | 适用 | 入口脚本 |
| --- | --- | --- |
| 直连 / OA | 公开 PDF（J-STAGE、MDPI、Frontiers、BMC…） | `generic/download_all.py` |
| 浙大 CARSI | Springer / IEEE / Nature / ScienceDirect / ACS / ACM | `generic/real_crawl.py`，`zju/*` |
| scihuber / scidownload | SAGE、部分 Wiley / Oxford / T&F / IOP | `scid/scid_download.py`，`scid/pub_gateway_batch.py` |
| 混沌书苑 | T&F、IOP、Ovid、RSC、ACS、ACM | `chaos/chaos_tf_session.py` + `chaos/chaos_tf_batch.py` |
| 知献 | Oxford、AIP、部分 T&F | `zx/zx_login.py`，`zx/ox_zx_batch.py`，`zx/aip_zx_batch.py` |
| 无忧 wytsg | Wiley（Cloudflare Turnstile） | `wytsg/wiley_download.py` |

## 推荐顺序

1. 公开能下的先跑 `generic/download_all.py`（不登录）。
2. 机构订阅走 CARSI：`generic/real_crawl.py --map <id,pdf_url.csv>`。
3. 仍缺的按库走网关：先 `session` 铸 cookie，再 `bulk`。
4. `gap_watch.py` 每小时对缺口库试 3 篇，命中再拉守护批量。

## 分库

| 库 | 流程 | 脚本 |
| --- | --- | --- |
| J-STAGE | doi.org 落地页抽 `_pdf` 链接，HK 代理 | `zju/jstage_batch.py` 或 `download_all` |
| Springer / BMC | 直链或 CARSI resource 9 | `real_crawl` / `download_all` |
| Wiley | wytsg `wiley.php?doi=` → SharedSP → 你点一次 Turnstile → cookie 批量 | `wytsg/wiley_download.py`，`wytsg/wiley_gateway_batch.py` |
| SAGE | scid ShowInfo → SharedSP → sagepub cookie → `/doi/pdf/` | `scid/sage_gateway.py`，`scid/sage_selfheal.py` |
| Oxford | 知献网关或 scid 入口 → `academic.oup.com/.../article-pdf/` | `zx/ox_zx_batch.py`，`scid/oxford_download.py` |
| T&F | 混沌卡进英文库 → `/auth/` 票 → tandfonline cookie → `/doi/pdf/{doi}?download=true` | `chaos/chaos_tf_session.py 41` 然后 `chaos_tf_batch.py` |
| IOP | 同混沌，`CHAOS_BASE=https://iop.66557.net`，`data_id=161` | `chaos_tf_session.py 161` + batch |
| Ovid | 混沌 `data_id=93` | `chaos/ovid_chaos_batch.py` |
| Elsevier | CARSI ScienceDirect（间隔 40s）或知献网关 | `real_crawl` |
| IEEE | ZJU WAYF 同浏览器 CAS（无验证码窗口）→ 页内 fetch getPDF；或镜像 `ieeexplore.66557.net` | `zju/ieee_zju_batch.py`，`zju/ieee_mirror_batch.py` |
| ASME | ZJU Shibboleth | `zju/asme_zju_batch.py` |
| World Scientific | ZJU | `zju/worldsci_zju_batch.py` |
| AIP | 知献 ShowInfo → aip.php 表单 → Camoufox 过 CF → `/doi/pdf/` | `zx/aip_zx_batch.py` |
| ACS / RSC / ACM | 混沌卡；桥接型卡不要轮询烧 `/auth/` 票 | `gap_watch.py` 配置 |
| PMC | 公开 PDF；缺 PMCID 的链接会失败 | `download_all` |

CARSI 资源号见 `carsi_resources.json`。scihuber 各库 `classid/id` 写在 `scid/pub_gateway_batch.py` 的 `PUBLISHERS`。

## 各通道注意

- **scihuber**：同帐号只能一人在线；优先复用 cookie，不要 OCR 刷登录。
- **混沌**：`/auth/` 票据有次数。先 session 再 bulk；`gap_watch` 每轮最多刷新 3 次。
- **Wiley / SAGE / Oxford**：发布商在 Cloudflare 后，不要用 requests 硬复用 UA；Camoufox 过验证后把真实 UA 和 cookie 存下来。
- **IEEE ZJU**：TSPD 指纹 cookie 不能跨浏览器，登录和下载必须同一 Camoufox context。
- **连续失败**：命中 0 就停，不要死循环同一查询。

## 输出

PDF 默认 `BOOKNOTE_PDFS/<库名>/<doi>.pdf`。断点文件 `*_done.txt` / `*_miss.txt` 在 `BOOKNOTE_LOG` 或 `BOOKNOTE_FULLTEXT`，不进 git。
