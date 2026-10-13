# WoS / Scopus 反重力定时巡检与自主修复体系

本模块适配 **Antigravity Scheduled Tasks** 定时任务能力（Cron 表达式调度），提供全自动的高可用采集巡检、进程守护、异常重启与数据库自动入库。

---

## 核心功能

1. **Scopus 详情线监控与自动拉起**：
   - 自动检测 Scopus 详情采集进程状态；
   - 监控日志，若检测到 `401 UNAUTHORIZED`（会话失效/Cookie 过期），自动杀掉旧进程并重新触发计划任务刷新登录会话；
   - 掉线异常时自动通过系统计划任务（`_zc_scopdet_runner`）拉起。
2. **WoS 采集线监控与进度核查**：
   - 核查目标日期（如 10-12、10-13）采集账本（`wos_day_ledger_ld.json`）；
   - 掉线时自动拉起计划任务（`_zc_wos_runner`）。
3. **WoS 采齐自动触发 RDS 增量入库**：
   - 目标批次（例如 10-12 与 10-13）全量采齐后，自动调用 `harvest/wos_to_mysql.py` 将最新数据导入 RDS 数据库表（`2026_wos_records`）；
   - 写入标记文件防止重复入库。
4. **采集线专注保护**：
   - 严格保持其余无关非重点采集线处于暂停，避免争抢带宽与 IP。
5. **持久化双线简报**：
   - 输出 <=8 行精简巡检报告；
   - 自动追加写入本地文件 `patrol_report.md`，支持追溯历史状态与自主修复动作。

---

## 快速运行与演练

### 1. 本地演练模式（Dry-Run）
不执行真实的杀进程、重启计划任务与入库操作，仅打印当前巡检状态与诊断动作：
```bash
python harvest/antigravity_patrol.py --dry-run
```

### 2. 真实执行一次巡检与修复
```bash
python harvest/antigravity_patrol.py
```

---

## 反重力定时任务（Scheduled Tasks）接入

将 `sidecars/wos-scopus/sidecar.json` 挂载到 Antigravity 配置目录（如 `~/.gemini/config/sidecars/wos-scopus/sidecar.json`）后，系统左侧菜单的 **Scheduled Tasks** 会自动加载该任务卡片：

- **周期**：`0 * * * *`（每小时整点自动触发）
- **持久化状态**：由 Sidecar 声明持久化落盘，重启客户端或切换模型均永不丢失。
