# ============================================================
# 环境体检：不启动 Web，也能判断数据库/Key/目录是否就绪。
# 输出只显示“是否配置”，绝不打印 Key 或密码。
# ============================================================
from __future__ import annotations

from scripts.runtime_env import prepare_runtime_env

prepare_runtime_env()

from sqlalchemy import text  # noqa: E402

from backend.config import get_settings  # noqa: E402
from backend.database import engine  # noqa: E402


def main() -> int:
    settings = get_settings()
    print("APP_ENV =", settings.app_env)
    print("DATABASE =", engine.url.get_backend_name())
    print("DB_HOST =", engine.url.host or "local-file")
    print("DEEPSEEK_KEY_CONFIGURED =", bool(settings.deepseek_api_key.strip()))
    print("VECTOR_ENABLED =", settings.vector_enabled)
    print("STORAGE =", settings.storage_dir)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("DB_CONNECT = ok")
        return 0
    except Exception as exc:
        print("DB_CONNECT = failed:", exc.__class__.__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
