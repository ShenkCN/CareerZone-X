# ============================================================
# 数据库连接：SQLAlchemy 2.x + MySQL（测试可切 SQLite）。
# 为什么用连接池 + pre_ping：WSL/本机 MySQL 可能空闲断连，
# pre_ping 会在借出连接前探活，避免第一个请求随机报 "server has gone away"。
# ============================================================
from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.config import get_settings

settings = get_settings()

# SQLite 只在测试/临时演示使用；多线程访问要关掉 same-thread 检查。
_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
_engine_kwargs = {
    "echo": settings.db_echo,
    "pool_pre_ping": True,
    "future": True,
}
if not settings.database_url.startswith("sqlite"):
    # MySQL 连接不要无限增长；个人应用 10 个连接足够。
    _engine_kwargs.update({"pool_size": 5, "max_overflow": 5, "pool_recycle": 1800})

engine: Engine = create_engine(settings.database_url, connect_args=_connect_args, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


# SQLite 默认不启用外键；测试里也要能验证级联删除，所以手动打开。
if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：每个请求一个 Session，请求结束一定关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """脚本/后台任务用的事务上下文：成功提交，异常回滚。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """建表（幂等）。

    这里用 create_all 而不是 Alembic：MVP 阶段表结构还在快速变化，
    create_all 足够；上线后如果要多版本升级，再引入 Alembic。
    """
    from backend import models  # noqa: F401  # 导入模型才会注册到 Base.metadata

    Base.metadata.create_all(bind=engine)
