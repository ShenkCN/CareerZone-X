# ============================================================
# 数据打包：管理员可导出 CSV + manifest 的 ZIP。
# 安全取舍：不导出密码哈希/会话/采集令牌哈希；简历文件不塞进 ZIP，
# 避免一次误发把全部候选人附件带走；附件在系统页单独下载。
# ============================================================
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import Settings, get_settings
from backend.models import (
    ApiUsage,
    AuditLog,
    Feedback,
    Job,
    JobAnalysis,
    JobScreening,
    Profile,
    Resume,
    User,
    utcnow,
)
from backend.services.stats_service import dashboard, human_bytes, system_info


def _csv_bytes(rows: Iterable[dict[str, Any]], headers: list[str]) -> bytes:
    """生成 Excel 友好的 UTF-8 BOM CSV；中文直接双击打开不乱码。"""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in headers})
    return buffer.getvalue().encode("utf-8-sig")


def _jsonable(value: Any) -> Any:
    """把日期/嵌套对象转成可写 JSON 的形式。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, dict, str, int, float, bool)) or value is None:
        return value
    return str(value)


def _dump_json_file(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=_jsonable), encoding="utf-8")


def create_export(db: Session, settings: Settings | None = None) -> tuple[Path, int]:
    """生成 ZIP，返回 (路径, 大小)。文件名带时间戳，避免覆盖旧包。"""
    settings = settings or get_settings()
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d_%H%M%S")
    path = settings.export_dir / f"careerzone_export_{stamp}.zip"

    users = [
        {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "status": user.status,
            "created_at": user.created_at,
            "approved_at": user.approved_at,
            "last_login_at": user.last_login_at,
        }
        for user in db.execute(select(User).order_by(User.id)).scalars()
    ]
    profiles = [
        {
            "user_id": profile.user_id,
            "real_name": profile.real_name,
            "phone": profile.phone,
            "email": profile.email,
            "city": profile.city,
            "target_cities": profile.target_cities,
            "target_roles": profile.target_roles,
            "job_type": profile.job_type,
            "salary_min": profile.salary_min,
            "salary_max": profile.salary_max,
            "experience": profile.experience,
            "education": profile.education,
            "skills": profile.skills,
            "industries": profile.industries,
            "completeness": profile.completeness,
            "updated_at": profile.updated_at,
        }
        for profile in db.execute(select(Profile).order_by(Profile.user_id)).scalars()
    ]
    jobs = [
        {
            "id": job.id,
            "source": job.source,
            "title": job.title,
            "company": job.company,
            "city": job.city,
            "salary_text": job.salary_text,
            "job_type": job.job_type,
            "experience": job.experience,
            "education": job.education,
            "industry": job.industry,
            "company_size": job.company_size,
            "url": job.url,
            "view_count": job.view_count,
            "captured_at": job.captured_at,
        }
        for job in db.execute(select(Job).order_by(Job.id)).scalars()
    ]
    screenings = [
        {
            "job_id": item.job_id,
            "user_id": item.user_id,
            "fit_score": item.fit_score,
            "recommendation": item.recommendation,
            "reason": item.reason,
            "gaps": item.gaps_json,
            "risks": item.risks_json,
            "model": item.model,
            "is_fallback": item.is_fallback,
            "created_at": item.created_at,
        }
        for item in db.execute(select(JobScreening).order_by(JobScreening.id)).scalars()
    ]
    analyses = [
        {
            "job_id": item.job_id,
            "user_id": item.user_id,
            "fit_score": item.fit_score,
            "recommendation": item.recommendation,
            "headline": item.headline,
            "pass1": item.pass1_json,
            "pass2": item.pass2_json,
            "model": item.model,
            "tokens_in": item.tokens_in,
            "tokens_out": item.tokens_out,
            "created_at": item.created_at,
        }
        for item in db.execute(select(JobAnalysis).order_by(JobAnalysis.id)).scalars()
    ]
    feedback = [
        {
            "id": item.id,
            "user_id": item.user_id,
            "category": item.category,
            "content": item.content,
            "rating": item.rating,
            "level": item.level,
            "status": item.status,
            "admin_reply": item.admin_reply,
            "created_at": item.created_at,
        }
        for item in db.execute(select(Feedback).order_by(Feedback.id)).scalars()
    ]
    usage = [
        {
            "id": item.id,
            "user_id": item.user_id,
            "model": item.model,
            "endpoint": item.endpoint,
            "prompt_tokens": item.prompt_tokens,
            "completion_tokens": item.completion_tokens,
            "total_tokens": item.total_tokens,
            "cost_estimate": item.cost_estimate,
            "latency_ms": item.latency_ms,
            "success": item.success,
            "created_at": item.created_at,
        }
        for item in db.execute(select(ApiUsage).order_by(ApiUsage.id)).scalars()
    ]
    audit = [
        {
            "id": item.id,
            "actor_id": item.actor_id,
            "action": item.action,
            "target": item.target,
            "detail": item.detail_json,
            "ip": item.ip,
            "created_at": item.created_at,
        }
        for item in db.execute(select(AuditLog).order_by(AuditLog.id)).scalars()
    ]
    resumes = [
        {
            "id": item.id,
            "user_id": item.user_id,
            "original_name": item.original_name,
            "size_bytes": item.size_bytes,
            "version": item.version,
            "is_active": item.is_active,
            "sha256": item.sha256,
            "created_at": item.created_at,
        }
        for item in db.execute(select(Resume).order_by(Resume.id)).scalars()
    ]

    stats = dashboard(db)
    sysinfo = system_info(db, settings)
    readme = f"""CareerZone X 数据导出包
生成时间：{utcnow().isoformat()}Z
数据库：{sysinfo["db_backend"]}
用户数：{sysinfo["user_count"]}
职位数：{sysinfo["job_count"]}
项目占用：{human_bytes(sysinfo["project_size_bytes"])}
存储占用：{human_bytes(sysinfo["storage_size_bytes"])}

文件说明：
- users.csv / profiles.csv：账号与画像（含 PII，请妥善保管）
- jobs.csv / screenings.csv / analyses.csv：职位与 AI 分析
- feedback.csv / usage.csv / audit.csv：反馈、Token 账本、审计
- resumes.csv：简历元数据；原始 PDF/DOCX 未打包，需在管理员系统页单独下载
- dashboard.json：统计快照
- README.txt：本说明

安全提示：本包含个人资料，不要上传网盘/发给无关人员；导出完成后可删除。
"""
    manifest = {
        "app": "CareerZone X",
        "version": "0.4.0",
        "generated_at": utcnow().isoformat() + "Z",
        "files": [
            "users.csv", "profiles.csv", "jobs.csv", "screenings.csv", "analyses.csv",
            "feedback.csv", "usage.csv", "audit.csv", "resumes.csv", "dashboard.json", "README.txt",
        ],
        "counts": stats["counts"],
        "usage": stats["usage"],
    }

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("users.csv", _csv_bytes(users, ["id", "username", "display_name", "role", "status", "created_at", "approved_at", "last_login_at"]))
        archive.writestr("profiles.csv", _csv_bytes(profiles, ["user_id", "real_name", "phone", "email", "city", "target_cities", "target_roles", "job_type", "salary_min", "salary_max", "experience", "education", "skills", "industries", "completeness", "updated_at"]))
        archive.writestr("jobs.csv", _csv_bytes(jobs, ["id", "source", "title", "company", "city", "salary_text", "job_type", "experience", "education", "industry", "company_size", "url", "view_count", "captured_at"]))
        archive.writestr("screenings.csv", _csv_bytes(screenings, ["job_id", "user_id", "fit_score", "recommendation", "reason", "gaps", "risks", "model", "is_fallback", "created_at"]))
        archive.writestr("analyses.csv", _csv_bytes(analyses, ["job_id", "user_id", "fit_score", "recommendation", "headline", "pass1", "pass2", "model", "tokens_in", "tokens_out", "created_at"]))
        archive.writestr("feedback.csv", _csv_bytes(feedback, ["id", "user_id", "category", "content", "rating", "level", "status", "admin_reply", "created_at"]))
        archive.writestr("usage.csv", _csv_bytes(usage, ["id", "user_id", "model", "endpoint", "prompt_tokens", "completion_tokens", "total_tokens", "cost_estimate", "latency_ms", "success", "created_at"]))
        archive.writestr("audit.csv", _csv_bytes(audit, ["id", "actor_id", "action", "target", "detail", "ip", "created_at"]))
        archive.writestr("resumes.csv", _csv_bytes(resumes, ["id", "user_id", "original_name", "size_bytes", "version", "is_active", "sha256", "created_at"]))
        archive.writestr("dashboard.json", json.dumps(stats, ensure_ascii=False, default=_jsonable, indent=2).encode("utf-8"))
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        archive.writestr("README.txt", readme.encode("utf-8"))

    return path, path.stat().st_size
