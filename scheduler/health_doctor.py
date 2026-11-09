# -*- coding: utf-8 -*-
"""
故障秒级定位与一键自愈诊断器 (scheduler/health_doctor.py)
================================================================================
专用于秒级定位并自动修复以下高频运维故障：
1. 【自动任务没有启动 / 被禁用 / 丢失】：
   - 自动检测 Windows 计划任务 (Daily_8AM_Scopus_WoS_Goal_Harvester) 注册状态、下次运行时间、上次退出码；
   - 支持 --fix 一键自动重建/启用每日 08:00 计划任务。
2. 【8090 登录失败 / 会话过期 / 12395 验证码拦截 / 流量额度触顶】：
   - 精准区分：BROWSER_DOWN (浏览器未启动)、AUTH_EXPIRED_OR_INVALID (8090 账号密码错误或卡密到期)、
     CAPTCHA_BLOCKED (12395 验证码拦截)、QUOTA_EXHAUSTED (当日流量额度已满)；
   - 支持 --fix 自动重启 Edge 9222、自动重新登录 8090 并穿透 12395 验证码恢复会话。
3. 【RDS 数据库近 7 日题录与详情对账】：
   - 直连阿里云 RDS 检查 2026_scopus_records、2026_scopus_details、2026_wos_records 逐日入库条数与缺口。
"""

import os
import sys
import csv
import io
import json
import time
import datetime
import subprocess
from playwright.sync_api import sync_playwright

from config.settings import AppSettings, load_settings
from gateway.channel_8090 import (
    Channel8090Gateway,
    GatewayAuthExpiredError,
    GatewayQuotaExhaustedError,
    GatewayCaptchaBlockedError,
    GatewayMirrorUnreachableError,
    GatewayError,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class HealthDoctor:
    """系统全链路健康检查、故障秒级定位与一键修复器"""

    def __init__(self, settings: AppSettings):
        self.settings = settings
        self.gateway = Channel8090Gateway(settings)
        os.makedirs(self.settings.log_dir, exist_ok=True)
        self.status_json_path = os.path.join(self.settings.log_dir, "health_status.json")

    def check_scheduled_task(self, auto_fix: bool = False) -> dict:
        """检查每日 08:00 自动计划任务是否已注册并处于就绪状态"""
        task_name = self.settings.scheduled_task_name
        cmd = f'schtasks /Query /TN "{task_name}" /FO CSV /V'
        res = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, encoding="gbk", errors="ignore"
        )

        if res.returncode != 0 or not res.stdout.strip():
            info = {
                "status": "TASK_NOT_INSTALLED",
                "task_name": task_name,
                "detail": f"计划任务 '{task_name}' 未注册，每日 08:00 自动任务不会启动！",
            }
            if auto_fix:
                fixed = self.install_or_repair_scheduled_task()
                info["auto_fixed"] = fixed
                if fixed:
                    info["status"] = "FIXED_AND_READY"
                    info["detail"] = f"已自动重新注册每日 08:00 计划任务 '{task_name}'。"
            return info

        reader = csv.DictReader(io.StringIO(res.stdout.strip()))
        row = next(reader, {})
        status_val = (
            row.get("Status") or row.get("状态") or row.get("模式") or "Unknown"
        ).strip()
        next_run = (
            row.get("Next Run Time") or row.get("下次运行时间") or "Unknown"
        ).strip()
        last_run = (
            row.get("Last Run Time") or row.get("上次运行时间") or "Unknown"
        ).strip()
        last_result = (
            row.get("Last Result") or row.get("上次结果") or "Unknown"
        ).strip()

        is_disabled = any(
            k in status_val.lower() for k in ["disabled", "禁用", "已禁用"]
        )
        if is_disabled:
            info = {
                "status": "TASK_DISABLED",
                "task_name": task_name,
                "task_state": status_val,
                "next_run_time": next_run,
                "last_run_time": last_run,
                "last_result": last_result,
                "detail": f"计划任务 '{task_name}' 处于禁用状态！",
            }
            if auto_fix:
                fixed = self.install_or_repair_scheduled_task()
                info["auto_fixed"] = fixed
                if fixed:
                    info["status"] = "FIXED_AND_READY"
            return info

        return {
            "status": "OK",
            "task_name": task_name,
            "task_state": status_val,
            "next_run_time": next_run,
            "last_run_time": last_run,
            "last_result": last_result,
            "detail": f"每日自动任务已就绪 (下次运行: {next_run}, 上次结果: {last_result})",
        }

    def install_or_repair_scheduled_task(self) -> bool:
        """一键注册或修复每日 08:00 Windows 计划任务"""
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bat_path = os.path.join(project_root, "scripts", "run_daily_harvest.bat")
        task_name = self.settings.scheduled_task_name
        create_cmd = (
            f'schtasks /Create /TN "{task_name}" '
            f'/TR "\\"{bat_path}\\"" '
            f"/SC DAILY /ST 08:00 /RL HIGHEST /F"
        )
        res = subprocess.run(
            create_cmd,
            shell=True,
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="ignore",
        )
        return res.returncode == 0

    def check_browser_and_gateway(
        self, probe_live_gateway: bool = False, auto_fix: bool = False
    ) -> dict:
        """检查浏览器 CDP 端口、8090 账号登录与会员有效期、以及下游网关状态"""
        cdp_alive = self.gateway.is_cdp_alive()
        if not cdp_alive:
            if not auto_fix:
                return {
                    "status": "BROWSER_DOWN",
                    "cdp_url": self.settings.cdp_url,
                    "detail": "浏览器 9222 远程调试端口未启动（可执行 --fix 自动拉起浏览器并登录 8090）",
                }
            try:
                self.gateway.restart_edge_browser(kill_existing=True)
                cdp_alive = True
            except Exception as e:
                return {
                    "status": "BROWSER_DOWN",
                    "cdp_url": self.settings.cdp_url,
                    "detail": f"自动重启浏览器 9222 端口失败: {e}",
                }

        result = {
            "status": "OK",
            "cdp_url": self.settings.cdp_url,
            "member_info": {},
            "scopus_channel": "SKIPPED",
            "wos_channel": "SKIPPED",
            "detail": "",
        }
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(self.settings.cdp_url)
            ctx = browser.contexts[0]

            # 1. 核心校验：8090 账号登录与会员到期时间检查（秒级定位登录失败/账号过期）
            try:
                mem_info = self.gateway.verify_8090_member_status(ctx)
                result["member_info"] = mem_info
                result["detail"] = (
                    f"【定位结论】8090 账号 ({mem_info.get('username')}, ID={mem_info.get('user_id')}) "
                    f"登录有效，会员未过期（英文库到期时间: {mem_info.get('en_expire_time')}）。"
                )
            except GatewayAuthExpiredError as e:
                result["status"] = "AUTH_EXPIRED_OR_INVALID"
                result["detail"] = (
                    f"【定位结论】8090 账号登录失败或卡密已过期: {e}！请更新 .env 中的 CHANNEL_8090_USER / CHANNEL_8090_PWD。"
                )
                return result

            if not probe_live_gateway:
                return result

            # 2. 若指定了 --probe-gateway，进一步实测下游 12395 穿透与镜像节点可达性
            try:
                pg_sco = self.gateway.open_scopus_ready_page(ctx, force_reopen=auto_fix)
                result["scopus_channel"] = f"OK ({pg_sco.url[:70]})"
            except GatewayQuotaExhaustedError as e:
                result["scopus_channel"] = f"QUOTA_EXHAUSTED: {e}"
            except GatewayMirrorUnreachableError as e:
                result["scopus_channel"] = f"MIRROR_TIMEOUT: {e}"
            except GatewayCaptchaBlockedError as e:
                result["scopus_channel"] = f"CAPTCHA_BLOCKED: {e}"
            except GatewayError as e:
                result["scopus_channel"] = f"ERROR: {e}"

            try:
                pg_wos, _ = self.gateway.open_wos_ready_page(ctx, force_reopen=auto_fix)
                result["wos_channel"] = f"OK ({pg_wos.url[:70]})"
            except GatewayQuotaExhaustedError as e:
                result["wos_channel"] = f"QUOTA_EXHAUSTED: {e}"
            except GatewayMirrorUnreachableError as e:
                result["wos_channel"] = f"MIRROR_TIMEOUT: {e}"
            except GatewayCaptchaBlockedError as e:
                result["wos_channel"] = f"CAPTCHA_BLOCKED: {e}"
            except GatewayError as e:
                result["wos_channel"] = f"ERROR: {e}"

        return result

    def check_rds_completeness(self, days: int = 7) -> dict:
        """检查阿里云 RDS 生产数据库近 N 天 Scopus 双表与 WoS 主表的数据完整度"""
        try:
            conn = self.settings.create_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT load_date, COUNT(*) FROM `2026_scopus_records` "
                    "WHERE load_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY) GROUP BY load_date",
                    (days + 1,),
                )
                sco_rec = {str(k): int(v) for k, v in cur.fetchall()}

                cur.execute(
                    "SELECT load_date, COUNT(*), "
                    "SUM(CASE WHEN source_title IS NOT NULL AND source_title != '' THEN 1 ELSE 0 END) "
                    "FROM `2026_scopus_details` "
                    "WHERE load_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY) GROUP BY load_date",
                    (days + 1,),
                )
                sco_det = {str(k): (int(cnt), int(st or 0)) for k, cnt, st in cur.fetchall()}

                cur.execute(
                    "SELECT date_of_export, COUNT(*) FROM `2026_wos_records` "
                    "WHERE date_of_export >= DATE_SUB(CURDATE(), INTERVAL %s DAY) GROUP BY date_of_export",
                    (days + 1,),
                )
                wos_rec = {str(k): int(v) for k, v in cur.fetchall()}
            conn.close()
        except Exception as e:
            return {"status": "DB_CONNECT_ERROR", "detail": f"RDS 数据库连接失败: {e}", "daily": []}

        today = datetime.date.today()
        daily_rows = []
        gaps = []
        for offset in range(0, days + 1):
            d_str = (today - datetime.timedelta(days=offset)).isoformat()
            r_cnt = sco_rec.get(d_str, 0)
            d_cnt, st_cnt = sco_det.get(d_str, (0, 0))
            w_cnt = wos_rec.get(d_str, 0)

            day_status = "OK"
            if r_cnt > 0 and abs(r_cnt - d_cnt) > 50:
                day_status = f"SCOPUS_DETAIL_GAP(rec={r_cnt},det={d_cnt})"
                gaps.append(f"{d_str}:{day_status}")
            elif offset >= 2 and r_cnt == 0:
                day_status = "SCOPUS_PENDING"
                gaps.append(f"{d_str}:{day_status}")

            daily_rows.append(
                {
                    "date": d_str,
                    "scopus_records": r_cnt,
                    "scopus_details": d_cnt,
                    "scopus_source_title_ok": st_cnt,
                    "wos_records": w_cnt,
                    "status": day_status,
                }
            )

        return {
            "status": "OK" if not gaps else "HAS_PENDING_DAYS",
            "gaps": gaps,
            "daily": daily_rows,
        }

    def run_full_diagnosis(
        self, probe_gateway: bool = False, auto_fix: bool = False
    ) -> dict:
        print("=" * 78)
        print("   [8090 Scopus & WoS 自动采集系统] 故障秒级定位与健康诊断报告")
        print("=" * 78)

        task_report = self.check_scheduled_task(auto_fix=auto_fix)
        print(f"\n1. [自动计划任务检查] 状态: {task_report['status']}")
        print(f"   - 任务名称: {task_report.get('task_name')}")
        print(f"   - 诊断详情: {task_report.get('detail')}")

        gw_report = self.check_browser_and_gateway(
            probe_live_gateway=probe_gateway, auto_fix=auto_fix
        )
        print(f"\n2. [浏览器与 8090 登录鉴权检查] 状态: {gw_report['status']}")
        print(f"   - 诊断详情: {gw_report.get('detail')}")
        if probe_gateway:
            print(f"   - Scopus (goto={self.settings.scopus_goto_id}): {gw_report.get('scopus_channel')}")
            print(f"   - WoS    (id={self.settings.wos_entry_id}):   {gw_report.get('wos_channel')}")

        db_report = self.check_rds_completeness(days=7)
        print(f"\n3. [RDS 生产库近 7 日双库三表对账] 状态: {db_report['status']}")
        print(
            f"   {'日期':<12} | {'Scopus题录表':>12} | {'Scopus详情表':>12} | {'刊名非空数':>10} | {'WoS主表':>10} | 状态"
        )
        print("   " + "-" * 72)
        for r in db_report.get("daily", []):
            print(
                f"   {r['date']:<12} | {r['scopus_records']:>12,} | {r['scopus_details']:>12,} | "
                f"{r['scopus_source_title_ok']:>10,} | {r['wos_records']:>10,} | {r['status']}"
            )
        print("=" * 78)

        summary = {
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "scheduled_task": task_report,
            "gateway_and_auth": gw_report,
            "rds_completeness": db_report,
        }
        try:
            with open(self.status_json_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return summary


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="8090 自动采集系统故障秒级定位与一键修复工具 (专治：登录失败过期 / 自动任务未启动)"
    )
    parser.add_argument(
        "--probe-gateway",
        action="store_true",
        help="进一步实测下游 12395 验证码穿透与 60993.top 镜像节点连通性",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="自动修复发现的问题（自动重建/启用计划任务、重启锁死浏览器、重新登录 8090 刷新会话）",
    )
    args = parser.parse_args()

    settings = load_settings()
    doctor = HealthDoctor(settings)
    doctor.run_full_diagnosis(
        probe_gateway=args.probe_gateway, auto_fix=args.fix
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
