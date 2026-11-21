# ============================================================
# API 启动器：先做 WSL MySQL 地址适配，再导入 backend。
# 这样 Windows 上不依赖不稳定的 localhost 转发。
# ============================================================
from __future__ import annotations

from scripts.runtime_env import prepare_runtime_env

prepare_runtime_env()

import uvicorn  # noqa: E402

from backend.config import get_settings  # noqa: E402

settings = get_settings()
uvicorn.run(
    "backend.main:app",
    host=settings.app_host,
    port=settings.app_port,
    log_level="info",
)
