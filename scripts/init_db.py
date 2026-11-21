# ============================================================
# 建库脚本：python -m scripts.init_db
# 幂等：重复执行不会覆盖已有管理员密码/用户数据。
# 导入 backend 之前先适配 WSL MySQL 地址。
# ============================================================
from __future__ import annotations

import sys

from scripts.runtime_env import prepare_runtime_env


def main() -> int:
    host = prepare_runtime_env()
    from backend.bootstrap import bootstrap
    from backend.config import get_settings
    from backend.database import engine

    settings = get_settings()
    print(f"[1/2] 数据库后端：{engine.url.get_backend_name()}｜主机：{engine.url.host or '本机文件'}")
    bootstrap()
    print("[2/2] 建表 + 管理员检查完成")
    print("管理员用户名：", settings.admin_username)
    print("首次凭据：storage/private/initial_credentials.txt（若已删除，请使用你保存的密码）")
    print("实际数据库主机：", host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
