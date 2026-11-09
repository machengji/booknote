# -*- coding: utf-8 -*-
"""
8090 专属通道网页打开与登录鉴权独立模块 (gateway/channel_8090.py)
================================================================================
架构定位（高内聚、与采集器 100% 解耦）：
1. 唯一职责：管理 Edge CDP 9222 浏览器实例、执行 8090 平台账号登录、穿透 12395 点选验证码、
   检测通道流量额度、打开并交付已就绪的 Scopus (goto=2350) 与 WoS (id=5157) 检索页面。
2. 严禁包含任何文献检索、CSV/Excel 导出或数据库入库代码。
3. 细分明确异常：
   - GatewayAuthExpiredError: 8090 账号登录失败、密码错误或卡密会员过期（秒级定位登录问题）
   - GatewayQuotaExhaustedError: 当日通道流量额度已满 (exceeded max daily flow)
   - GatewayCaptchaBlockedError: 12395 网关验证码连续多次未穿透
   - GatewayError: 浏览器端口未就绪或网络不可达
4. 支持独立运行：
   - 仅打开 Scopus 网页并保持登录：python -m gateway.channel_8090 --open scopus
   - 仅打开 WoS 网页并保持登录：   python -m gateway.channel_8090 --open wos
   - 同时打开双库网页并验证会话：  python -m gateway.channel_8090 --open both
"""

import os
import sys
import io
import re
import time
import json
import datetime
import subprocess
import urllib.request
from PIL import Image
import ddddocr
from playwright.sync_api import BrowserContext, Page, sync_playwright

from config.settings import AppSettings, load_settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class GatewayError(RuntimeError):
    """网关层基础异常"""


class GatewayAuthExpiredError(GatewayError):
    """8090 账号登录失败、会话失效或卡密过期异常"""


class GatewayQuotaExhaustedError(GatewayError):
    """当日通道流量额度已满 (exceeded max daily flow) 异常"""


class GatewayCaptchaBlockedError(GatewayError):
    """12395 字符点选验证码未能穿透异常"""


class GatewayMirrorUnreachableError(GatewayError):
    """8090 登录与 12395 认证均成功，但下游高校镜像节点 (60993.top) 响应超时或不可达异常"""


class Channel8090Gateway:
    """8090 专属通道网页打开与会话鉴权高内聚管理器"""

    def __init__(self, settings: AppSettings):
        self.settings = settings
        os.makedirs(self.settings.log_dir, exist_ok=True)
        self.log_file = os.path.join(self.settings.log_dir, "gateway_8090.log")
        self._det_ocr = None
        self._cls_ocr = None

    def _init_ocr(self) -> None:
        if self._det_ocr is None or self._cls_ocr is None:
            self._det_ocr = ddddocr.DdddOcr(det=True, show_ad=False)
            self._cls_ocr = ddddocr.DdddOcr(show_ad=False)

    def log(self, *args) -> None:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"{ts} [Gateway-8090] " + " ".join(str(a) for a in args)
        print(msg, flush=True)
        try:
            with open(self.log_file, "a", encoding="utf-8", errors="replace") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    def is_cdp_alive(self) -> bool:
        """检查 Edge 9222 远程调试端口是否在线"""
        try:
            with urllib.request.urlopen(f"{self.settings.cdp_url}/json/version", timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    def restart_edge_browser(self, kill_existing: bool = False) -> bool:
        """确保 Chrome/Edge 浏览器以 9222 远程调试模式独立常驻运行"""
        if not kill_existing and self.is_cdp_alive():
            self.log(f"浏览器 CDP 端口 ({self.settings.cdp_url}) 已在线活跃。")
            return True

        exe_name = os.path.basename(self.settings.edge_binary_path)
        if kill_existing and not self.is_cdp_alive():
            self.log(f"正在清理残留的 {exe_name} 进程以释放 Profile 锁...")
            subprocess.run(
                f'taskkill /f /im "{exe_name}"',
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(2)

        self.log(
            f"正在拉起 {exe_name} 远程调试实例 (Profile: {self.settings.edge_user_data_dir})..."
        )
        os.makedirs(self.settings.edge_user_data_dir, exist_ok=True)
        cmd_line = (
            f'"{self.settings.edge_binary_path}" '
            f"--remote-debugging-port=9222 "
            f'--user-data-dir="{self.settings.edge_user_data_dir}" '
            f"--no-proxy-server "
            f"--ignore-certificate-errors "
            f"--disable-blink-features=AutomationControlled "
            f"--no-first-run --no-default-browser-check"
        )
        ps_launch = (
            f"Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
            f"-Arguments @{{CommandLine='{cmd_line}'}} | Out-Null"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_launch],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(15):
            time.sleep(1.5)
            if self.is_cdp_alive():
                self.log(f"{exe_name} 已成功启动并监听 CDP 9222 端口。")
                return True

        raise GatewayError(
            f"[BROWSER_DOWN] 无法启动或连接浏览器 CDP 端口 ({self.settings.cdp_url})，请检查 EDGE_BINARY_PATH 与 Profile 目录权限。"
        )

    def check_quota_exhausted(self, page: Page, channel_label: str) -> None:
        """检查页面是否出现当日流量超限提示，若超限立即抛出 GatewayQuotaExhaustedError"""
        try:
            body_txt = page.evaluate(
                "() => document.body ? document.body.innerText.slice(0, 600) : ''"
            )
            if "exceeded max daily flow" in (body_txt or "").lower():
                raise GatewayQuotaExhaustedError(
                    f"[QUOTA_EXHAUSTED] {channel_label} 今日通道流量额度已满 (exceeded max daily flow)，请等待次日 08:00 额度重置。"
                )
        except GatewayQuotaExhaustedError:
            raise
        except Exception:
            pass

    def dismiss_cookie_overlays(self, page: Page) -> None:
        """清除 OneTrust / Elsevier 隐私与 Cookie 弹窗遮罩"""
        try:
            page.evaluate(
                """() => {
                const selectors = [
                    '#onetrust-accept-btn-handler',
                    '#accept-recommended-btn-handler'
                ];
                for (const sel of selectors) {
                    const btn = document.querySelector(sel);
                    if (btn) btn.click();
                }
                const sdk = document.querySelector('#onetrust-consent-sdk');
                if (sdk) sdk.remove();
                const dark = document.querySelector('.onetrust-pc-dark-filter');
                if (dark) dark.remove();
                if (document.body) document.body.style.overflow = 'auto';
            }"""
            )
        except Exception:
            pass

    def solve_12395_captcha(self, page: Page) -> bool:
        """检测并使用 ddddocr 自动穿透 12395 网关字符点选验证码"""
        try:
            img_el = page.locator("#image, img#image, #img, img#img").first
            if not img_el.count() or not img_el.is_visible():
                return False
        except Exception:
            return False

        self._init_ocr()
        self.log("检测到 12395 网关字符点选验证码，启动 ddddocr 识别穿透...")
        time.sleep(1.5)

        try:
            raw_target = page.evaluate(
                "() => document.querySelector('#key1') ? document.querySelector('#key1').innerText.trim() : ''"
            )
            chars = re.findall(r"[\u4e00-\u9fa5a-zA-Z0-9]", raw_target or "")
            target_char = chars[-1] if chars else (raw_target or "").strip()
            self.log(f"  验证码提示: '{raw_target}' -> 目标字符: 【{target_char}】")

            img_bytes = img_el.screenshot()
            crops = self._det_ocr.detection(img_bytes)
            self.log(f"  检测到 {len(crops)} 个候选字符区域")

            match_box = None
            full_im = Image.open(io.BytesIO(img_bytes))
            for box in crops:
                x1, y1, x2, y2 = box
                crop_im = full_im.crop((x1, y1, x2, y2))
                buf = io.BytesIO()
                crop_im.save(buf, format="PNG")
                char_txt = self._cls_ocr.classification(buf.getvalue()).strip()
                if target_char and (
                    target_char in char_txt or (len(char_txt) == 1 and char_txt == target_char)
                ):
                    self.log(f"  ★ 字符匹配命中: '{char_txt}' 位于坐标 {box}")
                    match_box = box
                    break

            if not match_box and crops:
                self.log("  未精确匹配字符，点击首个候选框重试")
                match_box = crops[0]

            if match_box:
                box_geo = img_el.bounding_box()
                if box_geo:
                    click_x = box_geo["x"] + (match_box[0] + match_box[2]) / 2
                    click_y = box_geo["y"] + (match_box[1] + match_box[3]) / 2
                    page.mouse.click(click_x, click_y)
                    time.sleep(0.8)
                    sub_btn = page.locator(
                        "#submit, input[type='submit'], button[type='submit']"
                    ).first
                    if sub_btn.count() and sub_btn.is_visible():
                        sub_btn.click()
                    time.sleep(4)
                    return True
        except Exception as e:
            self.log(f"  穿透 12395 验证码异常: {e}")
        return False

    def authenticate_8090_if_needed(self, page: Page) -> None:
        """
        检测并执行 8090 平台登录与二级 12395 网关登录。
        若账号密码错误、卡密过期或登录后仍停留在登录拦截页，立即抛出 GatewayAuthExpiredError。
        """
        cur_url = page.url or ""
        has_user_input = page.query_selector("input[name='username']") is not None
        if "login" in cur_url.lower() or has_user_input:
            self.log(
                f"检测到 8090 登录表单 ({cur_url[:80]})，正在使用账号 {self.settings.channel_8090_user} 自动登录..."
            )
            dialog_messages = []

            def _on_dialog(dialog):
                dialog_messages.append(dialog.message)
                try:
                    dialog.accept()
                except Exception:
                    pass

            page.on("dialog", _on_dialog)
            try:
                page.fill("input[name='username']", self.settings.channel_8090_user)
                time.sleep(0.3)
                page.fill("input[name='password']", self.settings.channel_8090_pwd)
                time.sleep(0.4)
                page.click("input[name='btn1'], input[type='submit'], button[type='submit']")
                time.sleep(3.5)
            except Exception as e:
                raise GatewayAuthExpiredError(
                    f"[AUTH_LOGIN_ERROR] 8090 登录表单提交失败: {e}"
                ) from e
            finally:
                try:
                    page.remove_listener("dialog", _on_dialog)
                except Exception:
                    pass

            if dialog_messages:
                joined_msg = " | ".join(dialog_messages)
                if any(
                    kw in joined_msg
                    for kw in ["错误", "过期", "到期", "不存在", "失败", "禁用", "invalid", "error"]
                ):
                    raise GatewayAuthExpiredError(
                        f"[AUTH_EXPIRED_OR_INVALID] 8090 账号 ({self.settings.channel_8090_user}) 登录被拒，系统弹窗提示: {joined_msg}"
                    )

            after_body = ""
            try:
                after_body = page.evaluate(
                    "() => document.body ? document.body.innerText.slice(0, 500) : ''"
                )
            except Exception:
                pass
            if any(
                kw in after_body
                for kw in ["密码错误", "账号已过期", "会员已到期", "用户名不存在", "您的账号已到期"]
            ):
                raise GatewayAuthExpiredError(
                    f"[AUTH_EXPIRED_OR_INVALID] 8090 账号 ({self.settings.channel_8090_user}) 登录失败或已过期: {after_body[:150]}"
                )

            if page.query_selector("input[name='username']") is not None and "login" in (
                page.url or ""
            ).lower():
                raise GatewayAuthExpiredError(
                    f"[AUTH_STUCK_ON_LOGIN] 提交账号 {self.settings.channel_8090_user} 后仍停留在 8090 登录页 ({page.url})，请核查账号密码或卡密有效期！"
                )
            self.log(f"8090 账号登录成功，当前跳转 URL: {page.url}")

        # 检查是否出现 12395 二级机构登录框
        if page.query_selector("#loginForm, #username") is not None and self.settings.gateway_12395_user:
            self.log("检测到 12395 二级网关登录表单，执行自动填充...")
            try:
                u_el = page.query_selector("#username")
                p_el = page.query_selector("#password")
                if u_el:
                    u_el.fill(self.settings.gateway_12395_user)
                if p_el:
                    p_el.fill(self.settings.gateway_12395_pwd)
                sub_el = page.query_selector("#submit, input[type='submit'], button[type='submit']")
                if sub_el:
                    sub_el.click()
                    time.sleep(4)
            except Exception as e:
                self.log(f"12395 二级网关登录填充异常: {e}")

    def verify_8090_member_status(self, ctx: BrowserContext) -> dict:
        """
        直连 8090 会员权限页 (http://www.8090chachong.com/e/member/my/) 验证账号登录与到期时间。
        若未登录则自动使用 .env 凭据登录；若密码错误或会员已到期，立即抛出 GatewayAuthExpiredError。
        """
        my_url = "http://www.8090chachong.com/e/member/my/"
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto(my_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

        self.authenticate_8090_if_needed(pg)
        if "member/my" not in (pg.url or "").lower():
            pg.goto(my_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)

        body = pg.inner_text("body") if pg.query_selector("body") else ""
        if self.settings.channel_8090_user not in body and "用户ID" not in body:
            raise GatewayAuthExpiredError(
                f"[AUTH_EXPIRED_OR_INVALID] 8090 会员中心未识别到有效登录态 ({pg.url})"
            )

        uid_m = re.search(r"用户ID[:：]\s*(\d+)", body)
        exp_matches = re.findall(r"([^\n:：]+)[:：]\s*到期时间[:：]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", body)
        uid_val = uid_m.group(1) if uid_m else ""
        now_dt = datetime.datetime.now()
        en_expire_str = ""
        for label, dt_str in exp_matches:
            if "英文" in label:
                en_expire_str = dt_str
                try:
                    exp_dt = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                    if exp_dt <= now_dt:
                        raise GatewayAuthExpiredError(
                            f"[AUTH_EXPIRED] 8090 账号 ({self.settings.channel_8090_user}) 的 [{label.strip()}] 权限已于 {dt_str} 过期，请及时续费！"
                        )
                except GatewayAuthExpiredError:
                    raise
                except Exception:
                    pass

        info = {
            "username": self.settings.channel_8090_user,
            "user_id": uid_val,
            "en_expire_time": en_expire_str,
        }
        self.log(
            f"[AUTH_OK] 8090 账号 ({info['username']}, ID={info['user_id']}) 登录正常，英文库到期时间: {en_expire_str or '有效'}"
        )
        return info

    def _penetrate_12395_gateway(self, page: Page, target_keywords: tuple, max_attempts: int = 5) -> None:
        """循环穿透 12395 点选验证码直至抵达目标学术镜像域名"""
        for attempt in range(max_attempts):
            cur_u = (page.url or "").lower()
            if "chrome-error://" in cur_u:
                err_txt = ""
                try:
                    err_txt = page.inner_text("body")[:250].replace("\n", " ")
                except Exception:
                    pass
                raise GatewayMirrorUnreachableError(
                    f"[MIRROR_UNREACHABLE] 8090 登录与 12395 验证码已通过，但下游镜像节点 (login.60993.top) 响应超时: {err_txt}"
                )
            if any(k in cur_u for k in target_keywords) and "12395.net" not in cur_u:
                return
            if "12395.net" in cur_u or page.query_selector("#image, img#image"):
                self.log(f"第 {attempt + 1}/{max_attempts} 次穿透 12395 点选验证码...")
                self.solve_12395_captcha(page)
                time.sleep(3)
            else:
                time.sleep(2)

        final_u = (page.url or "").lower()
        if "chrome-error://" in final_u:
            err_txt = ""
            try:
                err_txt = page.inner_text("body")[:250].replace("\n", " ")
            except Exception:
                pass
            raise GatewayMirrorUnreachableError(
                f"[MIRROR_UNREACHABLE] 8090 登录与 12395 验证码已通过，但下游镜像节点 (login.60993.top) 响应超时: {err_txt}"
            )
        if "12395.net" in final_u:
            raise GatewayCaptchaBlockedError(
                f"[CAPTCHA_BLOCKED] 连续 {max_attempts} 次未能穿透 12395 验证码，当前停留在: {page.url}"
            )

    def open_scopus_ready_page(self, ctx: BrowserContext, force_reopen: bool = False) -> Page:
        """
        打开或复用 8090 专属通道下的 Scopus 高级检索页面 (goto=2350)。
        仅负责网页打开、会话登录与就绪校验，返回可立即检索的 Page 对象。
        """
        sco_pg = None
        if not force_reopen:
            for p in ctx.pages:
                u = (p.url or "").lower()
                if ("scopus" in u or "elsevier" in u) and "login" not in u and "60993" in u:
                    try:
                        p.title()
                        self.check_quota_exhausted(p, f"Scopus(goto={self.settings.scopus_goto_id})")
                        sco_pg = p
                        self.log(f"成功复用已存活的 Scopus 标签页: {p.url[:85]}")
                        break
                    except GatewayQuotaExhaustedError:
                        raise
                    except Exception:
                        continue

        entry_url = (
            f"http://www.8090chachong.com/app123.php?goto={self.settings.scopus_goto_id}"
        )
        if not sco_pg:
            self.log(f"正在通过 8090 直通网关打开 Scopus 网页链接: {entry_url}")
            sco_pg = ctx.new_page()
            sco_pg.goto(entry_url, wait_until="domcontentloaded", timeout=40000)
            time.sleep(3)

            self.authenticate_8090_if_needed(sco_pg)
            if "8090chachong.com" in (sco_pg.url or "").lower() and "app123.php" not in (
                sco_pg.url or ""
            ).lower():
                self.log(f"8090 登录完成，重新进入 Scopus 直通网关: {entry_url}")
                sco_pg.goto(entry_url, wait_until="domcontentloaded", timeout=40000)
                time.sleep(3)

            self._penetrate_12395_gateway(sco_pg, ("60993", "scopus"))

        self.check_quota_exhausted(sco_pg, f"Scopus(goto={self.settings.scopus_goto_id})")

        cur_u = sco_pg.url or ""
        if "chrome-error://" in cur_u.lower():
            raise GatewayMirrorUnreachableError(
                f"[SCOPUS_MIRROR_TIMEOUT] 8090 鉴权通过，但下游 Scopus 镜像节点 (60993.top) 响应超时: {cur_u}"
            )
        if "scopus" not in cur_u.lower() and "60993" not in cur_u.lower():
            raise GatewayMirrorUnreachableError(
                f"[SCOPUS_GATEWAY_UNREACHABLE] 未能进入 Scopus 镜像页，当前停留 URL: {cur_u}"
            )

        base_origin = (
            cur_u.split("/results/")[0]
            .split("/pages/")[0]
            .split("/search/")[0]
            .rstrip("/")
        )
        adv_url = f"{base_origin}/search/form.uri?display=advanced"
        if "display=advanced" not in cur_u:
            self.log(f"导航至 Scopus 高级检索页面: {adv_url}")
            try:
                sco_pg.goto(adv_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)
            except Exception as e:
                self.log(f"导航 Scopus 高级检索页提示: {e}")

        self.check_quota_exhausted(sco_pg, f"Scopus(goto={self.settings.scopus_goto_id})")
        self.dismiss_cookie_overlays(sco_pg)
        self.log(f"[READY] Scopus 网页会话已就绪: {sco_pg.url[:90]}")
        return sco_pg

    def open_wos_ready_page(self, ctx: BrowserContext, force_reopen: bool = False) -> tuple[Page, str]:
        """
        打开或复用 8090 专属通道下的 Web of Science (WoS) 高级检索页面 (id=5157)。
        仅负责网页打开、会话登录与就绪校验，返回 (wos_page, adv_url)。
        """
        wos_pg = None
        if not force_reopen:
            for p in ctx.pages:
                u = (p.url or "").lower()
                if "webofscience" in u or "clarivate" in u or "a2313" in u:
                    try:
                        p.title()
                        self.check_quota_exhausted(p, f"WoS(id={self.settings.wos_entry_id})")
                        wos_pg = p
                        self.log(f"成功复用已存活的 WoS 标签页: {p.url[:85]}")
                        break
                    except GatewayQuotaExhaustedError:
                        raise
                    except Exception:
                        continue

        entry_url = (
            f"http://www.8090chachong.com/e/action/ShowInfo.php?classid=85&id={self.settings.wos_entry_id}"
        )
        if not wos_pg:
            self.log(f"正在通过 8090 专属通道打开 WoS 网页链接: {entry_url}")
            wos_pg = ctx.new_page()
            wos_pg.goto(entry_url, wait_until="domcontentloaded", timeout=45000)
            time.sleep(3.5)

            self.authenticate_8090_if_needed(wos_pg)
            if (
                "showinfo" not in (wos_pg.url or "").lower()
                and "12395" not in (wos_pg.url or "").lower()
                and "webofscience" not in (wos_pg.url or "").lower()
                and "60993" not in (wos_pg.url or "").lower()
            ):
                self.log(f"8090 登录完成，重新打开 WoS 通道入口: {entry_url}")
                wos_pg.goto(entry_url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(3.5)

            self._penetrate_12395_gateway(wos_pg, ("webofscience", "60993", "a2313"))

        self.check_quota_exhausted(wos_pg, f"WoS(id={self.settings.wos_entry_id})")

        cur_u = wos_pg.url or ""
        if "chrome-error://" in cur_u.lower():
            raise GatewayMirrorUnreachableError(
                f"[WOS_MIRROR_TIMEOUT] 8090 鉴权通过，但下游 WoS 镜像节点 (60993.top) 响应超时: {cur_u}"
            )
        idx = cur_u.find("/wos/")
        prefix = cur_u[:idx] if idx >= 0 else "https://www.webofscience.com"
        adv_url = f"{prefix}/wos/woscc/advanced-search"

        if "advanced-search" not in cur_u:
            self.log(f"导航至 WoS 核心合集高级检索页: {adv_url}")
            try:
                wos_pg.goto(adv_url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(3.5)
            except Exception as e:
                self.log(f"导航 WoS 高级检索页提示: {e}")

        self.check_quota_exhausted(wos_pg, f"WoS(id={self.settings.wos_entry_id})")
        self.dismiss_cookie_overlays(wos_pg)
        self.log(f"[READY] WoS 网页会话已就绪: {wos_pg.url[:90]}")
        return wos_pg, adv_url


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="8090 专属通道网页打开与鉴权独立工具（与题录采集解耦）"
    )
    parser.add_argument(
        "--open",
        choices=["scopus", "wos", "both"],
        default="both",
        help="仅打开并认证指定数据库的网页链接，不执行采集",
    )
    parser.add_argument(
        "--force-reopen",
        action="store_true",
        help="强制新建标签页重新走 8090 登录与网关认证流程",
    )
    parser.add_argument(
        "--restart-browser",
        action="store_true",
        help="先清理锁死的 Edge 进程并重启 9222 端口后再打开链接",
    )
    args = parser.parse_args()

    settings = load_settings()
    gw = Channel8090Gateway(settings)
    gw.restart_edge_browser(kill_existing=args.restart_browser)

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(settings.cdp_url)
        ctx = browser.contexts[0]
        if args.open in ("scopus", "both"):
            try:
                pg_sco = gw.open_scopus_ready_page(ctx, force_reopen=args.force_reopen)
                gw.log(f"Scopus 页面打开并验证通过: {pg_sco.url}")
            except GatewayError as e:
                gw.log(f"[告警] Scopus 网页通道状态: {e}")
                if args.open == "scopus":
                    return 2

        if args.open in ("wos", "both"):
            try:
                pg_wos, adv_url = gw.open_wos_ready_page(ctx, force_reopen=args.force_reopen)
                gw.log(f"WoS 页面打开并验证通过: {pg_wos.url} (高级检索: {adv_url})")
            except GatewayError as e:
                gw.log(f"[告警] WoS 网页通道状态: {e}")
                if args.open == "wos":
                    return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
