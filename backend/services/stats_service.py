# ============================================================
# 管理员统计 + 系统信息：所有数字都从数据库/磁盘现算，
# 不用前端硬编码，避免演示数据和真实数据混在一起。
# ============================================================
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.config import ROOT_DIR, Settings, get_settings
from backend.database import engine
from backend.models import (
    ApiUsage,
    AuditLog,
    Feedback,
    Job,
    JobAnalysis,
    JobScreening,
    JobView,
    Resume,
    User,
    utcnow,
)
from backend.services.text import source_label
from backend.services.vector_store import get_vector_store


def _safe_div(numerator: float, denominator: float) -> float:
    """除法兜底，避免空库时统计页出现 ZeroDivisionError。"""
    return float(numerator) / float(denominator) if denominator else 0.0


def _day_key(value: Any) -> str:
    """MySQL 的 func.date 可能返回 date/datetime/str，统一成 YYYY-MM-DD。"""
    if isinstance(value, (datetime,)):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def dashboard(db: Session) -> dict[str, Any]:
    """一次返回管理员首页所有卡片/图表需要的数据。"""
    now = utcnow()
    today = now.strftime("%Y-%m-%d")
    since = now - timedelta(days=13)

    total_users = int(db.scalar(select(func.count(User.id))) or 0)
    pending_users = int(db.scalar(select(func.count(User.id)).where(User.status == "pending")) or 0)
    total_jobs = int(db.scalar(select(func.count(Job.id)).where(Job.status == "active")) or 0)
    total_views = int(db.scalar(select(func.count(JobView.id))) or 0)
    total_feedback = int(db.scalar(select(func.count(Feedback.id))) or 0)
    pending_feedback = int(db.scalar(select(func.count(Feedback.id)).where(Feedback.status == "待处理")) or 0)
    pending_nudges = int(
        db.scalar(
            select(func.count(Feedback.id)).where(
                Feedback.category == "注册审批催促",
                Feedback.status == "待处理",
            )
        )
        or 0
    )

    total_tokens = int(db.scalar(select(func.coalesce(func.sum(ApiUsage.total_tokens), 0))) or 0)
    total_calls = int(db.scalar(select(func.count(ApiUsage.id))) or 0)
    failed_calls = int(db.scalar(select(func.count(ApiUsage.id)).where(ApiUsage.success.is_(False))) or 0)
    total_cost = float(db.scalar(select(func.coalesce(func.sum(ApiUsage.cost_estimate), 0.0))) or 0.0)
    avg_latency = float(db.scalar(select(func.coalesce(func.avg(ApiUsage.latency_ms), 0))) or 0)
    today_tokens = int(
        db.scalar(
            select(func.coalesce(func.sum(ApiUsage.total_tokens), 0)).where(
                func.date(ApiUsage.created_at) == today
            )
        )
        or 0
    )
    today_calls = int(
        db.scalar(
            select(func.count(ApiUsage.id)).where(func.date(ApiUsage.created_at) == today)
        )
        or 0
    )

    # 近 14 天趋势：先建完整日期轴，再填数据，图表不会缺日断线。
    usage_rows = db.execute(
        select(
            func.date(ApiUsage.created_at).label("day"),
            func.count(ApiUsage.id).label("calls"),
            func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("tokens"),
            func.coalesce(func.sum(ApiUsage.cost_estimate), 0.0).label("cost"),
        )
        .where(ApiUsage.created_at >= since)
        .group_by(func.date(ApiUsage.created_at))
    ).all()
    usage_map = {
        _day_key(row.day): {
            "calls": int(row.calls or 0),
            "tokens": int(row.tokens or 0),
            "cost": float(row.cost or 0.0),
        }
        for row in usage_rows
    }
    daily = []
    for offset in range(14):
        day = (since + timedelta(days=offset)).strftime("%Y-%m-%d")
        row = usage_map.get(day, {"calls": 0, "tokens": 0, "cost": 0.0})
        daily.append({"date": day, **row})

    source_rows = db.execute(
        select(Job.source, func.count(Job.id)).where(Job.status == "active").group_by(Job.source)
    ).all()
    source_distribution = [
        {"source": source, "label": source_label(source), "count": int(count)}
        for source, count in source_rows
    ]

    view_rows = db.execute(
        select(Job.source, func.count(JobView.id))
        .join(Job, Job.id == JobView.job_id)
        .group_by(Job.source)
    ).all()
    view_distribution = [
        {"source": source, "label": source_label(source), "count": int(count)}
        for source, count in view_rows
    ]

    rec_rows = db.execute(
        select(JobScreening.recommendation, func.count(JobScreening.id)).group_by(JobScreening.recommendation)
    ).all()
    recommendations = [
        {"recommendation": rec, "count": int(count)} for rec, count in rec_rows
    ]

    # 匹配分直方图：10 分一个桶，管理员能看出推荐质量分布。
    hist = {f"{start}-{start + 9}": 0 for start in range(0, 100, 10)}
    for score, in db.execute(select(JobScreening.fit_score)).all():
        bucket = min(90, int(score or 0) // 10 * 10)
        hist[f"{bucket}-{bucket + 9}"] += 1
    fit_histogram = [{"bucket": key, "count": value} for key, value in hist.items()]

    top_jobs = [
        {
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "source": source_label(job.source),
            "view_count": job.view_count,
        }
        for job in db.execute(select(Job).order_by(Job.view_count.desc(), Job.id.desc()).limit(6)).scalars()
    ]

    latest_feedback = [
        {
            "id": item.id,
            "category": item.category,
            "content": item.content[:120],
            "rating": item.rating,
            "status": item.status,
            "created_at": item.created_at,
        }
        for item in db.execute(select(Feedback).order_by(Feedback.created_at.desc()).limit(5)).scalars()
    ]

    return {
        "counts": {
            "users": total_users,
            "pending_users": pending_users,
            "jobs": total_jobs,
            "views": total_views,
            "feedback": total_feedback,
            "pending_feedback": pending_feedback,
            "pending_nudges": pending_nudges,
            "analyses": int(db.scalar(select(func.count(JobAnalysis.id))) or 0),
            "screenings": int(db.scalar(select(func.count(JobScreening.id))) or 0),
        },
        "charts": {
            "daily": daily,
            "sources": source_distribution,
            "views": view_distribution,
            "recommendations": recommendations,
            "fit_histogram": fit_histogram,
        },
        "top_jobs": top_jobs,
        "latest_feedback": latest_feedback,
        "usage": {
            "total_tokens": total_tokens,
            "total_calls": total_calls,
            "failed_calls": failed_calls,
            "success_rate": round(1 - _safe_div(failed_calls, total_calls), 4),
            "total_cost_estimate": round(total_cost, 6),
            "avg_latency_ms": int(avg_latency),
            "today_tokens": today_tokens,
            "today_calls": today_calls,
        },
        "generated_at": now,
    }


def _dir_size(path: Path, exclude: set[str] | None = None) -> int:
    """递归统计目录大小；跳过 .venv/缓存，管理员看到的是“项目本身”。"""
    exclude = exclude or set()
    total = 0
    if not path.exists():
        return 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [name for name in dirs if name not in exclude]
        for filename in files:
            file_path = Path(root) / filename
            try:
                total += file_path.stat().st_size
            except OSError:
                continue
    return total


def _database_size_bytes(db: Session) -> int:
    """MySQL 查 information_schema；SQLite 直接看文件大小。"""
    backend = engine.url.get_backend_name()
    try:
        if backend == "mysql":
            value = db.scalar(
                text(
                    "SELECT COALESCE(SUM(data_length + index_length), 0) "
                    "FROM information_schema.tables WHERE table_schema = DATABASE()"
                )
            )
            return int(value or 0)
        if backend == "sqlite":
            path = engine.url.database
            if path and path != ":memory:":
                return Path(path).stat().st_size if Path(path).exists() else 0
    except Exception:
        return 0
    return 0


def system_info(db: Session, settings: Settings | None = None) -> dict[str, Any]:
    """后端系统页数据：磁盘、数据库、上传文件清单。"""
    settings = settings or get_settings()
    project_size = _dir_size(ROOT_DIR, exclude={".venv", "__pycache__", ".git", ".pytest_cache", ".ruff_cache"})
    storage_size = _dir_size(settings.storage_dir)
    resumes = db.execute(select(Resume).order_by(Resume.created_at.desc())).scalars().all()
    files = [
        {
            "id": item.id,
            "kind": "resume",
            "user_id": item.user_id,
            "name": item.original_name,
            "size_bytes": item.size_bytes,
            "version": item.version,
            "is_active": item.is_active,
            "created_at": item.created_at,
            "path": item.stored_path,
        }
        for item in resumes
    ]
    # 头像文件也列出来，但数据库里只存路径，所以从磁盘反查。
    avatar_dir = settings.avatar_dir
    if avatar_dir.exists():
        for path in avatar_dir.rglob("*"):
            if path.is_file():
                files.append(
                    {
                        "id": 0,
                        "kind": "avatar",
                        "user_id": 0,
                        "name": path.name,
                        "size_bytes": path.stat().st_size,
                        "version": 1,
                        "is_active": True,
                        "created_at": datetime.fromtimestamp(path.stat().st_mtime),
                        "path": path.relative_to(settings.storage_dir).as_posix(),
                    }
                )
    vector = get_vector_store()
    # 系统页不能为了显示条数去触发模型初始化（本机可能卡在 DLL/下载）。
    # 只有已加载的集合才读 count；未加载显示 0，管理员点“重建索引”再初始化。
    vector_status = vector.status()
    vector_count = vector.count() if vector_status.get("loaded") else 0
    return {
        "project_size_bytes": project_size,
        "storage_size_bytes": storage_size,
        "database_size_bytes": _database_size_bytes(db),
        "upload_count": len(files),
        "resume_count": len(resumes),
        "job_count": int(db.scalar(select(func.count(Job.id))) or 0),
        "user_count": int(db.scalar(select(func.count(User.id))) or 0),
        "files": files[:500],
        "db_backend": engine.url.get_backend_name(),
        "vector_enabled": vector.configured,
        "vector_count": vector_count,
        "vector_backend": vector_status.get("backend", ""),
    }


def human_bytes(value: int) -> str:
    """把字节转成人类可读；前端/文档共用，避免各自写一套。"""
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def recent_audit(db: Session, limit: int = 50) -> list[dict[str, Any]]:
    """最近审计事件；只展示动作/对象/IP，不展示敏感细节。"""
    rows = db.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)).scalars()
    return [
        {
            "id": row.id,
            "actor_id": row.actor_id,
            "action": row.action,
            "target": row.target,
            "detail": row.detail_json,
            "ip": row.ip,
            "created_at": row.created_at,
        }
        for row in rows
    ]
