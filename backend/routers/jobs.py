# ============================================================
# 职位路由：搜索/筛选/换一批、详情、两轮分析、收藏、历史、导入。
# 重要边界：
# - 服务端不主动爬招聘网站；职位来自用户浏览器“点击采集”或手动导入；
# - 每次最多返回 1000 条；导入每批最多 100 条，避免变成批量抓取工具；
# - 换一批默认排除最近曝光，池子空了才重新列出并明确提示。
# ============================================================
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend.config import Settings, get_settings
from backend.database import get_db, session_scope
from backend.deps import (
    get_client_ip,
    get_current_user,
    get_user_by_import_key,
    require_user,
    write_audit,
)
from backend.models import AuditLog, ImportBatch, Job, JobAnalysis, JobCapture, RestoreRequest, User, utcnow
from backend.schemas import (
    AnalysisOut,
    BookmarkOut,
    ImportRequest,
    ImportResult,
    ImportTokenOut,
    JobDetailOut,
    JobIdsIn,
    JobOut,
    JobSearchIn,
    JobSearchOut,
    ManualJobIn,
    SimilarJobOut,
)
from backend.security import new_import_key
from backend.services.analysis_service import analysis_to_dict, deep_analyze, screen_jobs
from backend.services.job_service import (
    calculate_overlap,
    create_batch,
    get_bookmarked_ids,
    get_job,
    get_last_batch,
    is_bookmarked,
    job_to_dict,
    list_bookmarks,
    list_history,
    list_impression_ids,
    query_jobs,
    record_impressions,
    record_view,
    screening_map,
    toggle_bookmark,
    top_viewed_jobs,
    upsert_job,
)
from backend.services.llm import LLMError, LLMService
from backend.services.profile_service import (
    can_search,
    current_profile_hash,
    get_profile,
)
from backend.services.prompts import render_prompt
from backend.services.text import (
    allowed_host_for_source,
    clean_jd_text,
    detect_source_from_url,
    source_label,
)
from backend.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["职位"])


def _require_user_profile(db: Session, user: User):
    """职位业务只对普通用户开放，并且画像至少填了目标岗位/技能。"""
    if user.role != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请使用用户端访问")
    profile = get_profile(db, user)
    if not can_search(profile):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请先在“我的画像”填写目标岗位或技能，AI 才有判断依据",
        )
    return profile


def _analysis_row(db: Session, user_id: int, job_id: int, phash: str) -> Optional[JobAnalysis]:
    """取当前画像的深度分析缓存；画像变了自然查不到。"""
    if not phash:
        return None
    return db.execute(
        select(JobAnalysis).where(
            JobAnalysis.job_id == job_id,
            JobAnalysis.user_id == user_id,
            JobAnalysis.profile_hash == phash,
        )
    ).scalar_one_or_none()


def _index_jobs_background(job_ids: list[int]) -> None:
    """响应返回后再建向量索引，避免让用户等模型下载/编码。

    用独立 Session：BackgroundTasks 执行时，请求的 Session 可能已关闭。
    """
    if not job_ids:
        return
    try:
        with session_scope() as db:
            jobs = db.execute(select(Job).where(Job.id.in_(job_ids))).scalars().all()
            get_vector_store().index_jobs(jobs)
    except Exception:
        logger.warning("后台向量索引失败", exc_info=True)


@router.post("/jobs/search", response_model=JobSearchOut)
def search_jobs(
    payload: JobSearchIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JobSearchOut:
    """核心搜索：筛选 20 条 → 批量初筛 → 记录曝光/批次。"""
    profile = _require_user_profile(db, user)
    phash = current_profile_hash(profile)
    last_batch = get_last_batch(db, user.id)
    notice = ""
    excluded: list[int] = []

    if not payload.reset_exclusions:
        # 客户端 exclude_ids 是“本页内不想再看到”的即时排除；
        # impression 是服务端持久化的“已经展示过”，两者合并。
        excluded = list_impression_ids(db, user.id)
        excluded.extend(payload.exclude_ids)
        excluded = list(dict.fromkeys(excluded))

    jobs, total, exhausted = query_jobs(
        db,
        payload,
        user=user,
        profile=profile,
        profile_hash=phash,
        exclude_ids=excluded,
        limit=1000,
    )

    # 池子空了：去掉历史曝光再查一次，保证“除非实在没有了，就重新列出”。
    if not jobs and excluded and payload.offset == 0:
        jobs, total, _ = query_jobs(
            db,
            payload,
            user=user,
            profile=profile,
            profile_hash=phash,
            exclude_ids=[],
            limit=1000,
        )
        exhausted = True
        notice = "最近展示过的职位已看完，已重新列出较早的职位。"
    elif jobs and len(jobs) < 20 and exhausted:
        # 0 条时不要说“只剩这些”，否则界面会暗示“有岗但没显示”，非常误导。
        notice = "当前筛选条件下只剩这些职位，可以放宽城市/薪资条件或采集更多页面。"

    screen_status = "not_requested"
    screen_error = ""
    if payload.screen and jobs:
        screen_status, screen_error = screen_jobs(db, user, profile, jobs, LLMService(settings))

    scores = screening_map(db, user.id, [job.id for job in jobs], phash)
    bookmarked = get_bookmarked_ids(db, user.id, [job.id for job in jobs])
    items = [job_to_dict(job, scores.get(job.id), job.id in bookmarked) for job in jobs]

    overlap = calculate_overlap(last_batch, [job.id for job in jobs])
    batch = create_batch(db, user.id, payload, [job.id for job in jobs], overlap, exhausted)
    record_impressions(db, user.id, [job.id for job in jobs], batch.id)
    write_audit(
        db,
        "jobs.search",
        actor_id=user.id,
        target="jobs",
        detail={"count": len(jobs), "overlap": overlap, "screen": screen_status},
        ip=get_client_ip(request),
    )
    db.commit()

    return JobSearchOut(
        items=items,
        total=total,
        offset=payload.offset,
        limit=1000,
        overlap_rate=round(overlap, 4),
        exhausted=exhausted,
        batch_id=batch.id,
        screen_status=screen_status,
        screen_error=screen_error,
        profile_hash=phash,
        notice=notice,
    )


@router.get("/jobs/top-viewed")
def top_viewed(
    limit: int = Query(default=6, ge=1, le=20),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """浏览次数最高的岗位，给用户首页做“大家都在看”。"""
    if user.role != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请使用用户端访问")
    return {"items": top_viewed_jobs(db, user.id, limit)}


@router.get("/jobs/sources")
def job_sources(user: User = Depends(get_current_user)):
    """来源清单给前端下拉框，避免前端/后端各写一份中文映射。"""
    from backend.schemas import SOURCE_LABELS

    return {"items": [{"value": key, "label": value} for key, value in SOURCE_LABELS.items() if key != "demo"]}


@router.post("/jobs/import", response_model=ImportResult)
def import_jobs(
    payload: ImportRequest,
    background: BackgroundTasks,
    request: Request,
    user: User = Depends(get_user_by_import_key),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ImportResult:
    """浏览器扩展/官方适配器导入职位。

    扩展只在用户点击时读取当前页；服务端不提供“按关键词自动翻页”接口。
    """
    if payload.source == "demo":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="演示来源不允许通过导入接口写入")
    if len(payload.jobs) > settings.import_max_jobs:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"单次最多导入 {settings.import_max_jobs} 条职位",
        )
    if payload.page_url and not allowed_host_for_source(payload.source, payload.page_url):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="页面链接与来源不匹配")

    # 一次导入 = 一个批次；管理员可以按批次查看和恢复。
    batch = ImportBatch(
        user_id=user.id,
        source=payload.source,
        mode=payload.mode,
        inserted=0,
        updated=0,
        skipped=0,
        job_ids_json=[],
    )
    db.add(batch)
    db.flush()

    inserted = 0
    updated = 0
    skipped = 0
    job_ids: list[int] = []
    errors: list[str] = []
    for item in payload.jobs:
        data = item.model_dump()
        data["source"] = payload.source
        # 列表页可能没有详情链接，用页面 URL 兜底；但绝不把空链接伪造成职位链接。
        if not data.get("url") and payload.mode == "detail":
            data["url"] = payload.page_url
        if data.get("url") and not allowed_host_for_source(payload.source, data["url"]):
            skipped += 1
            errors.append("链接域名与来源不匹配")
            continue
        try:
            job, created = upsert_job(db, data, user_id=user.id)
            inserted += int(created)
            updated += int(not created)
            job_ids.append(job.id)
            # 记录“这个用户采过这个职位”；清空/恢复都作用在这一行上。
            capture = db.execute(
                select(JobCapture).where(
                    JobCapture.user_id == user.id,
                    JobCapture.job_id == job.id,
                )
            ).scalar_one_or_none()
            if capture is None:
                db.add(
                    JobCapture(
                        user_id=user.id,
                        job_id=job.id,
                        batch_id=batch.id,
                        captured_at=utcnow(),
                    )
                )
            else:
                capture.batch_id = batch.id
                capture.captured_at = utcnow()
                capture.cleared_at = None
        except ValueError as exc:
            skipped += 1
            errors.append(str(exc))
            continue

    batch.inserted = inserted
    batch.updated = updated
    batch.skipped = skipped
    batch.job_ids_json = list(dict.fromkeys(job_ids))
    if job_ids:
        background.add_task(_index_jobs_background, job_ids[:40])
    write_audit(
        db,
        "jobs.import",
        actor_id=user.id,
        target=f"source:{payload.source}",
        detail={
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "mode": payload.mode,
            "batch_id": batch.id,
        },
        ip=get_client_ip(request),
    )
    db.commit()
    message = f"采集完成：新增 {inserted}，更新 {updated}，跳过 {skipped}。"
    if errors:
        message += " 跳过原因：" + "；".join(errors[:3])
    return ImportResult(
        inserted=inserted,
        updated=updated,
        skipped=skipped,
        job_ids=job_ids,
        message=message,
    )


@router.post("/jobs/manual", response_model=JobOut)
def manual_import(
    payload: ManualJobIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JobOut:
    """粘贴一段 JD 文本，用 DeepSeek 抽取成职位；没有 Key 时明确拒绝而不是瞎猜。"""
    if user.role != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请使用用户端访问")
    llm = LLMService(settings)
    if not llm.available:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="未配置 DeepSeek，无法解析手动粘贴的职位")
    source = payload.source.strip().lower()
    if source not in {"manual", "official", "boss", "liepin", "zhaopin", "51job", "yupao"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="未知来源")
    if payload.url:
        source = detect_source_from_url(payload.url) if source == "manual" else source
        if not allowed_host_for_source(source, payload.url):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="链接域名与来源不匹配")
    try:
        result = llm.chat_json(
            system="你是招聘信息结构化抽取器，只输出 JSON。",
            user=render_prompt("job_extract", JOB_TEXT=payload.text[:20000]),
            endpoint="job_extract_manual",
            user_id=user.id,
            temperature=0.1,
            max_tokens=2600,
        )
    except LLMError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if not isinstance(result.data, dict):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="模型返回格式异常，请重试")
    data = result.data
    data.update(
        {
            "source": source,
            "url": payload.url,
            "description": payload.text,
            "raw": {"manual": True, "text": payload.text[:30000]},
        }
    )
    try:
        job, created = upsert_job(db, data, user_id=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    batch = ImportBatch(
        user_id=user.id,
        source=source,
        mode="detail",
        inserted=int(created),
        updated=int(not created),
        skipped=0,
        job_ids_json=[job.id],
    )
    db.add(batch)
    db.flush()
    capture = db.execute(
        select(JobCapture).where(
            JobCapture.user_id == user.id,
            JobCapture.job_id == job.id,
        )
    ).scalar_one_or_none()
    if capture is None:
        db.add(JobCapture(user_id=user.id, job_id=job.id, batch_id=batch.id, captured_at=utcnow()))
    else:
        capture.batch_id = batch.id
        capture.captured_at = utcnow()
        capture.cleared_at = None
    write_audit(
        db,
        "jobs.manual_import",
        actor_id=user.id,
        target=f"job:{job.id}",
        detail={"batch_id": batch.id},
        ip=get_client_ip(request),
    )
    db.commit()
    if created:
        _index_jobs_background([job.id])
    return JobOut(**job_to_dict(job, None, False))


@router.get("/jobs/cleared")
def cleared_jobs(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """当前用户已清空的职位（只影响自己的可见性）。"""
    rows = db.execute(
        select(JobCapture, Job)
        .join(Job, Job.id == JobCapture.job_id)
        .where(JobCapture.user_id == user.id, JobCapture.cleared_at.is_not(None))
        .order_by(desc(JobCapture.cleared_at), desc(JobCapture.id))
        .limit(1000)
    ).all()
    return {
        "items": [
            {
                "job": job_to_dict(job, None, False),
                "batch_id": capture.batch_id,
                "captured_at": capture.captured_at,
                "cleared_at": capture.cleared_at,
            }
            for capture, job in rows
        ]
    }


@router.post("/jobs/restore-request")
def create_restore_request(
    payload: JobIdsIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """用户申请恢复一个/几个/全部已清空职位，交给管理员审批。"""
    ids = list(dict.fromkeys(int(item) for item in payload.job_ids))
    rows = db.execute(
        select(JobCapture).where(
            JobCapture.user_id == user.id,
            JobCapture.job_id.in_(ids),
            JobCapture.cleared_at.is_not(None),
        )
    ).scalars().all()
    valid_ids = {row.job_id for row in rows}
    if len(valid_ids) != len(ids):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="部分职位不在你的可恢复列表中")
    req = RestoreRequest(
        user_id=user.id,
        job_ids_json=ids,
        note=payload.note.strip()[:500],
    )
    db.add(req)
    db.commit()
    return {"ok": True, "request_id": req.id, "message": f"已提交恢复申请，共 {len(ids)} 条。"}


@router.get("/import/token", response_model=ImportTokenOut)
def get_import_token(
    user: User = Depends(require_user),
    settings: Settings = Depends(get_settings),
) -> ImportTokenOut:
    """只返回提示片段；完整令牌只在生成时显示一次。"""
    return ImportTokenOut(
        hint=user.import_key_hint or "",
        message="完整令牌不会再次显示；如果丢失，请重新生成。" if user.import_key_hint else "尚未生成采集令牌。",
        required=settings.import_token_required,
    )


@router.post("/import/token", response_model=ImportTokenOut)
def create_import_token(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ImportTokenOut:
    """生成/轮换采集令牌；旧令牌立即失效。"""
    raw, hashed, hint = new_import_key()
    user.import_key_hash = hashed
    user.import_key_hint = hint
    write_audit(db, "import.token_create", actor_id=user.id, target=f"user:{user.id}", ip=get_client_ip(request))
    db.commit()
    return ImportTokenOut(
        token=raw,
        hint=hint,
        message="令牌只显示这一次，请立即填入扩展或密码管理器。",
        required=settings.import_token_required,
    )


@router.delete("/jobs/clear")
def clear_jobs(
    request: Request,
    user: User = Depends(get_user_by_import_key),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """清空该用户采集的职位；扩展弹窗二次确认后调用。"""
    # 清空前先备份 SQLite 主库；测试库不写生产 storage/backups。
    backup_name = ""
    try:
        bind = db.get_bind()
        if settings.app_env != "test" and bind.dialect.name == "sqlite" and bind.url.database:
            db_path = Path(bind.url.database).resolve()
            backup_dir = db_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"before_clear_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            with sqlite3.connect(db_path) as source, sqlite3.connect(backup_path) as target:
                source.backup(target)
            backup_name = backup_path.name
    except Exception:
        logger.warning("清空前数据库备份失败", exc_info=True)

    captures = db.execute(
        select(JobCapture).where(
            JobCapture.user_id == user.id,
            JobCapture.cleared_at.is_(None),
        )
    ).scalars().all()
    # 兼容极旧数据：没有 capture 时按 created_by 补关系，而不是直接删职位。
    if not captures:
        legacy_jobs = db.execute(select(Job).where(Job.created_by == user.id)).scalars().all()
        for job in legacy_jobs:
            existing = db.execute(
                select(JobCapture).where(
                    JobCapture.user_id == user.id,
                    JobCapture.job_id == job.id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                captures.append(existing)
            else:
                row = JobCapture(user_id=user.id, job_id=job.id, captured_at=utcnow())
                db.add(row)
                captures.append(row)
    cleared = 0
    for capture in captures:
        if capture.cleared_at is None:
            capture.cleared_at = utcnow()
            cleared += 1
    write_audit(
        db,
        "jobs.clear",
        actor_id=user.id,
        target="jobs",
        detail={"cleared": cleared, "mode": "per-user-hide"},
        ip=get_client_ip(request),
    )
    db.commit()
    message = (
        f"已从你的职位板清空 {cleared} 条；没有自己副本的用户也会看不到这批，"
        "自己采过的用户保留。管理员可在账号管理中恢复。"
    )
    if backup_name:
        message += f" 清空前备份：storage/backups/{backup_name}"
    return {"deleted": cleared, "message": message, "backup": backup_name, "hidden": cleared}


@router.get("/import/last")
def last_import(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """返回该用户最近一次扩展导入的统计，供职位板显示“采集结果”。"""
    row = db.execute(
        select(AuditLog)
        .where(
            AuditLog.actor_id == user.id,
            AuditLog.action == "jobs.import",
            # 用户 ID 可能被 SQLite 复用；只取该账号创建之后的采集记录。
            AuditLog.created_at >= user.created_at,
        )
        .order_by(desc(AuditLog.created_at), desc(AuditLog.id))
        .limit(1)
    ).scalars().first()
    if row is None:
        return {"found": False}
    detail = row.detail_json or {}
    inserted = int(detail.get("inserted", 0) or 0)
    updated = int(detail.get("updated", 0) or 0)
    skipped = int(detail.get("skipped", 0) or 0)
    total = inserted + updated
    return {
        "found": True,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "duplicate_rate": round(updated / total, 4) if total else 0.0,
        "mode": detail.get("mode", ""),
        "created_at": row.created_at,
    }


@router.get("/jobs/{job_id}", response_model=JobDetailOut)
def job_detail(
    job_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobDetailOut:
    """详情页：原 JD + 已有分析 + 收藏状态；浏览计数在这里发生。"""
    if user.role != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请使用用户端访问")
    job = get_job(db, job_id)
    if job is None or job.status != "active":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="职位不存在或已下线")
    profile = get_profile(db, user)
    phash = current_profile_hash(profile) if profile else ""
    screening = screening_map(db, user.id, [job.id], phash).get(job.id) if phash else None
    row = _analysis_row(db, user.id, job.id, phash)
    record_view(db, user, job)
    db.commit()
    return JobDetailOut(
        job=JobOut(**job_to_dict(job, screening, is_bookmarked(db, user.id, job.id))),
        description=clean_jd_text(job.description),
        requirements=clean_jd_text(job.requirements),
        analysis=AnalysisOut(**analysis_to_dict(row)) if row else None,
    )


@router.post("/jobs/{job_id}/analyze", response_model=AnalysisOut)
def analyze_job(
    job_id: int,
    request: Request,
    force: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AnalysisOut:
    """触发两轮深度分析；已缓存且画像未变时直接返回，不重复扣 token。"""
    if user.role != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请使用用户端访问")
    job = get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="职位不存在")
    profile = _require_user_profile(db, user)
    try:
        row = deep_analyze(db, user, profile, job, LLMService(settings), force=force)
    except LLMError as exc:
        db.rollback()
        code = status.HTTP_422_UNPROCESSABLE_ENTITY if exc.code == "jd_too_short" else status.HTTP_503_SERVICE_UNAVAILABLE
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    write_audit(db, "jobs.analyze", actor_id=user.id, target=f"job:{job.id}", ip=get_client_ip(request))
    db.commit()
    return AnalysisOut(**analysis_to_dict(row))


@router.post("/jobs/{job_id}/bookmark", response_model=BookmarkOut)
def bookmark_job(
    job_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BookmarkOut:
    """收藏切换；幂等性由数据库唯一约束保证。"""
    if user.role != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请使用用户端访问")
    job = get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="职位不存在")
    state = toggle_bookmark(db, user, job)
    db.commit()
    return BookmarkOut(job_id=job.id, bookmarked=state, message="已收藏" if state else "已取消收藏")


@router.get("/bookmarks", response_model=JobSearchOut)
def bookmarks(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=50),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobSearchOut:
    """收藏列表；保持和搜索页相同的 JobOut 结构，前端组件可复用。"""
    if user.role != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请使用用户端访问")
    profile = get_profile(db, user)
    phash = current_profile_hash(profile) if profile else ""
    items, total = list_bookmarks(db, user, phash, offset, limit)
    return JobSearchOut(items=items, total=total, offset=offset, limit=limit, screen_status="cached")


@router.get("/history")
def history(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=50),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """浏览历史；同一岗位只显示最近一次。"""
    if user.role != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请使用用户端访问")
    profile = get_profile(db, user)
    phash = current_profile_hash(profile) if profile else ""
    items, total = list_history(db, user, phash, offset, limit)
    return {"items": items, "total": total, "offset": offset, "limit": limit}


@router.get("/jobs/{job_id}/similar", response_model=list[SimilarJobOut])
def similar_jobs(
    job_id: int,
    limit: int = Query(default=5, ge=1, le=10),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[SimilarJobOut]:
    """Chroma 语义相似职位；向量不可用时退回 SQL 同城/标题关键词。"""
    if user.role != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请使用用户端访问")
    job = get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="职位不存在")
    found: list[Job] = []
    vector = get_vector_store()
    for hit in vector.query(f"{job.title} {job.company} {job.description[:1000]}", n_results=limit + 3):
        if hit["job_id"] == job.id:
            continue
        candidate = db.get(Job, hit["job_id"])
        if candidate is not None and candidate.status == "active":
            found.append(candidate)
        if len(found) >= limit:
            break
    if not found:
        # 没有索引时至少给同城/同来源的相近岗位，页面不会空着。
        candidates = db.execute(
            select(Job)
            .where(Job.id != job.id, Job.status == "active", Job.city == job.city)
            .order_by(desc(Job.captured_at))
            .limit(limit)
        ).scalars().all()
        found = list(candidates)
    return [
        SimilarJobOut(
            id=item.id,
            title=item.title,
            company=item.company,
            city=item.city,
            salary_text=item.salary_text,
            source=source_label(item.source),
        )
        for item in found[:limit]
    ]
