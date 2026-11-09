# Booknote — 8090 专属通道 (Scopus & Web of Science) 高内聚解耦自动采集系统

本项目是针对 **Elsevier Scopus** 与 **Clarivate Web of Science (WoS)** 的工业级每日全自动题录与全字段详情收割系统。
系统已彻底剔除所有失效的科研通（SciHuber）及高校 CARSI/WebVPN 历史脚本，全面收敛并固化至实测 100% 跑通的 **8090 专属机构高速通道**，并采用 **“网页打开/登录鉴权”与“题录采集/入库”完全解耦** 的高内聚架构。

---

## 一、 核心架构设计（高内聚、低耦合）

```text
booknote/
├── config/
│   └── settings.py                # 统一配置加载与强校验（读取 .env，缺失必填项直接报错定位）
├── gateway/
│   └── channel_8090.py            # 【网页打开与鉴权独立层】管理 Edge CDP 9222、8090 自动登录、12395 验证码穿透、流量超限检测，独立打开并交付 Scopus/WoS 页面
├── collectors/
│   ├── scopus_collector.py        # 【Scopus 采集高内聚层】接收就绪 Page -> 检索 -> 单包/正交二分导出 45 列 CSV -> 双重质量门禁 -> 双表原子入库 (2026_scopus_records & 2026_scopus_details)
│   ├── wos_collector.py           # 【WoS 采集高内聚层】接收就绪 Page -> LD 检索 -> 千篇 Full Record 切片导出 -> 断点续传 -> 71 列 JSONL & RDS 入库 (2026_wos_records)
│   ├── wos_xls.py                 # WoS Excel (.xls / .xlsx) 71 列标准解析器
│   ├── wos_to_mysql.py            # WoS JSONL -> 2026_wos_records 批量事务同步器
│   └── literature_oa_resolver.py  # 独立公网 OA & Landing Page 倒序反查增强器（不经过 8090 通道）
├── scheduler/
│   ├── daily_runner.py            # 【每日 08:00 自动目标级总控】串联 Gateway 与 Collectors，遇流量上限保护断点次日续传
│   └── health_doctor.py           # 【故障秒级定位与一键修复器】专治：①8090登录失败/过期 ②自动任务未启动 ③浏览器锁死 ④RDS双表对账
├── scripts/
│   ├── install_daily_task.ps1     # 一键注册每日 08:00 Windows 计划任务
│   ├── run_daily_harvest.bat      # 每日全自动采集启动入口
│   ├── open_gateway_only.bat      # 【解耦独立入口】仅打开并登录 8090 网页链接（不触发采集）
│   └── diagnose_and_fix.bat       # 【秒级定位与修复入口】一键诊断并修复计划任务、登录会话与数据对账
└── sql/
    ├── scopus_mysql.sql           # Scopus 双表建表结构
    └── wos_mysql.sql              # WoS 主表建表结构
```

---

## 二、 8090 通道双库采集标准机制

### 1. Scopus 专属通道 (`goto=2350`)
- **网关入口**：`http://www.8090chachong.com/app123.php?goto=2350`（穿透 12395 点选验证码后直达 `scopus-s-a2350.60993.top`）。
- **四步标准下载法**：
  1. **全字段导出提交**：勾选全部 28 个字段组（45 列 CSV，覆盖标题、刊名、摘要、作者、机构、关键词、基金、来源 JSON 等），提交后转入 `/pages/user/exports`；
  2. **任务卡片精准匹配**：按目标篇数精准匹配任务卡片，生成期间（`Export in Progress`）绝不抢点历史旧任务，等待 `READY`；
  3. **CDP 路径授权与落盘监听**：通过 `Browser.setDownloadBehavior` 授权下载目录，点击 `Download CSV` 后实时监听 `.crdownload` 消失且 `export_*.csv` 稳定落盘；
  4. **双重硬核门禁与双表原子入库**：校验必含 `Source title` 表头且空值率 `<= 5%`，按 `eid` 去重后同时原子写入 `2026_scopus_records` 与 `2026_scopus_details`。
- **超 20,000 篇正交二分与断点续传**：单日命中 `> 20,000` 篇时，自动拆分为 `DOCTYPE(ar)` 与 `NOT DOCTYPE(ar)` 两个正交子批次，已下载的子批次 CSV 自动缓存复用。

### 2. Web of Science 专属通道 (`id=5157`)
- **网关入口**：`http://www.8090chachong.com/e/action/ShowInfo.php?classid=85&id=5157`（穿透 12395 点选验证码后直达 `webofscience-s-a2313.60993.top`）。
- **千篇切片与断点续传**：按 `LD=YYYY-MM-DD/YYYY-MM-DD` 检索，每 1,000 篇切片导出 `Full Record` Excel（`.xls`），已有切片自动跳过，当日全部切片完成后同步入库 `2026_wos_records`。

---

## 三、 快速配置与解耦运行指南

### 1. 环境准备
```powershell
pip install -r requirements.txt
copy .env.example .env
# 编辑 .env 填入真实的 CHANNEL_8090_USER / CHANNEL_8090_PWD 及 RDS 数据库连接配置
```

### 2. 解耦模式 A：仅打开网页链接并完成 8090 登录鉴权（不执行采集）
当您需要单独打开网页检查会话、或预先建立浏览器登录态时，直接运行：
```powershell
# 同时打开并登录 Scopus 与 WoS 网页
python -m gateway.channel_8090 --open both

# 仅打开 Scopus 网页 / 仅打开 WoS 网页
python -m gateway.channel_8090 --open scopus
python -m gateway.channel_8090 --open wos
```
或直接双击运行 `scripts\open_gateway_only.bat`。

### 3. 解耦模式 B：独立执行 Scopus 或 WoS 题录采集
```powershell
# 独立运行 Scopus 近 7 日断口扫描与双表采集（也可指定具体日期，如 2026-11-08）
python -m collectors.scopus_collector

# 独立运行 WoS 近 7 日断口扫描与千篇切片采集
python -m collectors.wos_collector
```

### 4. 注册每日 08:00 全自动目标级采集任务
```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_daily_task.ps1
```
- 每天早上 `08:00` 自动运行 `scheduler.daily_runner`；
- 若当日通道流量额度用满（`exceeded max daily flow`），系统自动保护已落盘的 CSV / XLS 断点并安全退出，次日早上 `08:00` 通道额度恢复后自动从断点继续补全，直至所有日期 100% 对账完成。

---

## 四、 故障秒级定位与一键修复手册（专治：登录失败过期 / 自动任务未启动）

当怀疑系统出现 **“8090 账号登录失败或卡密过期”** 或 **“每日自动任务没有启动”** 时，直接双击运行 `scripts\diagnose_and_fix.bat` 或在终端执行：

```powershell
python -m scheduler.health_doctor --fix --probe-gateway
```

### 诊断状态码速查与处置：

| 诊断模块 | 状态码 | 含义与秒级定位原因 | 自动/人工修复方式 |
| :--- | :--- | :--- | :--- |
| **1. 自动计划任务** | `TASK_NOT_INSTALLED` | Windows 计划任务 `Daily_8AM_Scopus_WoS_Goal_Harvester` 丢失或未注册 | 加上 `--fix` 参数会自动调用 `schtasks /Create` 一键重建并设为每日 08:00 运行 |
| **1. 自动计划任务** | `TASK_DISABLED` | 计划任务被禁用 | 加上 `--fix` 参数会自动重新启用并恢复 `Ready` 状态 |
| **2. 浏览器与网关** | `BROWSER_DOWN` | Edge 9222 调试端口未启动或被残留进程锁死 | 加上 `--fix` 参数会自动清理残留 `msedge.exe` 并重启 9222 端口 |
| **2. 浏览器与网关** | `AUTH_EXPIRED_OR_INVALID` | **8090 账号密码错误或会员卡密已到期** | 打开 `.env` 更新 `CHANNEL_8090_USER` 与 `CHANNEL_8090_PWD` 后重跑 `--fix` |
| **2. 浏览器与网关** | `CAPTCHA_BLOCKED` | 12395 点选验证码连续未通过 | 重新执行 `--fix --probe-gateway` 触发 `ddddocr` 重新识别穿透 |
| **2. 浏览器与网关** | `QUOTA_EXHAUSTED` | 8090 账号正常，但**今日通道流量额度已满 (`exceeded max daily flow`)** | 无需人工干预，断点已保护，次日 08:00 额度重置后自动继续 |
| **3. RDS 数据对账** | `SCOPUS_DETAIL_GAP` | 某日题录表已有数据，但详情表缺失或条数不一致 | 下轮 `daily_runner` 或 `scopus_collector` 会自动将该日加入队列补齐双表 |
