# ============================================================
# 数据库切换：python -m scripts.switch_db sqlite|mysql
# 只改 .env 的 DATABASE_URL，不碰数据文件。
# MySQL 连接串备份在 .env.mysql；切换前请确认 WSL MySQL 已启动。
# ============================================================
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
MYSQL_ENV = ROOT / ".env.mysql"
SQLITE_URL = "sqlite:///./storage/careerzone.db"


def _replace_url(text: str, new_url: str) -> str:
    """保留 .env 其它行，只替换 DATABASE_URL；没有就追加。"""
    if re.search(r"^DATABASE_URL=.*$", text, flags=re.M):
        return re.sub(r"^DATABASE_URL=.*$", f"DATABASE_URL={new_url}", text, count=1, flags=re.M)
    return text.rstrip() + f"\nDATABASE_URL={new_url}\n"


def main() -> int:
    target = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    if target not in {"sqlite", "mysql"}:
        print("用法：python -m scripts.switch_db sqlite|mysql")
        return 2
    if not ENV.exists():
        print("缺少 .env")
        return 1
    text = ENV.read_text(encoding="utf-8")

    if target == "sqlite":
        # 如果当前是 MySQL，先备份连接串，方便以后切回。
        match = re.search(r"^DATABASE_URL=(mysql.*)$", text, flags=re.M)
        if match:
            MYSQL_ENV.write_text("DATABASE_URL=" + match.group(1) + "\n", encoding="utf-8")
        ENV.write_text(_replace_url(text, SQLITE_URL), encoding="utf-8")
        print("已切换到 SQLite：storage/careerzone.db")
        print("原因：本机可跑、无外部依赖；MySQL 连接已备份到 .env.mysql")
        return 0

    if not MYSQL_ENV.exists():
        print("缺少 .env.mysql；请先手动写入 MySQL DATABASE_URL，或继续用 SQLite。")
        return 1
    mysql_url = MYSQL_ENV.read_text(encoding="utf-8").strip().removeprefix("DATABASE_URL=").strip()
    if not mysql_url.startswith("mysql"):
        print(".env.mysql 格式不正确")
        return 1
    ENV.write_text(_replace_url(text, mysql_url), encoding="utf-8")
    print("已切换到 MySQL（连接串来自 .env.mysql）")
    print("下一步：确认 WSL MySQL 已启动，然后运行 python -m scripts.check_env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
