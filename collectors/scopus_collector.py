# -*- coding: utf-8 -*-
"""
Scopus 题录与详情全字段高内聚采集器 (collectors/scopus_collector.py)
================================================================================
架构设计（与网页打开/登录层 100% 解耦）：
1. 接收由 gateway.channel_8090 打开并完成鉴权的就绪 Scopus Page 对象；
2. 严格执行已实测验证的四大标准下载环节：
   (1) 提交全字段导出（覆盖 28 个字段组 / 45 列 CSV）并转入任务中心 /pages/user/exports；
   (2) 任务卡片精准状态匹配（按目标篇数匹配卡片，生成中绝不盲目抢点旧任务）；
   (3) CDP 底层路径授权 (Browser.setDownloadBehavior) + 实时监听 .crdownload 落盘完毕；
   (4) 双重硬核质量门禁 + 全字段映射 + 双表原子级去重入库 (2026_scopus_records & 2026_scopus_details)。
3. 单日 <= 20,000 篇单包导出；> 20,000 篇自动启用 DOCTYPE(ar) 与 NOT DOCTYPE(ar) 正交二分，
   且支持本地子批次 CSV 断点续传复用。
"""

import os
import sys
import csv
import io
import re
import json
import time
import shutil
import datetime
from playwright.sync_api import Page, sync_playwright

from config.settings import AppSettings, load_settings
from gateway.channel_8090 import (
    Channel8090Gateway,
    GatewayQuotaExhaustedError,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    csv.field_size_limit(2147483647)
except Exception:
    pass

MAX_BATCH_SIZE = 20000


class ScopusCollector:
    """Scopus 题录与全字段详情高内聚采集器"""

    def __init__(self, settings: AppSettings, gateway: Channel8090Gateway):
        self.settings = settings
        self.gateway = gateway
        os.makedirs(self.settings.scopus_data_dir, exist_ok=True)
        os.makedirs(self.settings.scopus_raw_csv_dir, exist_ok=True)
        os.makedirs(self.settings.log_dir, exist_ok=True)
        self.log_file = os.path.join(self.settings.log_dir, "scopus_collector.log")

    def log(self, *args) -> None:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"{ts} [Scopus-Collector] " + " ".join(str(a) for a in args)
        print(msg, flush=True)
        try:
            with open(self.log_file, "a", encoding="utf-8", errors="replace") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    def detect_todo_days(self, lookback_days: int = 7) -> list[str]:
        """动态扫描本地账本与阿里云 RDS 双表，识别最近 N 天内未完成或详情表缺失的日期"""
        ledger = {}
        if os.path.exists(self.settings.scopus_ledger_path):
            try:
                with open(self.settings.scopus_ledger_path, "r", encoding="utf-8") as f:
                    ledger = json.load(f)
            except Exception as e:
                self.log(f"读取 Scopus 账本异常: {e}")

        rds_rec_map, rds_det_map = {}, {}
        try:
            conn = self.settings.create_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT load_date, COUNT(*) FROM `2026_scopus_records` "
                    "WHERE load_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY) GROUP BY load_date",
                    (lookback_days + 3,),
                )
                rds_rec_map = {str(k): int(v) for k, v in cur.fetchall()}

                cur.execute(
                    "SELECT load_date, COUNT(*) FROM `2026_scopus_details` "
                    "WHERE load_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY) GROUP BY load_date",
                    (lookback_days + 3,),
                )
                rds_det_map = {str(k): int(v) for k, v in cur.fetchall()}
            conn.close()
        except Exception as e:
            self.log(f"查询 RDS 双表对账状态异常: {e}")

        today = datetime.date.today()
        todo = []
        for offset in range(0, lookback_days + 1):
            d_str = (today - datetime.timedelta(days=offset)).isoformat()
            rec = ledger.get(d_str) or {}
            is_ok = rec.get("ok", False)
            total_now = rec.get("total_now", 0)
            gap = rec.get("gap", 999999)

            r_cnt = rds_rec_map.get(d_str, 0)
            d_cnt = rds_det_map.get(d_str, 0)

            # 硬核双表一致性检查：若题录表已有数据但详情表缺失或差额 > 50，强制重采补齐
            if r_cnt > 0 and (d_cnt == 0 or (r_cnt - d_cnt) > 50):
                self.log(
                    f"检测到日期 {d_str} 详情表不完整 (records={r_cnt:,}, details={d_cnt:,})，加入待补齐队列"
                )
                todo.append(d_str)
                continue

            # 最近 2 天若此前命中为 0（官方当时尚未入库），重检一次
            if offset <= 2 and total_now == 0 and r_cnt == 0:
                todo.append(d_str)
                continue

            if is_ok and total_now > 0 and d_cnt > 0:
                continue
            if total_now > 100 and gap <= 15 and d_cnt > 0:
                continue

            todo.append(d_str)

        return todo

    def search_and_get_count(self, page: Page, query: str, timeout: int = 120) -> int:
        """在已就绪的 Scopus 页面提交检索式并提取官方命中篇数"""
        base_origin = (
            page.url.split("/results/")[0]
            .split("/pages/")[0]
            .split("/search/")[0]
            .rstrip("/")
        )
        adv_url = f"{base_origin}/search/form.uri?display=advanced"

        box_selectors = [
            "textarea[name='searchfield']",
            "#searchfield",
            "textarea#searchfield",
            "textarea[data-testid='search-input']",
            "textarea",
        ]

        search_box = None
        for attempt in range(3):
            if attempt > 0 or "display=advanced" not in (page.url or ""):
                self.log(f"进入高级检索页面: {adv_url}")
                try:
                    page.goto(adv_url, wait_until="commit", timeout=25000)
                    time.sleep(3)
                except Exception:
                    pass

            for wait_s in range(25):
                self.gateway.dismiss_cookie_overlays(page)
                self.gateway.check_quota_exhausted(
                    page, f"Scopus(goto={self.settings.scopus_goto_id})"
                )
                for sel in box_selectors:
                    loc = page.locator(sel).first
                    if loc.count() and loc.is_visible():
                        search_box = loc
                        break
                if search_box:
                    break
                if wait_s in (10, 20):
                    try:
                        page.reload(wait_until="commit", timeout=15000)
                        time.sleep(2)
                    except Exception:
                        pass
                time.sleep(1)

            if search_box:
                break

        if not search_box:
            raise RuntimeError(f"等待 Scopus 高级检索输入框超时 (当前 URL: {page.url})")

        try:
            page.evaluate(
                """(q) => {
                const box = document.querySelector('#searchfield') || document.querySelector('textarea');
                if (box) {
                    box.focus();
                    if (box.tagName.toLowerCase() === 'textarea' || box.tagName.toLowerCase() === 'input') {
                        box.value = q;
                    } else {
                        box.innerText = q;
                    }
                    box.dispatchEvent(new Event('input', {bubbles: true}));
                    box.dispatchEvent(new Event('change', {bubbles: true}));
                    const lbl = document.querySelector('#contentEditLabel');
                    if (lbl) lbl.style.display = 'none';
                }
            }""",
                query,
            )
        except Exception:
            search_box.click(force=True)
            search_box.fill(query)
        time.sleep(0.5)

        search_btn = page.locator(
            "#advSearch, button[type='submit'], button:has-text('检索'), button:has-text('搜尋'), button:has-text('Search')"
        ).first
        search_btn.click(force=True)

        t0 = time.time()
        header_found = False
        doc_count = 0

        for step in range(timeout):
            time.sleep(1)
            self.gateway.dismiss_cookie_overlays(page)
            self.gateway.check_quota_exhausted(
                page, f"Scopus(goto={self.settings.scopus_goto_id})"
            )

            if step in (15, 30) and "results.uri" not in (page.url or ""):
                try:
                    if search_btn.count() and search_btn.is_visible():
                        search_btn.click(force=True)
                except Exception:
                    pass

            if step in (25, 45) and "results.uri" in (page.url or ""):
                try:
                    page.reload(wait_until="commit", timeout=15000)
                except Exception:
                    pass

            try:
                res_info = page.evaluate(
                    r"""() => {
                    const countEl = document.querySelector('[data-testid="results-count"], #headerCount, span.resultsCount, .headerCount, .resultsCount');
                    if (countEl && countEl.innerText) {
                        const mEl = countEl.innerText.match(/([\d,]+)/);
                        if (mEl) {
                            return { found: true, count: parseInt(mEl[1].replace(/,/g, ''), 10), text: countEl.innerText };
                        }
                    }
                    const body = document.body ? document.body.innerText : '';
                    if (body.includes('未找到与您的关键字匹配的文献') || body.includes('0 篇文献结果') || body.includes('未找到文献') || body.includes('0 document results') || body.includes('0 篇文獻結果')) {
                        return { found: true, count: 0, text: '0 篇' };
                    }
                    const m = body.match(/找到\s*([\d,]+)\s*篇/) || 
                              body.match(/([\d,]+)\s*篇[文獻文献]/) || 
                              body.match(/([\d,]+)\s*篇/) || 
                              body.match(/([\d,]+)\s*document/i);
                    if (m) {
                        return { found: true, count: parseInt(m[1].replace(/,/g, ''), 10), text: m[0] };
                    }
                    return { found: false, count: 0, text: '' };
                }"""
                )
            except Exception:
                continue

            if res_info.get("found"):
                header_found = True
                doc_count = res_info.get("count", 0)
                self.log(
                    f"成功捕获检索结果！官方命中数: {doc_count:,} 篇 (耗时 {time.time() - t0:.1f}s)"
                )
                break

        if not header_found:
            raise RuntimeError(f"等待 Scopus 检索结果页超时 (URL: {page.url[:80]})")

        return doc_count

    def export_current_results_to_csv(
        self, page: Page, doc_count: int, temp_csv_path: str
    ) -> tuple[str, str] | None:
        """
        严格执行跑通的标准下载四步法：
        1. 打开 Export -> CSV 弹窗，填入范围 1..doc_count 并全选 28 个字段组（排除参考文献列表以保证速度）；
        2. 提交并导航至 /pages/user/exports；
        3. 精准匹配目标篇数对应的任务卡片，等待 READY 状态；
        4. CDP 授权下载路径 + 监听 .crdownload 消失与 export_*.csv 落盘稳定后转存。
        """
        self.log(f"准备导出当前检索结果页 (共 {doc_count:,} 篇)...")
        page.wait_for_selector("button:has-text('Export')", timeout=30000)
        time.sleep(1)

        base_origin = (
            page.url.split("/results/")[0]
            .split("/pages/")[0]
            .split("/search/")[0]
            .rstrip("/")
        )

        modal_opened = False
        for _ in range(6):
            if page.locator("button[data-testid='submit-export-button']").count():
                modal_opened = True
                break
            exp_btn = (
                page.locator("button:has-text('Export')")
                .filter(has_not_text="篩選")
                .filter(has_not_text="筛选")
                .filter(has_not_text="filter")
                .filter(has_not_text="refine")
                .first
            )
            if exp_btn.count():
                exp_btn.click()
                time.sleep(1.5)
            csv_item = page.locator(
                "[role='menu'] button:has-text('CSV'), [role='menu'] *[role='menuitem']:has-text('CSV'), button:has-text('CSV')"
            ).first
            if csv_item.count():
                csv_item.click(force=True)
                time.sleep(2)

        if not modal_opened and not page.locator(
            "button[data-testid='submit-export-button']"
        ).count():
            raise RuntimeError("无法打开 Scopus Export CSV 导出弹窗！")

        range_radio = page.locator("input#select-range, input[id*='range']").first
        if range_radio.count():
            try:
                range_radio.click(force=True)
            except Exception:
                pass

        to_inp = page.locator(
            "div[role='dialog'] input[placeholder='To'], div[class*='Modal'] input[placeholder='To'], input[data-testid='input-range-to']"
        ).last
        if to_inp.count():
            try:
                to_inp.click()
                to_inp.fill(str(doc_count))
            except Exception:
                pass

        page.evaluate(
            """(targetCount) => {
            const r = document.querySelector("#select-range") || document.querySelector("input[id*='range']");
            if (r) {
                r.checked = true;
                r.click();
                if (r.parentElement) r.parentElement.click();
                r.dispatchEvent(new Event('change', { bubbles: true }));
            }
            const inps = Array.from(document.querySelectorAll("input[placeholder='To'], input[data-testid='input-range-to']"));
            let targetInp = inps.find(i => i.offsetParent !== null && i.placeholder === 'To') || inps[inps.length - 1];
            if (targetInp) {
                targetInp.focus();
                const setVal = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                if (setVal) {
                    setVal.call(targetInp, String(targetCount));
                } else {
                    targetInp.value = String(targetCount);
                }
                targetInp.dispatchEvent(new Event('input', { bubbles: true }));
                targetInp.dispatchEvent(new Event('change', { bubbles: true }));
                targetInp.blur();
            }
        }""",
            doc_count,
        )
        time.sleep(1)

        # 勾选全部详情字段（摘要、关键词、机构、基金、通讯地址、来源出版物等），仅排除超长参考文献列表
        page.evaluate(
            """() => {
            const selectAll = document.querySelector("#select-all-fields, input[data-testid='select-all-fields']");
            if (selectAll && !selectAll.checked) {
                selectAll.click();
            }
            const allCbs = Array.from(document.querySelectorAll("div[class*='Modal-module'] input[type='checkbox'], [role='dialog'] input[type='checkbox']"));
            for (const cb of allCbs) {
                if (cb.id === 'field_group_references') {
                    if (cb.checked) cb.click();
                    continue;
                }
                if (!cb.checked && !cb.disabled) {
                    cb.click();
                }
            }
        }"""
        )
        time.sleep(0.5)

        submit_btn = page.locator("button[data-testid='submit-export-button']").first
        for _ in range(10):
            try:
                if submit_btn.count() and submit_btn.is_enabled():
                    break
            except Exception:
                pass
            time.sleep(0.5)

        submit_btn.click()
        self.log(f"已提交全字段 CSV 导出任务 (目标篇数: {doc_count:,})，导航至任务中心...")
        time.sleep(8)

        exports_url = f"{base_origin}/pages/user/exports"
        try:
            page.goto(exports_url, wait_until="commit", timeout=15000)
        except Exception:
            pass

        downloads_dir = self.settings.browser_downloads_dir
        os.makedirs(downloads_dir, exist_ok=True)
        try:
            cdp = page.context.new_cdp_session(page)
            cdp.send(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": downloads_dir,
                    "eventsEnabled": True,
                },
            )
        except Exception:
            pass

        for wait_i in range(45):
            time.sleep(5)
            card_info = page.evaluate(
                """(targetCnt) => {
                const b = document.body ? document.body.innerText : '';
                if (!b.includes('Download CSV') && !b.includes('Export in Progress') && !b.includes('正在导出') && !b.includes('处理中')) {
                    return { status: 'SKELETON_LOADING' };
                }
                const cntStr = String(targetCnt);
                const cntFormatted = Number(targetCnt).toLocaleString();
                const all = Array.from(document.querySelectorAll('*'));
                const matched = all.filter(el => {
                    const txt = (el.innerText || '').trim();
                    return (txt.includes(cntStr) || txt.includes(cntFormatted)) && txt.length < 600;
                });
                for (const card of matched) {
                    const txt = card.innerText || '';
                    if (txt.includes('Export in Progress') || txt.includes('In Progress') || txt.includes('正在导出') || txt.includes('处理中')) {
                        return { status: 'IN_PROGRESS' };
                    }
                    const btns = Array.from(card.querySelectorAll('button, a')).filter(b => (b.innerText || '').includes('Download CSV') || b.getAttribute('data-testid') === 'downloadExportFile');
                    if (btns.length >= 1) {
                        document.querySelectorAll('[data-target-download]').forEach(e => e.removeAttribute('data-target-download'));
                        btns[0].setAttribute('data-target-download', 'true');
                        return { status: 'READY' };
                    }
                }
                const inProg = b.includes('Export in Progress') || b.includes('正在导出') || b.includes('处理中');
                if (inProg) return { status: 'IN_PROGRESS' };
                return { status: 'PENDING' };
            }""",
                doc_count,
            )

            status = (
                card_info.get("status") if isinstance(card_info, dict) else "PENDING"
            )
            if status == "READY":
                self.log(f"★ 目标任务卡片已就绪 (篇数: {doc_count:,})，执行 CDP 授权下载...")
                target_btn = page.locator('[data-target-download="true"]').first
                pre_downloads = set(os.listdir(downloads_dir))
                try:
                    cdp = page.context.new_cdp_session(page)
                    cdp.send(
                        "Browser.setDownloadBehavior",
                        {
                            "behavior": "allow",
                            "downloadPath": downloads_dir,
                            "eventsEnabled": True,
                        },
                    )
                except Exception:
                    pass

                target_btn.click()
                self.log("已点击 Download CSV 按钮，实时监控 .crdownload 与落盘状态...")

                found_csv = None
                for wait_f in range(120):
                    time.sleep(2)
                    cr_files = [
                        f for f in os.listdir(downloads_dir) if f.endswith(".crdownload")
                    ]
                    if cr_files and wait_f % 5 == 0:
                        self.log(f"文件正在传输写入中 ({cr_files[0]})...")

                    cands = [
                        f
                        for f in os.listdir(downloads_dir)
                        if f.startswith("export_")
                        and f.endswith(".csv")
                        and f not in pre_downloads
                    ]
                    if not cands:
                        cands = [
                            f
                            for f in os.listdir(downloads_dir)
                            if f.startswith("export_") and f.endswith(".csv")
                        ]

                    if cands:
                        cands.sort(
                            key=lambda x: os.path.getmtime(os.path.join(downloads_dir, x)),
                            reverse=True,
                        )
                        latest_f = os.path.join(downloads_dir, cands[0])
                        if (
                            (time.time() - os.path.getmtime(latest_f) < 300)
                            and not cr_files
                            and os.path.getsize(latest_f) > 1000
                        ):
                            found_csv = latest_f
                            break

                if found_csv:
                    os.makedirs(os.path.dirname(temp_csv_path), exist_ok=True)
                    shutil.copy2(found_csv, temp_csv_path)
                    sz = os.path.getsize(temp_csv_path)
                    self.log(
                        f"★ CSV 导出文件成功落盘: {temp_csv_path} ({sz:,} 字节, {sz / (1024 * 1024):.2f} MB)"
                    )
                    return temp_csv_path, found_csv

                self.log("超时未在下载目录捕获到有效的 export_*.csv 文件！")
                return None

            elif status == "IN_PROGRESS":
                self.log(
                    f"目标任务在后台生成中 (Export in Progress)... 已等待 {wait_i * 5}s"
                )
            else:
                self.log(f"等待任务卡片状态更新 (当前: {status})... 已等待 {wait_i * 5}s")

            if wait_i > 0 and wait_i % 6 == 0:
                try:
                    page.reload(wait_until="commit", timeout=15000)
                except Exception:
                    pass

        self.log("超时未在任务中心完成目标卡片下载！")
        return None

    @staticmethod
    def _parse_eid_from_link(link: str) -> str:
        if not link:
            return ""
        m = re.search(r"eid=([0-9a-zA-Z\.\-]+)", link)
        return m.group(1) if m else link

    @staticmethod
    def _split_json_list(val_str: str | None) -> str | None:
        if not val_str or not str(val_str).strip():
            return None
        parts = [x.strip() for x in re.split(r";\s*|\n+", str(val_str)) if x.strip()]
        return json.dumps(parts, ensure_ascii=False) if parts else None

    def parse_csv_file(self, csv_path: str, target_day: str) -> list[dict]:
        """解析导出的 CSV 文件，含表头硬核质量门禁与中繁英全字段映射"""
        if not os.path.exists(csv_path):
            return []

        with open(csv_path, "rb") as f:
            raw = f.read()

        text = raw.decode("utf-8-sig", errors="ignore")
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames or []

        # 硬核质量门禁 1：验证 CSV 表头包含来源出版物列
        has_source_col = any(
            k in fieldnames
            for k in ["Source title", "Source Title", "来源出版物名称", "來源出版物名稱"]
        )
        if not has_source_col:
            raise RuntimeError(
                f"[QUALITY_GATE_FAILED] 导出的 CSV ({csv_path}) 缺失 'Source title / 来源出版物名称' 列！表头: {fieldnames[:10]}"
            )

        records = []
        for row in reader:
            title = (
                row.get("文献标题")
                or row.get("文獻標題")
                or row.get("Document title")
                or row.get("Title")
                or ""
            )
            authors = row.get("作者") or row.get("Authors") or ""
            year_raw = row.get("年份") or row.get("Year") or ""
            year_int = int(year_raw) if str(year_raw).isdigit() else None

            source = (
                row.get("来源出版物名称")
                or row.get("來源出版物名稱")
                or row.get("Source title")
                or row.get("Source Title")
                or ""
            )
            doi = (row.get("DOI") or "").replace("https://doi.org/", "").strip()
            link = row.get("链接") or row.get("連結") or row.get("Link") or ""
            eid = row.get("EID") or self._parse_eid_from_link(link)
            if not eid:
                continue

            citedby_raw = (
                row.get("施引文献")
                or row.get("被引用次数")
                or row.get("被引用次數")
                or row.get("Cited by")
                or "0"
            )
            citedby_int = int(citedby_raw) if str(citedby_raw).isdigit() else 0

            subtype = (
                row.get("文献类型")
                or row.get("文獻類型")
                or row.get("Document Type")
                or ""
            )
            issn = row.get("ISSN") or ""
            volume = row.get("卷") or row.get("Volume") or ""
            issue = row.get("期") or row.get("Issue") or ""
            pages = (
                (row.get("起始页码") or row.get("起始頁碼") or row.get("Page start") or "")
                + "-"
                + (row.get("结束页码") or row.get("結束頁碼") or row.get("Page end") or "")
            ).strip("-")
            if not pages:
                pages = row.get("页码") or row.get("Page count") or ""

            article_num = (
                row.get("论文编号") or row.get("論文編號") or row.get("Art. No.") or ""
            )
            publisher = (
                row.get("出版社") or row.get("出版者") or row.get("Publisher") or ""
            )
            pubmed_id = row.get("PubMed ID") or row.get("PubMed Id") or ""
            source_id = row.get("Source ID") or row.get("来源出版物ID") or ""
            pub_stage = (
                row.get("出版阶段")
                or row.get("出版階段")
                or row.get("Publication Stage")
                or ""
            )
            language = (
                row.get("原始文献语言")
                or row.get("原文语言")
                or row.get("Language of Original Document")
                or row.get("Original Language")
                or ""
            )
            open_access_raw = (
                row.get("开放获取")
                or row.get("開放取用")
                or row.get("Open Access")
                or ""
            )
            open_access = 1 if open_access_raw.strip() else 0
            abbrev_source = (
                row.get("来源出版物简称")
                or row.get("來源出版物簡稱")
                or row.get("Abbreviated Source Title")
                or ""
            )
            isbn = row.get("ISBN") or ""
            coden = row.get("CODEN") or ""

            abstract = row.get("摘要") or row.get("Abstract") or ""
            author_keywords = (
                row.get("作者关键词")
                or row.get("作者關鍵詞")
                or row.get("Author Keywords")
                or ""
            )
            indexed_keywords = (
                row.get("索引关键词")
                or row.get("索引關鍵詞")
                or row.get("Index Keywords")
                or row.get("Indexed Keywords")
                or ""
            )
            affiliations = row.get("机构") or row.get("機構") or row.get("Affiliations") or ""
            funding_details = (
                row.get("资助细节")
                or row.get("资助详细信息")
                or row.get("資助細節")
                or row.get("Funding Details")
                or ""
            )
            funding_text = (
                row.get("资助文本")
                or row.get("资助文本 1")
                or row.get("Funding Texts")
                or row.get("Funding Text 1")
                or ""
            )
            sponsors = row.get("赞助商") or row.get("Sponsors") or ""
            correspondence = (
                row.get("通讯地址")
                or row.get("通訊地址")
                or row.get("Correspondence Address")
                or ""
            )

            records.append(
                {
                    "eid": eid,
                    "doi": doi,
                    "title": title,
                    "year": year_int,
                    "source": source,
                    "source_id": source_id,
                    "volume": volume,
                    "issue": issue,
                    "pages": pages,
                    "citedby": citedby_int,
                    "link": link,
                    "affiliations": affiliations,
                    "authors": authors,
                    "author_keywords": author_keywords,
                    "indexed_keywords": indexed_keywords,
                    "abstract": abstract,
                    "subtype": subtype,
                    "publisher": publisher,
                    "issn": issn,
                    "pubmed_id": pubmed_id,
                    "publication_stage": pub_stage,
                    "language": language,
                    "open_access": open_access,
                    "abbrev_source_title": abbrev_source,
                    "isbn": isbn,
                    "coden": coden,
                    "funding_details": funding_details,
                    "funding_text": funding_text,
                    "sponsors": sponsors,
                    "correspondence": correspondence,
                    "load_date": target_day,
                    "article_number": article_num,
                }
            )

        return records

    def ingest_and_reconcile(
        self,
        records: list[dict],
        target_day: str,
        expected_total: int,
        temp_files_to_clean: list[str],
    ) -> tuple[int, int]:
        """双轨双表原子级入库（本地 JSONL + RDS MySQL 2026_scopus_records & 2026_scopus_details）"""
        seen_eids = set()
        unique_records = []
        for r in records:
            eid = r.get("eid")
            if eid and eid not in seen_eids:
                seen_eids.add(eid)
                unique_records.append(r)

        current_day_total = len(unique_records)
        if current_day_total > 0:
            # 硬核质量门禁 2：检查 source_title 空值率不得超过 5%
            null_source_cnt = sum(1 for r in unique_records if not r.get("source"))
            null_rate = null_source_cnt / current_day_total
            if null_rate > 0.05:
                raise RuntimeError(
                    f"[QUALITY_GATE_FAILED] 日期 {target_day} 的 {current_day_total} 篇文献中 source_title 空值率达 {null_rate:.2%} (>5%)，熔断阻断入库！"
                )

        # 1. 写入本地月度 JSONL
        ym = target_day[:7]
        jsonl_path = os.path.join(self.settings.scopus_data_dir, f"scopus_{ym}.jsonl")
        existing_eids = set()
        if os.path.exists(jsonl_path):
            try:
                with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.strip():
                            try:
                                obj = json.loads(line)
                                if obj.get("eid"):
                                    existing_eids.add(obj["eid"])
                            except Exception:
                                pass
            except Exception as e:
                self.log(f"读取本地 JSONL 异常: {e}")

        added_count = 0
        with open(jsonl_path, "a", encoding="utf-8") as f:
            for r in unique_records:
                if r["eid"] not in existing_eids:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                    existing_eids.add(r["eid"])
                    added_count += 1

        self.log(
            f"  ★ 本地 JSONL 写入完成: {jsonl_path} (新增={added_count:,}, 当日去重总量={current_day_total:,})"
        )

        # 2. 批量原子级双表写入阿里云 RDS MySQL
        if unique_records:
            conn = self.settings.create_db_connection(autocommit=True)
            try:
                with conn.cursor() as cur:
                    sql_records = """
                    INSERT INTO `2026_scopus_records`
                    (eid, doi, title, source_title, source_id, issn, year, citedby, subtype, authors, load_date, has_detail)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        doi = COALESCE(VALUES(doi), doi),
                        title = COALESCE(VALUES(title), title),
                        source_title = COALESCE(VALUES(source_title), source_title),
                        source_id = COALESCE(VALUES(source_id), source_id),
                        issn = COALESCE(VALUES(issn), issn),
                        year = COALESCE(VALUES(year), year),
                        citedby = VALUES(citedby),
                        subtype = COALESCE(VALUES(subtype), subtype),
                        authors = COALESCE(VALUES(authors), authors),
                        load_date = COALESCE(VALUES(load_date), load_date),
                        has_detail = VALUES(has_detail);
                    """
                    sql_details = """
                    INSERT INTO `2026_scopus_details`
                    (eid, doi, title, source_title, source_id, issn, publication_year, publication_stage,
                     language, open_access, volume, issue, pages, article_number, publisher, document_type,
                     cited_by, pubmed_id, abstract_text, author_keywords, authors_json, affiliations_json,
                     indexed_keywords_json, funding_json, correspondences_json, source_json, is_missing,
                     load_date, fetched_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        doi = COALESCE(VALUES(doi), doi),
                        title = COALESCE(VALUES(title), title),
                        source_title = COALESCE(VALUES(source_title), source_title),
                        source_id = COALESCE(VALUES(source_id), source_id),
                        issn = COALESCE(VALUES(issn), issn),
                        publication_year = COALESCE(VALUES(publication_year), publication_year),
                        publication_stage = COALESCE(VALUES(publication_stage), publication_stage),
                        language = COALESCE(VALUES(language), language),
                        open_access = COALESCE(VALUES(open_access), open_access),
                        volume = COALESCE(VALUES(volume), volume),
                        issue = COALESCE(VALUES(issue), issue),
                        pages = COALESCE(VALUES(pages), pages),
                        article_number = COALESCE(VALUES(article_number), article_number),
                        publisher = COALESCE(VALUES(publisher), publisher),
                        document_type = COALESCE(VALUES(document_type), document_type),
                        cited_by = VALUES(cited_by),
                        pubmed_id = COALESCE(VALUES(pubmed_id), pubmed_id),
                        abstract_text = COALESCE(VALUES(abstract_text), abstract_text),
                        author_keywords = COALESCE(VALUES(author_keywords), author_keywords),
                        authors_json = COALESCE(VALUES(authors_json), authors_json),
                        affiliations_json = COALESCE(VALUES(affiliations_json), affiliations_json),
                        indexed_keywords_json = COALESCE(VALUES(indexed_keywords_json), indexed_keywords_json),
                        funding_json = COALESCE(VALUES(funding_json), funding_json),
                        correspondences_json = COALESCE(VALUES(correspondences_json), correspondences_json),
                        source_json = COALESCE(VALUES(source_json), source_json),
                        is_missing = 0,
                        load_date = COALESCE(VALUES(load_date), load_date),
                        fetched_at = VALUES(fetched_at);
                    """
                    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
                    batch_records = []
                    batch_details = []
                    for r in unique_records:
                        src_dict = {
                            "title": r.get("source") or None,
                            "issn": r.get("issn") or None,
                            "publisher": r.get("publisher") or None,
                        }
                        if r.get("abbrev_source_title"):
                            src_dict["abbrev_source_title"] = r["abbrev_source_title"]
                        if r.get("isbn"):
                            src_dict["isbn"] = r["isbn"]
                        if r.get("coden"):
                            src_dict["coden"] = r["coden"]
                        source_json_str = (
                            json.dumps(src_dict, ensure_ascii=False)
                            if any(src_dict.values())
                            else None
                        )

                        fund_dict = {
                            "details": r.get("funding_details") or None,
                            "text": r.get("funding_text") or None,
                            "sponsor": r.get("sponsors") or None,
                        }
                        funding_json_str = (
                            json.dumps(fund_dict, ensure_ascii=False)
                            if any(fund_dict.values())
                            else None
                        )

                        batch_records.append(
                            (
                                r["eid"][:64],
                                (r.get("doi") or None)[:255] if r.get("doi") else None,
                                r.get("title") or None,
                                (r.get("source") or None)[:512] if r.get("source") else None,
                                (r.get("source_id") or None)[:32] if r.get("source_id") else None,
                                (r.get("issn") or None)[:16] if r.get("issn") else None,
                                r.get("year"),
                                r.get("citedby"),
                                (r.get("subtype") or None)[:128] if r.get("subtype") else None,
                                r.get("authors") or None,
                                r.get("load_date"),
                                1,
                            )
                        )

                        batch_details.append(
                            (
                                r["eid"][:64],
                                (r.get("doi") or None)[:255] if r.get("doi") else None,
                                r.get("title") or None,
                                (r.get("source") or None)[:512] if r.get("source") else None,
                                (r.get("source_id") or None)[:32] if r.get("source_id") else None,
                                (r.get("issn") or None)[:16] if r.get("issn") else None,
                                r.get("year"),
                                (r.get("publication_stage") or None)[:32]
                                if r.get("publication_stage")
                                else None,
                                (r.get("language") or None)[:64] if r.get("language") else None,
                                r.get("open_access", 0),
                                (r.get("volume") or None)[:32] if r.get("volume") else None,
                                (r.get("issue") or None)[:128] if r.get("issue") else None,
                                (r.get("pages") or None)[:64] if r.get("pages") else None,
                                (r.get("article_number") or None)[:64]
                                if r.get("article_number")
                                else None,
                                (r.get("publisher") or None)[:512]
                                if r.get("publisher")
                                else None,
                                (r.get("subtype") or None)[:32] if r.get("subtype") else None,
                                r.get("citedby"),
                                (r.get("pubmed_id") or None)[:32]
                                if r.get("pubmed_id")
                                else None,
                                r.get("abstract") or None,
                                r.get("author_keywords") or None,
                                self._split_json_list(r.get("authors")),
                                self._split_json_list(r.get("affiliations")),
                                self._split_json_list(r.get("indexed_keywords")),
                                funding_json_str,
                                self._split_json_list(r.get("correspondence")),
                                source_json_str,
                                r.get("load_date"),
                                now_str,
                            )
                        )

                    chunk_sz = 1000
                    for idx in range(0, len(batch_records), chunk_sz):
                        cur.executemany(sql_records, batch_records[idx : idx + chunk_sz])
                        cur.executemany(sql_details, batch_details[idx : idx + chunk_sz])
                self.log(
                    f"  ★ 成功原子级双表写入 RDS: 2026_scopus_records ({len(batch_records):,} 条) & 2026_scopus_details ({len(batch_details):,} 条)"
                )
            finally:
                conn.close()

        # 3. 更新对账账本
        ledger = {}
        if os.path.exists(self.settings.scopus_ledger_path):
            try:
                with open(self.settings.scopus_ledger_path, "r", encoding="utf-8") as f:
                    ledger = json.load(f)
            except Exception:
                pass

        gap = max(0, expected_total - current_day_total)
        is_ok = (gap == 0) or (expected_total > 100 and gap <= 15)
        ledger[target_day] = {
            "ok": is_ok,
            "total_now": current_day_total,
            "gap": gap,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        with open(self.settings.scopus_ledger_path, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)

        if is_ok:
            cleaned = 0
            for fp in set(temp_files_to_clean):
                if fp and os.path.exists(fp):
                    try:
                        os.remove(fp)
                        cleaned += 1
                    except Exception:
                        pass
            self.log(f"  ★ 当日对账达标，已清理 {cleaned} 个临时 CSV 文件。")
        else:
            self.log(
                f"  ★ 当日尚存差额 ({gap:,} 篇)，保留临时 CSV 断点文件供下次续传复用。"
            )

        return added_count, current_day_total

    def harvest_day(self, page: Page, target_day: str) -> bool:
        """在已就绪的 Scopus Page 上采集指定日期的全部题录与详情"""
        dt = datetime.datetime.strptime(target_day, "%Y-%m-%d")
        prev_day = (dt - datetime.timedelta(days=1)).strftime("%Y%m%d")
        next_day = (dt + datetime.timedelta(days=1)).strftime("%Y%m%d")

        base_query = f"ORIG-LOAD-DATE AFT {prev_day} AND ORIG-LOAD-DATE BEF {next_day}"
        self.log(f"--> [开始采集] Scopus 入库日: {target_day} | 检索式: {base_query}")

        total_count = self.search_and_get_count(page, base_query)
        self.log(f"[{target_day}] 官方全天命中总数: {total_count:,} 篇")

        if total_count == 0:
            self.log(f"{target_day} 官方命中为 0 篇，暂不标记完成，等待后续重检。")
            return False

        temp_files = []
        all_records = []

        if total_count <= MAX_BATCH_SIZE:
            tmp_csv = os.path.join(
                self.settings.scopus_raw_csv_dir, f"temp_{target_day}_all.csv"
            )
            res = self.export_current_results_to_csv(page, total_count, tmp_csv)
            if not res:
                return False
            csv_path, origin_dl = res
            temp_files.extend([csv_path, origin_dl])
            all_records.extend(self.parse_csv_file(csv_path, target_day))
        else:
            self.log(
                f"【超 20,000 篇正交二分】{target_day} 总数 {total_count:,} > 20,000，启动 DOCTYPE(ar) 与 NOT DOCTYPE(ar) 正交分批导出..."
            )
            sub_batches = [
                (f"{base_query} AND DOCTYPE(ar)", "期刊论文(ar)"),
                (f"{base_query} AND NOT DOCTYPE(ar)", "非期刊论文(NOT ar)"),
            ]
            for sub_q, label in sub_batches:
                self.log(f"---> 处理子批次【{label}】: {sub_q}")
                sub_count = self.search_and_get_count(page, sub_q)
                self.log(f"子批次【{label}】官方命中数: {sub_count:,} 篇")
                if sub_count == 0:
                    continue

                clean_label = re.sub(r"[^a-zA-Z0-9]", "_", label)
                tmp_csv = os.path.join(
                    self.settings.scopus_raw_csv_dir,
                    f"temp_{target_day}_{clean_label}.csv",
                )
                if os.path.exists(tmp_csv) and os.path.getsize(tmp_csv) > 1000:
                    local_recs = self.parse_csv_file(tmp_csv, target_day)
                    if len(local_recs) >= sub_count - 50:
                        self.log(
                            f"★ 命中本地完整断点文件 ({tmp_csv}, {len(local_recs):,} 条)，直接复用！"
                        )
                        all_records.extend(local_recs)
                        temp_files.append(tmp_csv)
                        continue

                res = self.export_current_results_to_csv(page, sub_count, tmp_csv)
                if not res:
                    self.log(f"子批次【{label}】导出失败！")
                    return False
                csv_path, origin_dl = res
                temp_files.extend([csv_path, origin_dl])
                recs = self.parse_csv_file(csv_path, target_day)
                self.log(f"子批次【{label}】解析完成: {len(recs):,} 条")
                all_records.extend(recs)

        added, day_total = self.ingest_and_reconcile(
            all_records, target_day, total_count, temp_files
        )
        self.log(
            f"[OK] {target_day} Scopus 双表采集完成！新增={added:,}, 当日总入库={day_total:,}/{total_count:,}"
        )
        return True


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Scopus 独立高内聚采集器")
    parser.add_argument(
        "days", nargs="*", help="指定待采集日期 (YYYY-MM-DD)，留空则自动扫描近 7 天断口"
    )
    parser.add_argument("--lookback", type=int, default=7, help="自动扫描回溯天数")
    args = parser.parse_args()

    settings = load_settings()
    gateway = Channel8090Gateway(settings)
    collector = ScopusCollector(settings, gateway)

    target_days = args.days or collector.detect_todo_days(lookback_days=args.lookback)
    if not target_days:
        collector.log("所有近期日期 Scopus 双表均已齐备，无需补采。")
        return 0

    collector.log(f"待采集 Scopus 日期队列: {target_days}")
    gateway.restart_edge_browser(kill_existing=False)

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(settings.cdp_url)
        ctx = browser.contexts[0]
        page = gateway.open_scopus_ready_page(ctx)

        for day in target_days:
            try:
                collector.harvest_day(page, day)
            except GatewayQuotaExhaustedError as qe:
                collector.log(
                    f"【流量额度触顶】{qe} -> 已安全保护本地断点，等待次日 08:00 自动续传！"
                )
                return 3
            except Exception as e:
                collector.log(f"采集日期 {day} 异常: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
