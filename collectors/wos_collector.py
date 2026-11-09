# -*- coding: utf-8 -*-
"""
Web of Science (WoS) 题录与全字段高内聚采集器 (collectors/wos_collector.py)
================================================================================
架构设计（与网页打开/登录层 100% 解耦）：
1. 接收由 gateway.channel_8090 打开并完成鉴权的就绪 (wos_page, adv_url)；
2. 执行 LD=YYYY-MM-DD/YYYY-MM-DD 入库日检索；
3. 按 1,000 篇切片导出 Full Record Excel (.xls)，支持本地有效切片断点续传秒级复用；
4. 解析写入月度 JSONL 并调用 collectors.wos_to_mysql 批量同步至阿里云 RDS 2026_wos_records。
"""

import os
import re
import sys
import json
import time
import datetime
from playwright.sync_api import Page, sync_playwright

from config.settings import AppSettings, load_settings
from gateway.channel_8090 import (
    Channel8090Gateway,
    GatewayQuotaExhaustedError,
)
from collectors.wos_xls import ingest_xls_to_jsonl
from collectors.wos_to_mysql import sync_jsonl_to_mysql

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BATCH_SIZE = 1000


class WosCollector:
    """Web of Science 高内聚采集器"""

    def __init__(self, settings: AppSettings, gateway: Channel8090Gateway):
        self.settings = settings
        self.gateway = gateway
        os.makedirs(self.settings.wos_data_dir, exist_ok=True)
        os.makedirs(self.settings.wos_raw_xls_dir, exist_ok=True)
        os.makedirs(self.settings.log_dir, exist_ok=True)
        self.log_file = os.path.join(self.settings.log_dir, "wos_collector.log")

    def log(self, *args) -> None:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"{ts} [WoS-Collector] " + " ".join(str(a) for a in args)
        print(msg, flush=True)
        try:
            with open(self.log_file, "a", encoding="utf-8", errors="replace") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    def load_ledger(self) -> dict:
        if os.path.exists(self.settings.wos_ledger_path):
            try:
                with open(self.settings.wos_ledger_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                self.log(f"读取 WoS 账本异常: {e}")
        return {}

    def update_ledger(
        self,
        day: str,
        remote_total: int,
        rows_count: int,
        added_count: int,
        complete: bool,
        note: str = "",
    ) -> None:
        ledger = self.load_ledger()
        ledger[day] = {
            "remote": int(remote_total),
            "rows": int(rows_count),
            "added": int(added_count),
            "complete": bool(complete),
            "note": note,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(self.settings.wos_ledger_path, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)

    def detect_todo_days(self, lookback_days: int = 7) -> list[str]:
        """动态扫描最近 N 天内尚未完成 WoS 采集对账的日期"""
        ledger = self.load_ledger()
        today = datetime.date.today()
        min_d = today - datetime.timedelta(days=lookback_days)
        todo = []
        cur = min_d
        while cur <= today:
            d_str = cur.isoformat()
            rec = ledger.get(d_str) or {}
            if not rec.get("complete"):
                todo.append(d_str)
            cur += datetime.timedelta(days=1)
        return todo

    def harvest_day(self, page: Page, adv_url: str, day: str) -> bool:
        """在已就绪的 WoS Page 上收割指定入库日 (LD=day/day) 的全部记录"""
        self.log(f"--> [开始采集] WoS 入库日: {day}")

        if "advanced-search" not in (page.url or ""):
            self.log(f"返回 WoS 高级检索页: {adv_url}")
            page.goto(adv_url, wait_until="domcontentloaded", timeout=45000)
            time.sleep(3)

        self.gateway.dismiss_cookie_overlays(page)
        self.gateway.check_quota_exhausted(
            page, f"WoS(id={self.settings.wos_entry_id})"
        )

        query = f"LD={day}/{day}"
        self.log(f"提交检索式: {query}")

        ta = page.locator("#advancedSearchInputArea")
        ta.fill("")
        time.sleep(0.3)
        ta.fill(query)
        time.sleep(0.8)

        search_btn = page.locator("button[cdxanalyticscategory='wos-search-search-adv']")
        search_btn.click(force=True)

        total = None
        for _ in range(25):
            time.sleep(2)
            self.gateway.dismiss_cookie_overlays(page)
            self.gateway.check_quota_exhausted(
                page, f"WoS(id={self.settings.wos_entry_id})"
            )
            u = page.url or ""
            t = page.title() or ""

            if "summary" in u:
                m_t = re.search(r"–\s*([0-9,]+)\s*–", t)
                if m_t:
                    total = int(m_t.group(1).replace(",", ""))
                    break
                txt = page.evaluate(
                    """() => {
                    const el = document.querySelector('.tab-results-count') 
                            || document.querySelector('.mat-headline-6')
                            || document.querySelector('.results-count');
                    return el ? el.innerText.trim() : null;
                }"""
                )
                if txt:
                    total = int(re.sub(r"[^0-9]", "", txt))
                    break

            check_no = page.evaluate(
                """(q) => {
                const body = document.body ? document.body.innerText : '';
                if (body.includes('Your search found no results')) return true;
                const rows = Array.from(document.querySelectorAll('tr, [role=row], .search-history-row, div'));
                for (const r of rows) {
                    const txt = (r.innerText || '').trim();
                    if (txt.includes(q) && (txt.includes('\\n0\\n') || txt.includes(' 0 '))) {
                        return true;
                    }
                }
                return false;
            }""",
                query,
            )
            if check_no:
                total = 0
                break

            sq = page.evaluate(
                """(q) => {
                const links = Array.from(document.querySelectorAll('a')).filter(a => a.href && a.href.includes('/summary/'));
                for (const l of links) {
                    const row = l.closest('tr, [role=row]') || l.parentElement;
                    if (row && (row.innerText || '').includes(q)) {
                        return { count: (l.innerText||'').trim(), href: l.href };
                    }
                }
                return null;
            }""",
                query,
            )
            if sq and sq.get("href"):
                cnt_clean = re.sub(r"[^0-9]", "", sq.get("count", "0"))
                if cnt_clean:
                    total = int(cnt_clean)
                    page.goto(sq["href"], wait_until="domcontentloaded", timeout=45000)
                    time.sleep(4)
                    break

        if total is None:
            self.log(f"超时未能获取 {day} 的 WoS 检索结果 (URL: {page.url[:90]})")
            return False

        if total == 0:
            self.log(f"{day} 检索命中为 0 篇，暂不标记完成，留待下轮重检。")
            self.update_ledger(day, 0, 0, 0, False, "pending-zero")
            return False

        self.log(f"[{day}] WoS 官方命中总数: {total:,} 篇")
        out_jsonl = os.path.join(self.settings.wos_data_dir, f"wos_{day[:7]}.jsonl")
        n_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
        total_added = 0
        total_rows = 0

        for k in range(1, n_batches + 1):
            a = (k - 1) * BATCH_SIZE + 1
            b = min(k * BATCH_SIZE, total)
            raw_xls = os.path.join(self.settings.wos_raw_xls_dir, f"wos_{day}_{k}.xls")

            # 1. 本地千篇切片断点续传复用
            if os.path.exists(raw_xls) and os.path.getsize(raw_xls) > 100000:
                try:
                    added, nrows = ingest_xls_to_jsonl(raw_xls, out_jsonl)
                    total_added += added
                    total_rows += nrows
                    self.log(
                        f"  [断点复用 {k}/{n_batches}] {raw_xls} -> rows={nrows}, added={added}"
                    )
                    continue
                except Exception as e:
                    self.log(f"  本地切片解析失败，重新下载: {e}")

            self.log(f"--- [批次 {k}/{n_batches}] 导出范围 {a:,} ~ {b:,} ---")
            exported = False
            for attempt in range(3):
                try:
                    self.gateway.dismiss_cookie_overlays(page)
                    self.gateway.check_quota_exhausted(
                        page, f"WoS(id={self.settings.wos_entry_id})"
                    )
                    has_dialog = page.evaluate(
                        """() => {
                        const el = document.querySelector('.window-header.export-overlay') 
                                || document.querySelector('app-export-dialog');
                        return !!el;
                    }"""
                    )
                    if not has_dialog:
                        page.evaluate(
                            """() => {
                            const b = document.querySelector('#export-trigger-btn') 
                                   || Array.from(document.querySelectorAll('button')).find(e => (e.innerText||'').trim().startsWith('Export'));
                            if (b) b.click();
                        }"""
                        )
                        time.sleep(2)
                        page.evaluate(
                            """() => {
                            const b = document.querySelector('#exportToExcelButton')
                                   || Array.from(document.querySelectorAll('button, a')).find(e => (e.innerText||'').includes('Excel'));
                            if (b) b.click();
                        }"""
                        )
                        time.sleep(2.5)

                    page.evaluate(
                        """() => {
                        const btns = Array.from(document.querySelectorAll('button, mat-select')).filter(b => b.offsetParent);
                        const cur = btns.find(b => /author|full record|record content/i.test(b.innerText||''));
                        if (cur && !/full record/i.test(cur.innerText||'')) {
                            cur.click();
                        }
                    }"""
                    )
                    time.sleep(1.2)
                    page.evaluate(
                        """() => {
                        const opts = Array.from(document.querySelectorAll('.mat-mdc-menu-panel button, [role=option], mat-option')).filter(e => e.offsetParent);
                        const x = opts.find(e => /full record/i.test((e.innerText||'').trim()));
                        if (x) x.click();
                    }"""
                    )
                    time.sleep(1)

                    fill_res = page.evaluate(
                        """({from_v, to_v}) => {
                        const r = document.querySelector('#radio3-input') || document.querySelector('#radio3');
                        if (r) r.click();
                        const f = document.querySelector("input[name='markFrom']");
                        const t = document.querySelector("input[name='markTo']");
                        if (f) {
                            f.value = String(from_v);
                            f.dispatchEvent(new Event('input', {bubbles:true}));
                            f.dispatchEvent(new Event('change', {bubbles:true}));
                        }
                        if (t) {
                            t.value = String(to_v);
                            t.dispatchEvent(new Event('input', {bubbles:true}));
                            t.dispatchEvent(new Event('change', {bubbles:true}));
                        }
                        return {
                            radioChecked: r ? r.checked : false,
                            fromVal: f ? f.value : null,
                            toVal: t ? t.value : null
                        };
                    }""",
                        {"from_v": a, "to_v": b},
                    )
                    time.sleep(1)

                    if not fill_res.get("radioChecked") or str(
                        fill_res.get("fromVal")
                    ) != str(a):
                        time.sleep(1.5)
                        continue

                    if os.path.exists(raw_xls):
                        try:
                            os.remove(raw_xls)
                        except Exception:
                            pass

                    with page.expect_download(timeout=120000) as dl:
                        page.evaluate(
                            """() => {
                            const b = document.querySelector('#exportButton');
                            if (b) b.click();
                        }"""
                        )
                    d = dl.value
                    d.save_as(raw_xls)
                    sz = os.path.getsize(raw_xls) if os.path.exists(raw_xls) else 0
                    self.log(f"  批次 {k} 下载完成: {raw_xls} ({sz:,} 字节)")
                    exported = True
                    break
                except GatewayQuotaExhaustedError:
                    raise
                except Exception as e:
                    self.log(f"  批次 {k} 导出尝试 {attempt + 1}/3 异常: {e}")
                    time.sleep(4)

            if not exported:
                self.update_ledger(
                    day, total, total_rows, total_added, False, f"fail-b{k}"
                )
                return False

            added, nrows = ingest_xls_to_jsonl(raw_xls, out_jsonl)
            total_added += added
            total_rows += nrows
            self.log(
                f"  [落盘进度] 批次 {k}/{n_batches} 解析成功: rows={nrows}, added={added}, 累计新增={total_added:,}"
            )
            time.sleep(10)

        self.update_ledger(day, total, total_rows, total_added, True, "complete")
        sync_jsonl_to_mysql(out_jsonl)
        self.log(f"[OK] {day} WoS 采集与 RDS 同步全部完成！总计新增={total_added:,}")
        return True


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Web of Science (WoS) 独立高内聚采集器")
    parser.add_argument(
        "days", nargs="*", help="指定待采集日期 (YYYY-MM-DD)，留空则自动扫描近 7 天断口"
    )
    parser.add_argument("--lookback", type=int, default=7, help="自动扫描回溯天数")
    args = parser.parse_args()

    settings = load_settings()
    gateway = Channel8090Gateway(settings)
    collector = WosCollector(settings, gateway)

    target_days = args.days or collector.detect_todo_days(lookback_days=args.lookback)
    if not target_days:
        collector.log("所有近期日期 WoS 均已完成采集，无需补采。")
        return 0

    collector.log(f"待采集 WoS 日期队列: {target_days}")
    gateway.restart_edge_browser(kill_existing=False)

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(settings.cdp_url)
        ctx = browser.contexts[0]
        page, adv_url = gateway.open_wos_ready_page(ctx)

        for day in target_days:
            try:
                collector.harvest_day(page, adv_url, day)
            except GatewayQuotaExhaustedError as qe:
                collector.log(
                    f"【流量额度触顶】{qe} -> 已安全保护本地千篇切片断点，等待次日 08:00 自动续传！"
                )
                return 3
            except Exception as e:
                collector.log(f"采集日期 {day} 异常: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
