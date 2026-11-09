# -*- coding: utf-8 -*-
"""
Scopus & Web of Science (WoS) 每日 08:00 全自动目标级收割总控引擎 (scheduler/daily_runner.py)
================================================================================
核心解耦流程：
1. 阶段 0：调用 HealthDoctor 检查计划任务与 RDS 近期日期缺口；
2. 阶段 1 (Scopus 采集)：
   - 步骤 A (网页链接打开与鉴权)：调用 Channel8090Gateway.open_scopus_ready_page 打开 goto=2350 页面；
   - 步骤 B (高内聚题录采集入库)：调用 ScopusCollector.harvest_day 执行全字段 CSV 导出与双表原子入库；
3. 阶段 2 (WoS 采集)：
   - 步骤 A (网页链接打开与鉴权)：调用 Channel8090Gateway.open_wos_ready_page 打开 id=5157 页面；
   - 步骤 B (高内聚题录采集入库)：调用 WosCollector.harvest_day 执行千篇切片导出与 RDS 入库；
4. 异常定位与断点保护：
   - 若捕获 GatewayAuthExpiredError（登录失败/过期），立即写入醒目告警日志并记录至 health_status.json；
   - 若捕获 GatewayQuotaExhaustedError（当日流量触顶），安全保护本地已下载子批次断点，次日 08:00 自动续传。
"""

import os
import sys
import time
import datetime
from playwright.sync_api import sync_playwright

from config.settings import load_settings
from gateway.channel_8090 import (
    Channel8090Gateway,
    GatewayAuthExpiredError,
    GatewayQuotaExhaustedError,
    GatewayError,
)
from collectors.scopus_collector import ScopusCollector
from collectors.wos_collector import WosCollector
from scheduler.health_doctor import HealthDoctor

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def run_daily_goal(lookback_days: int = 7, target_db: str = "both") -> int:
    settings = load_settings()
    os.makedirs(settings.log_dir, exist_ok=True)
    log_path = os.path.join(settings.log_dir, "daily_runner.log")

    def log(*args):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"{ts} [Daily-Goal-Runner] " + " ".join(str(a) for a in args)
        print(msg, flush=True)
        try:
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    log("=" * 72)
    log("   启动每日 08:00 [8090 专属通道: Scopus + WoS] 全自动解耦采集总控任务")
    log("=" * 72)

    gateway = Channel8090Gateway(settings)
    scopus_collector = ScopusCollector(settings, gateway)
    wos_collector = WosCollector(settings, gateway)
    doctor = HealthDoctor(settings)

    # 确保 Edge 9222 浏览器就绪
    try:
        gateway.restart_edge_browser(kill_existing=False)
    except GatewayError as e:
        log(f"首次连接 Edge 9222 受阻 ({e})，尝试清理残留进程并重启浏览器...")
        gateway.restart_edge_browser(kill_existing=True)

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(settings.cdp_url)
        ctx = browser.contexts[0]

        # ----------------------------------------------------------------------
        # 阶段 1：Scopus (goto=2350) 网页打开 -> 题录与全字段详情双表采集
        # ----------------------------------------------------------------------
        if target_db in ("scopus", "both"):
            scopus_todos = scopus_collector.detect_todo_days(lookback_days=lookback_days)
            log(f"\n>>> [阶段 1/2: Scopus] 待采集/补齐日期队列: {scopus_todos}")
            if scopus_todos:
                try:
                    # 解耦步骤 A：仅由 Gateway 打开并验证 Scopus 网页会话
                    sco_page = gateway.open_scopus_ready_page(ctx)
                    # 解耦步骤 B：仅由 Collector 在已就绪页面上执行检索导出与双表入库
                    for d_str in scopus_todos:
                        try:
                            scopus_collector.harvest_day(sco_page, d_str)
                            time.sleep(15)
                        except GatewayQuotaExhaustedError as qe:
                            log(
                                f"【Scopus 流量额度触顶】{qe} -> 已保护本地 CSV 断点，留待明日 08:00 续传。"
                            )
                            break
                        except Exception as e:
                            log(f"[Scopus 单日异常] 日期 {d_str} 采集出错: {e}")
                except GatewayAuthExpiredError as ae:
                    log(
                        f"【严重告警: 8090 登录失败或过期】Scopus 网关鉴权被拒: {ae}！请立即检查 .env 中的 8090 账号！"
                    )
                    doctor.run_full_diagnosis(probe_gateway=False, auto_fix=False)
                    return 2
                except GatewayQuotaExhaustedError as qe:
                    log(f"【Scopus 流量额度触顶】{qe}")
                except Exception as e:
                    log(f"[Scopus 网关异常] {e}")
            else:
                log(">>> [阶段 1/2: Scopus] 最近 7 天双表数据全部齐备，无需补采。")

        # ----------------------------------------------------------------------
        # 阶段 2：Web of Science (id=5157) 网页打开 -> 千篇切片导出与 RDS 入库
        # ----------------------------------------------------------------------
        if target_db in ("wos", "both"):
            wos_todos = wos_collector.detect_todo_days(lookback_days=lookback_days)
            log(f"\n>>> [阶段 2/2: WoS] 待采集/补齐日期队列: {wos_todos}")
            if wos_todos:
                try:
                    # 解耦步骤 A：仅由 Gateway 打开并验证 WoS 网页会话
                    wos_page, adv_url = gateway.open_wos_ready_page(ctx)
                    # 解耦步骤 B：仅由 Collector 在已就绪页面上执行检索导出与入库
                    for d_str in wos_todos:
                        try:
                            wos_collector.harvest_day(wos_page, adv_url, d_str)
                            time.sleep(10)
                        except GatewayQuotaExhaustedError as qe:
                            log(
                                f"【WoS 流量额度触顶】{qe} -> 已保护本地 .xls 千篇切片断点，留待明日 08:00 续传。"
                            )
                            break
                        except Exception as e:
                            log(f"[WoS 单日异常] 日期 {d_str} 采集出错: {e}")
                except GatewayAuthExpiredError as ae:
                    log(
                        f"【严重告警: 8090 登录失败或过期】WoS 网关鉴权被拒: {ae}！请立即检查 .env 中的 8090 账号！"
                    )
                    doctor.run_full_diagnosis(probe_gateway=False, auto_fix=False)
                    return 2
                except GatewayQuotaExhaustedError as qe:
                    log(f"【WoS 流量额度触顶】{qe}")
                except Exception as e:
                    log(f"[WoS 网关异常] {e}")
            else:
                log(">>> [阶段 2/2: WoS] 最近 7 天数据全部齐备，无需补采。")

    # 阶段 3：输出全链路对账与健康状态报告
    doctor.run_full_diagnosis(probe_gateway=False, auto_fix=False)
    log("=== 本轮每日自动采集任务执行完毕 ===\n")
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Scopus & WoS 每日 08:00 自动目标级总控引擎")
    parser.add_argument("--lookback", type=int, default=7, help="回溯检查天数 (默认 7 天)")
    parser.add_argument(
        "--target",
        choices=["scopus", "wos", "both"],
        default="both",
        help="指定运行的数据库目标",
    )
    args = parser.parse_args()
    sys.exit(run_daily_goal(lookback_days=args.lookback, target_db=args.target))
