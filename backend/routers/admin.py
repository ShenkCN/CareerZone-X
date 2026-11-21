# ============================================================
# 管理员路由：审批、密码重置、反馈、导出、系统监控、密保修改。
# 所有接口都挂 require_admin，普通用户即使拿到 URL 也返回 403。
# 管理员密码固定：这里刻意不提供“改密码”接口。
# ============================================================
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config import Settings, get_settings
from backend.database import get_db, session_scope
from backend.deps import get_client_ip, require_admin, write_audit
from backend.models import (
    AdminSetting,
    Bookmark,
    Feedback,
    ImportBatch,
    Job,
    JobCapture,
    JobView,
    PasswordResetRequest,
    RestoreRequest,
    Resume,
    User,
    utcnow,
)
from backend.schemas import (
    AdminUserOut,
    DashboardOut,
    FeedbackReplyIn,
    JobIdsIn,
    MessageOut,
    OfficialCollectIn,
    ResetApproveOut,
    ResetRequestOut,
    ReviewIn,
    SecurityQuestionIn,
    SystemOut,
)
from backend.security import (
    hash_password,
    new_reset_code,
    normalize_security_answer,
    verify_password,
)
from backend.services.export_service import create_export
from backend.services.job_service import job_to_dict, upsert_job
from backend.services.official_source import OfficialSourceError, fetch_official_jobs
from backend.services.stats_service import dashboard, recent_audit, system_info
from backend.services.vector_store import get_vector_store

router = APIRouter(prefix="/api/admin", tags=["管理员"])


def _reindex_all() -> None:
    """后台重建全部向量；个人数据量小，串行即可。"""
    try:
        with session_scope() as db:
            jobs = db.execute(select(Job).where(Job.status == "active")).scalars().all()
            get_vector_store().index_jobs(jobs)
    except Exception:
        # 索引失败不影响主业务，管理员页会显示 vector_count=-1。
        pass


@router.get("/dashboard", response_model=DashboardOut)
def admin_dashboard(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> DashboardOut:
    """管理员首页：用户/职位/反馈/Token/图表一次性返回。"""
    return DashboardOut(**dashboard(db))


@router.post("/official/collect")
def admin_official_collect(
    payload: OfficialCollectIn,
    background: BackgroundTasks,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """官方 API 预留入口；未配置时明确 503，不偷偷爬。"""
    try:
        items = fetch_official_jobs(
            settings,
            query=payload.query,
            city=payload.city,
            page=payload.page,
            page_size=payload.page_size,
        )
    except OfficialSourceError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    batch = ImportBatch(
        user_id=admin.id,
        source="official",
        mode="list",
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
    for item in items:
        try:
            job, created = upsert_job(db, item, user_id=admin.id)
            inserted += int(created)
            updated += int(not created)
            job_ids.append(job.id)
            capture = db.execute(
                select(JobCapture).where(
                    JobCapture.user_id == admin.id,
                    JobCapture.job_id == job.id,
                )
            ).scalar_one_or_none()
            if capture is None:
                db.add(JobCapture(user_id=admin.id, job_id=job.id, batch_id=batch.id, captured_at=utcnow()))
            else:
                capture.batch_id = batch.id
                capture.captured_at = utcnow()
                capture.cleared_at = None
        except ValueError:
            skipped += 1
    batch.inserted = inserted
    batch.updated = updated
    batch.skipped = skipped
    batch.job_ids_json = list(dict.fromkeys(job_ids))
    if job_ids:
        background.add_task(_reindex_all)
    write_audit(
        db,
        "admin.official_collect",
        actor_id=admin.id,
        target="official",
        detail={"batch_id": batch.id, "inserted": inserted, "updated": updated},
        ip=get_client_ip(request),
    )
    db.commit()
    return {
        "batch_id": batch.id,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "message": f"官方 API 采集完成：新增 {inserted}，更新 {updated}，跳过 {skipped}。",
    }


@router.get("/users", response_model=list[AdminUserOut])
def list_users(
    status_filter: str = Query(default="", alias="status"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[AdminUserOut]:
    """按状态筛选账号；管理员账号也列出，但不可被普通操作修改。"""
    query = select(User).order_by(User.created_at.desc())
    if status_filter:
        query = query.where(User.status == status_filter)
    users = db.execute(query.limit(500)).scalars().all()
    result = []
    for user in users:
        capture_count = int(
            db.scalar(select(func.count(JobCapture.id)).where(JobCapture.user_id == user.id)) or 0
        )
        cleared_count = int(
            db.scalar(
                select(func.count(JobCapture.id)).where(
                    JobCapture.user_id == user.id,
                    JobCapture.cleared_at.is_not(None),
                )
            )
            or 0
        )
        bookmark_count = int(
            db.scalar(select(func.count(Bookmark.id)).where(Bookmark.user_id == user.id)) or 0
        )
        view_count = int(
            db.scalar(select(func.count(JobView.id)).where(JobView.user_id == user.id)) or 0
        )
        result.append(
            AdminUserOut(
                id=user.id,
                username=user.username,
                display_name=user.display_name or user.username,
                role=user.role,
                status=user.status,
                created_at=user.created_at,
                approved_at=user.approved_at,
                last_login_at=user.last_login_at,
                review_note=user.review_note or "",
                job_captures=capture_count,
                job_cleared=cleared_count,
                bookmarks=bookmark_count,
                views=view_count,
                activity=capture_count + bookmark_count + view_count,
            )
        )
    return result


@router.get("/users/{user_id}/captures")
def admin_user_captures(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """管理员查看某用户每一次采集批次和对应岗位，用于恢复。"""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    batches = db.execute(
        select(ImportBatch)
        .where(ImportBatch.user_id == user_id)
        .order_by(ImportBatch.created_at.desc())
        .limit(100)
    ).scalars().all()
    rows = db.execute(
        select(JobCapture, Job)
        .join(Job, Job.id == JobCapture.job_id)
        .where(JobCapture.user_id == user_id)
        .order_by(JobCapture.captured_at.desc())
        .limit(1000)
    ).all()
    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name or user.username,
            "status": user.status,
            "created_at": user.created_at,
            "last_login_at": user.last_login_at,
        },
        "batches": [
            {
                "id": batch.id,
                "source": batch.source,
                "mode": batch.mode,
                "inserted": batch.inserted,
                "updated": batch.updated,
                "skipped": batch.skipped,
                "count": len(batch.job_ids_json or []),
                "created_at": batch.created_at,
            }
            for batch in batches
        ],
        "captures": [
            {
                "job_id": capture.job_id,
                "batch_id": capture.batch_id,
                "title": job.title,
                "company": job.company,
                "city": job.city,
                "source": job.source,
                "captured_at": capture.captured_at,
                "cleared_at": capture.cleared_at,
            }
            for capture, job in rows
        ],
    }


@router.post("/users/{user_id}/restore-jobs", response_model=MessageOut)
def admin_restore_jobs(
    user_id: int,
    payload: JobIdsIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MessageOut:
    """管理员恢复指定用户的指定职位（只解除该用户的清空标记）。"""
    user = db.get(User, user_id)
    if user is None or user.role != "user":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    ids = list(dict.fromkeys(payload.job_ids))
    captures = db.execute(
        select(JobCapture).where(
            JobCapture.user_id == user_id,
            JobCapture.job_id.in_(ids),
        )
    ).scalars().all()
    if not captures:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="没有找到该用户的采集记录")
    for capture in captures:
        capture.cleared_at = None
    write_audit(
        db,
        "admin.restore_jobs",
        actor_id=admin.id,
        target=f"user:{user_id}",
        detail={"jobs": ids},
        ip=get_client_ip(request),
    )
    db.commit()
    return MessageOut(message=f"已恢复 {len(captures)} 条职位给 {user.username}")


@router.get("/restore-requests")
def admin_restore_requests(
    status_filter: str = Query(default="pending", alias="status"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """用户提交的职位恢复申请。"""
    query = (
        select(RestoreRequest, User)
        .join(User, User.id == RestoreRequest.user_id)
        .order_by(RestoreRequest.created_at.desc())
    )
    if status_filter:
        query = query.where(RestoreRequest.status == status_filter)
    rows = db.execute(query.limit(300)).all()
    job_ids = sorted({int(item) for req, _ in rows for item in (req.job_ids_json or [])})
    jobs = {
        job.id: job
        for job in db.execute(select(Job).where(Job.id.in_(job_ids))).scalars().all()
    } if job_ids else {}
    return {
        "items": [
            {
                "id": req.id,
                "user_id": req.user_id,
                "username": user.username,
                "display_name": user.display_name or user.username,
                "job_ids": list(req.job_ids_json or []),
                "jobs": [
                    {
                        "id": job_id,
                        "title": jobs[job_id].title if job_id in jobs else f"职位 #{job_id}",
                        "company": jobs[job_id].company if job_id in jobs else "",
                    }
                    for job_id in (req.job_ids_json or [])
                ],
                "note": req.note,
                "status": req.status,
                "created_at": req.created_at,
                "reviewed_at": req.reviewed_at,
            }
            for req, user in rows
        ]
    }


@router.post("/restore-requests/{request_id}/approve", response_model=MessageOut)
def approve_restore_request(
    request_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MessageOut:
    req = db.get(RestoreRequest, request_id)
    if req is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="申请不存在")
    if req.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="申请已处理")
    ids = list(dict.fromkeys(int(item) for item in (req.job_ids_json or [])))
    captures = db.execute(
        select(JobCapture).where(
            JobCapture.user_id == req.user_id,
            JobCapture.job_id.in_(ids),
        )
    ).scalars().all()
    for capture in captures:
        capture.cleared_at = None
    req.status = "approved"
    req.reviewed_by = admin.id
    req.reviewed_at = utcnow()
    write_audit(
        db,
        "admin.restore_approve",
        actor_id=admin.id,
        target=f"restore:{req.id}",
        detail={"jobs": ids},
        ip=get_client_ip(request),
    )
    db.commit()
    return MessageOut(message=f"已批准恢复 {len(captures)} 条职位")


@router.post("/restore-requests/{request_id}/reject", response_model=MessageOut)
def reject_restore_request(
    request_id: int,
    payload: ReviewIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MessageOut:
    req = db.get(RestoreRequest, request_id)
    if req is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="申请不存在")
    if req.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="申请已处理")
    req.status = "rejected"
    req.note = (req.note + " | 管理员：" + payload.note)[:500]
    req.reviewed_by = admin.id
    req.reviewed_at = utcnow()
    write_audit(db, "admin.restore_reject", actor_id=admin.id, target=f"restore:{req.id}", ip=get_client_ip(request))
    db.commit()
    return MessageOut(message="已拒绝恢复申请")


@router.post("/users/{user_id}/approve", response_model=MessageOut)
def approve_user(
    user_id: int,
    payload: ReviewIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MessageOut:
    """批准注册；只允许 pending 用户，不能通过接口“批准”管理员。"""
    user = db.get(User, user_id)
    if user is None or user.role != "user":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if user.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该账号不在待审核状态")
    user.status = "active"
    user.approved_by = admin.id
    user.approved_at = utcnow()
    user.review_note = payload.note[:255]
    # 批准后把“催审批”反馈标记为已处理，管理员首页不再重复提醒。
    db.execute(
        Feedback.__table__.update()
        .where(
            Feedback.user_id == user.id,
            Feedback.category == "注册审批催促",
            Feedback.status == "待处理",
        )
        .values(status="已处理", admin_reply="账号已通过审核", replied_at=utcnow())
    )
    write_audit(db, "admin.user_approve", actor_id=admin.id, target=f"user:{user.id}", ip=get_client_ip(request))
    db.commit()
    return MessageOut(message="已通过注册审核")


@router.post("/users/{user_id}/reject", response_model=MessageOut)
def reject_user(
    user_id: int,
    payload: ReviewIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MessageOut:
    """拒绝注册；保留记录而不是删除，便于审计和申诉。"""
    user = db.get(User, user_id)
    if user is None or user.role != "user":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if user.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该账号不在待审核状态")
    user.status = "rejected"
    user.review_note = payload.note[:255]
    db.execute(
        Feedback.__table__.update()
        .where(
            Feedback.user_id == user.id,
            Feedback.category == "注册审批催促",
            Feedback.status == "待处理",
        )
        .values(status="已处理", admin_reply="注册申请已被拒绝", replied_at=utcnow())
    )
    user.approved_by = admin.id
    user.approved_at = utcnow()
    write_audit(db, "admin.user_reject", actor_id=admin.id, target=f"user:{user.id}", ip=get_client_ip(request))
    db.commit()
    return MessageOut(message="已拒绝该注册申请")


@router.get("/password-resets", response_model=list[ResetRequestOut])
def list_reset_requests(
    status_filter: str = Query(default="pending", alias="status"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[ResetRequestOut]:
    """忘记密码申请列表；默认只看 pending。"""
    query = (
        select(PasswordResetRequest, User)
        .join(User, User.id == PasswordResetRequest.user_id)
        .order_by(PasswordResetRequest.created_at.desc())
    )
    if status_filter:
        query = query.where(PasswordResetRequest.status == status_filter)
    rows = db.execute(query.limit(300)).all()
    return [
        ResetRequestOut(
            id=item.id,
            user_id=item.user_id,
            username=user.username,
            display_name=user.display_name or user.username,
            note=item.note or "",
            status=item.status,
            created_at=item.created_at,
            reviewed_at=item.reviewed_at,
        )
        for item, user in rows
    ]


@router.post("/password-resets/{request_id}/approve", response_model=ResetApproveOut)
def approve_reset(
    request_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> ResetApproveOut:
    """批准重置并生成 10 位一次性短码；短码只在这里显示一次。"""
    item = db.get(PasswordResetRequest, request_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="申请不存在")
    if item.status not in {"pending", "approved"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该申请已处理")
    raw, code_hash, expires_at = new_reset_code()
    item.status = "approved"
    item.code_hash = code_hash
    item.expires_at = expires_at
    item.reviewed_by = admin.id
    item.reviewed_at = utcnow()
    write_audit(db, "admin.reset_approve", actor_id=admin.id, target=f"reset:{item.id}", ip=get_client_ip(request))
    db.commit()
    return ResetApproveOut(
        request_id=item.id,
        code=raw,
        message="已批准。把短码线下告诉用户，30 分钟内有效；再次批准会生成新短码。",
    )


@router.post("/password-resets/{request_id}/reject", response_model=MessageOut)
def reject_reset(
    request_id: int,
    payload: ReviewIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MessageOut:
    """拒绝重置申请；不生成短码。"""
    item = db.get(PasswordResetRequest, request_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="申请不存在")
    if item.status not in {"pending", "approved"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该申请已处理")
    item.status = "rejected"
    item.reviewed_by = admin.id
    item.reviewed_at = utcnow()
    item.note = (item.note + " | 管理员备注：" + payload.note)[:255]
    write_audit(db, "admin.reset_reject", actor_id=admin.id, target=f"reset:{item.id}", ip=get_client_ip(request))
    db.commit()
    return MessageOut(message="已拒绝该重置申请")


@router.get("/feedback")
def admin_feedback(
    status_filter: str = Query(default="", alias="status"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """反馈列表带用户名；用户端不返回这个字段，避免暴露他人账号。"""
    query = (
        select(Feedback, User)
        .join(User, User.id == Feedback.user_id)
        .order_by(Feedback.created_at.desc())
    )
    if status_filter:
        query = query.where(Feedback.status == status_filter)
    rows = db.execute(query.limit(300)).all()
    return {
        "items": [
            {
                "id": item.id,
                "user_id": item.user_id,
                "username": user.username,
                "display_name": user.display_name or user.username,
                "category": item.category,
                "content": item.content,
                "rating": item.rating,
                "level": item.level,
                "status": item.status,
                "admin_reply": item.admin_reply,
                "created_at": item.created_at,
                "replied_at": item.replied_at,
            }
            for item, user in rows
        ]
    }


@router.patch("/feedback/{feedback_id}", response_model=MessageOut)
def reply_feedback(
    feedback_id: int,
    payload: FeedbackReplyIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MessageOut:
    """管理员评级 + 回复；用户下次打开设置页就能看到。"""
    item = db.get(Feedback, feedback_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="反馈不存在")
    if payload.level not in {"低", "一般", "重要", "紧急"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="评级必须是 低/一般/重要/紧急")
    item.level = payload.level
    item.admin_reply = payload.reply.strip()
    item.status = "已回复"
    item.replied_at = utcnow()
    write_audit(db, "admin.feedback_reply", actor_id=admin.id, target=f"feedback:{item.id}", ip=get_client_ip(request))
    db.commit()
    return MessageOut(message="反馈已回复")


@router.get("/system", response_model=SystemOut)
def admin_system(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SystemOut:
    """后端系统页：磁盘/数据库/文件/向量索引状态。"""
    return SystemOut(**system_info(db, settings))


@router.get("/audit")
def admin_audit(
    limit: int = Query(default=50, ge=1, le=300),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """最近审计事件；不返回密码、令牌等敏感字段。"""
    return {"items": recent_audit(db, limit)}


@router.get("/jobs")
def admin_jobs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """后台职位清单；用于检查采集质量和删除脏数据前的定位。"""
    total = int(db.scalar(select(func.count(Job.id))) or 0)
    jobs = db.execute(
        select(Job).order_by(Job.captured_at.desc()).offset(offset).limit(limit)
    ).scalars().all()
    return {
        "total": total,
        "items": [job_to_dict(job) for job in jobs],
    }


@router.post("/resumes/{resume_id}/activate", response_model=MessageOut)
def admin_rollback_resume(
    resume_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MessageOut:
    """管理员回档某用户的历史简历版本。"""
    item = db.get(Resume, resume_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="简历不存在")
    db.execute(Resume.__table__.update().where(Resume.user_id == item.user_id).values(is_active=False))
    item.is_active = True
    write_audit(db, "admin.resume_rollback", actor_id=admin.id, target=f"resume:{item.id}", ip=get_client_ip(request))
    db.commit()
    return MessageOut(message="已回档到该简历版本")


@router.get("/export")
def admin_export(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """生成并下载 ZIP；导出动作写审计，文件不长期暴露静态 URL。"""
    path, size = create_export(db, settings)
    write_audit(
        db,
        "admin.export",
        actor_id=admin.id,
        target="export",
        detail={"filename": path.name, "size": size},
        ip=get_client_ip(request),
    )
    db.commit()
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/zip",
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/reindex", response_model=MessageOut)
def reindex(
    background: BackgroundTasks,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MessageOut:
    """后台重建向量索引；首次会下载 bge 模型，接口不阻塞等待。"""
    background.add_task(_reindex_all)
    write_audit(db, "admin.reindex", actor_id=admin.id, target="jobs", ip=get_client_ip(request))
    db.commit()
    return MessageOut(message="已开始重建向量索引", detail="首次会下载中文 embedding 模型，完成后系统页的 vector_count 会增加。")


@router.get("/security-question")
def get_security_question(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """只返回问题，不返回答案哈希。"""
    row = db.execute(
        select(AdminSetting).where(AdminSetting.key == "security_question")
    ).scalar_one_or_none()
    return {"question": row.value if row else settings.admin_security_question}


@router.put("/security-question", response_model=MessageOut)
def update_security_question(
    payload: SecurityQuestionIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MessageOut:
    """修改密保问题/答案；必须再次验证当前管理员密码，防止会话被劫持后直接改。"""
    if not verify_password(payload.current_password, admin.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="当前管理员密码错误")
    question_row = db.execute(
        select(AdminSetting).where(AdminSetting.key == "security_question")
    ).scalar_one_or_none()
    answer_row = db.execute(
        select(AdminSetting).where(AdminSetting.key == "security_answer_hash")
    ).scalar_one_or_none()
    if question_row is None:
        question_row = AdminSetting(key="security_question", value=payload.question.strip())
        db.add(question_row)
    else:
        question_row.value = payload.question.strip()
    if answer_row is None:
        db.add(
            AdminSetting(
                key="security_answer_hash",
                value=hash_password(normalize_security_answer(payload.answer)),
            )
        )
    else:
        answer_row.value = hash_password(normalize_security_answer(payload.answer))
    write_audit(db, "admin.security_question_update", actor_id=admin.id, target=f"user:{admin.id}", ip=get_client_ip(request))
    db.commit()
    return MessageOut(message="密保问题已更新", detail="下次管理员登录请使用新答案。")
