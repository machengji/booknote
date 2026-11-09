# -*- coding: utf-8 -*-
"""
系统全局配置加载与强校验模块 (config/settings.py)
严格遵循零兜底与安全规范：
1. 所有账号、数据库密钥均从 .env 或系统环境变量加载，严禁在代码中硬编码密码；
2. 严禁静默借用其他字段或假数据回退，缺失必要配置项时直接抛出清晰异常，便于秒级定位。
"""

import os
from dataclasses import dataclass
import pymysql


def _load_dotenv_file(dotenv_path: str) -> None:
    if not os.path.isfile(dotenv_path):
        return
    with open(dotenv_path, "r", encoding="utf-8", errors="ignore") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def _require_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(
            f"[CONFIG_MISSING] 缺少必填环境变量 '{key}'！请检查项目根目录下的 .env 配置文件。"
        )
    return val


@dataclass(frozen=True)
class AppSettings:
    # 8090 专属通道认证与入口配置
    channel_8090_user: str
    channel_8090_pwd: str
    scopus_goto_id: str
    wos_entry_id: str
    gateway_12395_user: str
    gateway_12395_pwd: str

    # 浏览器与 CDP 调试配置
    cdp_url: str
    edge_binary_path: str
    edge_user_data_dir: str
    browser_downloads_dir: str

    # RDS MySQL 生产库配置
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    mysql_database: str
    mysql_dsn: str

    # 本地数据目录、账本与日志目录
    data_root_dir: str
    scopus_data_dir: str
    scopus_raw_csv_dir: str
    scopus_ledger_path: str
    wos_data_dir: str
    wos_raw_xls_dir: str
    wos_ledger_path: str
    log_dir: str
    scheduled_task_name: str

    def create_db_connection(self, autocommit: bool = True):
        """创建并返回阿里云 RDS MySQL 数据库连接"""
        return pymysql.connect(
            host=self.mysql_host,
            port=self.mysql_port,
            user=self.mysql_user,
            password=self.mysql_password,
            database=self.mysql_database,
            charset="utf8mb4",
            autocommit=autocommit,
            connect_timeout=15,
        )


def load_settings(custom_env_path: str = "") -> AppSettings:
    """加载并校验全局配置"""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate_paths = []
    if custom_env_path:
        candidate_paths.append(custom_env_path)
    candidate_paths.append(os.path.join(project_root, ".env"))
    candidate_paths.append(r"E:\booknote\.env")
    candidate_paths.append(r"E:\machengji\booknote\.env")

    for path in candidate_paths:
        if os.path.isfile(path):
            _load_dotenv_file(path)
            break

    return AppSettings(
        channel_8090_user=_require_env("CHANNEL_8090_USER"),
        channel_8090_pwd=_require_env("CHANNEL_8090_PWD"),
        scopus_goto_id=_require_env("SCOPUS_GOTO_ID"),
        wos_entry_id=_require_env("WOS_ENTRY_ID"),
        gateway_12395_user=os.environ.get("GATEWAY_12395_USER", "").strip(),
        gateway_12395_pwd=os.environ.get("GATEWAY_12395_PWD", "").strip(),
        cdp_url=_require_env("CDP_URL"),
        edge_binary_path=_require_env("EDGE_BINARY_PATH"),
        edge_user_data_dir=_require_env("EDGE_USER_DATA_DIR"),
        browser_downloads_dir=_require_env("BROWSER_DOWNLOADS_DIR"),
        mysql_host=_require_env("MYSQL_HOST"),
        mysql_port=int(_require_env("MYSQL_PORT")),
        mysql_user=_require_env("MYSQL_USER"),
        mysql_password=_require_env("MYSQL_PASSWORD"),
        mysql_database=_require_env("MYSQL_DATABASE"),
        mysql_dsn=_require_env("MYSQL_DSN"),
        data_root_dir=_require_env("DATA_ROOT_DIR"),
        scopus_data_dir=_require_env("SCOPUS_DATA_DIR"),
        scopus_raw_csv_dir=_require_env("SCOPUS_RAW_CSV_DIR"),
        scopus_ledger_path=_require_env("SCOPUS_LEDGER_PATH"),
        wos_data_dir=_require_env("WOS_DATA_DIR"),
        wos_raw_xls_dir=_require_env("WOS_RAW_XLS_DIR"),
        wos_ledger_path=_require_env("WOS_LEDGER_PATH"),
        log_dir=_require_env("LOG_DIR"),
        scheduled_task_name=_require_env("SCHEDULED_TASK_NAME"),
    )
