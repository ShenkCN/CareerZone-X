# ============================================================
# 运行环境适配：Windows 上的 127.0.0.1 不一定能连到 WSL2 MySQL。
# 这个模块在导入 backend 之前运行：
# 1) 先试 .env 里的 DATABASE_URL；
# 2) 连不上且 host 是 localhost，就取 WSL IP 再试；
# 3) 成功则只改本进程的 os.environ，不修改 .env，不打印密码。
# ============================================================
from __future__ import annotations

import os
import subprocess
from urllib.parse import urlsplit, urlunsplit

import pymysql
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _wsl_ip() -> str:
    """取 WSL2 的 IPv4；失败返回空字符串。"""
    try:
        output = subprocess.run(
            ["wsl.exe", "-d", "Ubuntu-22.04", "--", "hostname", "-I"],
            capture_output=True,
            text=True,
            timeout=8,
            encoding="utf-8",
            errors="ignore",
        ).stdout
        for token in output.split():
            if token.count(".") == 3:
                return token
    except Exception:
        return ""
    return ""


def _can_connect(url: str) -> bool:
    """用 PyMySQL 做 2 秒探活；不打印 URL/密码。"""
    try:
        parts = urlsplit(url)
        pymysql.connect(
            host=parts.hostname or "127.0.0.1",
            port=parts.port or 3306,
            user=parts.username or "",
            password=parts.password or "",
            database=(parts.path or "").lstrip("/"),
            connect_timeout=2,
            read_timeout=2,
            write_timeout=2,
        ).close()
        return True
    except Exception:
        return False


def prepare_runtime_env() -> str:
    """返回实际使用的数据库 host，供启动脚本打印。"""
    load_dotenv(os.path.join(ROOT, ".env"), override=False)
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("mysql"):
        return "non-mysql"
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost"}:
        return host

    # 关键：Windows 的 WSL localhost 转发时好时坏，不能“碰巧连上就固定用 127”。
    # 只要 .env 写的是 127/localhost，就优先找 WSL IP 并固定到本进程；
    # 找不到 WSL（例如纯 Windows MySQL）再退回 127。
    wsl_ip = _wsl_ip()
    if wsl_ip:
        netloc = f"{parts.username}:{parts.password}@{wsl_ip}:{parts.port or 3306}" if parts.username else f"{wsl_ip}:{parts.port or 3306}"
        candidate = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        if _can_connect(candidate):
            os.environ["DATABASE_URL"] = candidate
            print(f"[runtime] MySQL 固定使用 WSL：{wsl_ip}:{parts.port or 3306}")
            return wsl_ip
        print(f"[runtime] WSL IP {wsl_ip} 的 MySQL 不可达；尝试 127.0.0.1")
    if _can_connect(url):
        return "127.0.0.1"
    print("[runtime] MySQL 127.0.0.1 和 WSL IP 均不可达，请检查 bind-address / 用户权限")
    return "127.0.0.1"
