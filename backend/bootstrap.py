# ============================================================
# 初始化：建表 + 写入管理员 + 写入默认密保问题。
# 为什么启动时也执行：用户可能忘了先跑 init_db；幂等操作不会覆盖已有数据。
# ============================================================
from __future__ import annotations

import logging

from sqlalchemy import select

from backend.config import Settings, get_settings
from backend.database import session_scope
from backend.models import AdminSetting, ImportBatch, Job, JobCapture, User, utcnow
from backend.security import (
    hash_password,
    normalize_security_answer,
    validate_password,
    validate_username,
)

logger = logging.getLogger(__name__)


def ensure_admin(settings: Settings | None = None) -> None:
    """确保唯一管理员存在，且密保问题已写入。

    管理员密码固定：这里只在“不存在”时创建；已存在时绝不再用 .env 覆盖。
    这样即使 .env 被改，也不会把管理员密码悄悄换掉。
    """
    settings = settings or get_settings()
    with session_scope() as db:
        username = (settings.admin_username or "Shenk").strip()
        existing = db.execute(
            select(User).where(User.role == "admin").order_by(User.id.asc())
        ).scalars().first()

        if existing is None:
            if not settings.admin_initial_password or not settings.admin_initial_answer:
                raise RuntimeError(
                    "数据库中没有管理员，但 .env 的 ADMIN_INITIAL_PASSWORD / "
                    "ADMIN_INITIAL_ANSWER 为空。请补齐后重启。"
                )
            validate_username(username)
            validate_password(settings.admin_initial_password)
            admin = User(
                username=username,
                display_name=f"{username}（管理员）",
                password_hash=hash_password(settings.admin_initial_password),
                role="admin",
                status="active",
                is_password_fixed=True,
                approved_at=utcnow(),
            )
            db.add(admin)
            db.flush()
            question = settings.admin_security_question.strip()
            answer = normalize_security_answer(settings.admin_initial_answer)
            db.add(AdminSetting(key="security_question", value=question))
            db.add(AdminSetting(key="security_answer_hash", value=hash_password(answer)))
            logger.info("已创建管理员账号 %s", username)
            return

        # 已存在：只修复必要标记，不覆盖密码、不覆盖用户改过的密保。
        if existing.role != "admin":
            existing.role = "admin"
        if existing.status != "active":
            existing.status = "active"
        existing.is_password_fixed = True

        question_setting = db.execute(
            select(AdminSetting).where(AdminSetting.key == "security_question")
        ).scalar_one_or_none()
        answer_setting = db.execute(
            select(AdminSetting).where(AdminSetting.key == "security_answer_hash")
        ).scalar_one_or_none()
        if question_setting is None:
            db.add(
                AdminSetting(
                    key="security_question",
                    value=settings.admin_security_question.strip(),
                )
            )
        if answer_setting is None and settings.admin_initial_answer:
            db.add(
                AdminSetting(
                    key="security_answer_hash",
                    value=hash_password(normalize_security_answer(settings.admin_initial_answer)),
                )
            )


def backfill_captures() -> None:
    """把旧版本 jobs.created_by 迁移成 JobCapture。

    旧版本没有 capture 表，清空会直接删 Job；迁移后清空只影响当前用户。
    幂等：已有 capture 的 (user, job) 不重复写。
    """
    with session_scope() as db:
        jobs = db.execute(
            select(Job).where(Job.created_by.is_not(None)).order_by(Job.id.asc())
        ).scalars().all()
        by_user: dict[int, list[Job]] = {}
        existing = {(row.user_id, row.job_id) for row in db.execute(select(JobCapture)).scalars().all()}
        for job in jobs:
            if job.created_by is None:
                continue
            by_user.setdefault(int(job.created_by), []).append(job)
        for user_id, items in by_user.items():
            missing = [job for job in items if (user_id, job.id) not in existing]
            if not missing:
                continue
            batch = ImportBatch(
                user_id=user_id,
                source="legacy",
                mode="list",
                inserted=0,
                updated=len(missing),
                skipped=0,
                job_ids_json=[job.id for job in missing],
            )
            db.add(batch)
            db.flush()
            for job in missing:
                db.add(
                    JobCapture(
                        user_id=user_id,
                        job_id=job.id,
                        batch_id=batch.id,
                        captured_at=job.captured_at or utcnow(),
                    )
                )
        if by_user:
            logger.info("已补齐 %s 个用户的职位采集关系", len(by_user))


def bootstrap() -> None:
    """建表 + 管理员种子 + 旧职位 capture 迁移。"""
    from backend.database import init_db

    init_db()
    ensure_admin()
    backfill_captures()
