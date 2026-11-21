# ============================================================
# 简历路由：上传 → 解析 → 保存版本 → 用户确认画像。
# 文件永远保存，解析失败也能下载；AI 只生成草稿，不直接覆盖画像。
# ============================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config import Settings, get_settings
from backend.database import get_db
from backend.deps import get_client_ip, get_current_user, write_audit
from backend.models import Resume, User
from backend.schemas import MessageOut, ResumeOut, ResumeUploadOut
from backend.services.llm import LLMService
from backend.services.resume_service import (
    ResumeError,
    extract_text,
    parse_resume,
    resolve_stored_path,
    save_upload,
)

router = APIRouter(prefix="/api/resumes", tags=["简历"])


def resume_to_out(item: Resume) -> ResumeOut:
    """统一输出；下载地址固定用 API 路由，前端不用拼磁盘路径。"""
    data = ResumeOut.model_validate(item)
    data.download_url = f"/api/resumes/{item.id}/download"
    return data


def activate_resume(db: Session, user_id: int, resume_id: int, actor_id: int) -> Resume:
    """把指定版本设为当前简历，其余版本取消 active。

    用“全量置 false 再置 true”而不是只改目标：避免历史脏数据出现多个 active。
    """
    target = db.execute(
        select(Resume).where(Resume.id == resume_id, Resume.user_id == user_id)
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="简历不存在")
    db.execute(
        Resume.__table__.update().where(Resume.user_id == user_id).values(is_active=False)
    )
    target.is_active = True
    write_audit(db, "resume.activate", actor_id=actor_id, target=f"resume:{resume_id}")
    db.flush()
    return target


@router.post("", response_model=ResumeUploadOut, status_code=status.HTTP_201_CREATED)
async def upload_resume(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ResumeUploadOut:
    """上传简历并生成画像草稿；返回 warning 时前端必须提示用户核对。"""
    if user.role != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="管理员不使用简历上传")
    try:
        absolute, relative, size, digest = await save_upload(file, user.id, settings)
    except ResumeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    warning = ""
    text = ""
    try:
        text = extract_text(absolute)
    except ResumeError as exc:
        warning = f"{exc}；文件已保存，可下载查看。"

    draft: dict = {}
    parse_mode = "rules"
    if text:
        llm = LLMService(settings)
        draft, parse_mode, parse_warning = parse_resume(text, llm=llm, user_id=user.id)
        warning = "；".join(part for part in (warning, parse_warning) if part)
    else:
        warning = warning or "未提取到文字，请手动填写画像。"

    # 版本号按用户维度递增；先算再插，避免并发下用 count 造成重复版本（个人使用足够）。
    max_version = int(
        db.scalar(select(func.max(Resume.version)).where(Resume.user_id == user.id)) or 0
    )
    db.execute(
        Resume.__table__.update().where(Resume.user_id == user.id).values(is_active=False)
    )
    resume = Resume(
        user_id=user.id,
        original_name=(file.filename or "resume")[:255],
        stored_path=relative,
        mime_type=(file.content_type or "")[:128],
        size_bytes=size,
        sha256=digest,
        version=max_version + 1,
        is_active=True,
        extracted_text=text,
        parsed_json=draft or {},
    )
    db.add(resume)
    db.flush()
    write_audit(
        db,
        "resume.upload",
        actor_id=user.id,
        target=f"resume:{resume.id}",
        detail={"size": size, "parse_mode": parse_mode},
        ip=get_client_ip(request),
    )
    db.commit()
    db.refresh(resume)
    return ResumeUploadOut(
        resume=resume_to_out(resume),
        parsed_profile=draft,
        parse_mode=parse_mode,
        warning=warning,
    )


@router.get("", response_model=list[ResumeOut])
def list_resumes(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[ResumeOut]:
    """用户只看自己的简历版本；管理员走后台接口。"""
    if user.role != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请使用管理员后台")
    items = db.execute(
        select(Resume).where(Resume.user_id == user.id).order_by(Resume.created_at.desc())
    ).scalars().all()
    return [resume_to_out(item) for item in items]


@router.get("/{resume_id}/download")
def download_resume(
    resume_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """下载自己的简历；管理员可通过后台接口下载任意用户简历。"""
    item = db.get(Resume, resume_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="简历不存在")
    if user.role != "admin" and item.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该简历")
    path = resolve_stored_path(settings, item.stored_path)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件已丢失，请联系管理员")
    return FileResponse(
        path,
        filename=item.original_name,
        media_type=item.mime_type or "application/octet-stream",
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/{resume_id}/activate", response_model=MessageOut)
def activate(
    resume_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageOut:
    """用户自己回档历史简历版本。"""
    if user.role != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请使用管理员后台")
    activate_resume(db, user.id, resume_id, user.id)
    write_audit(db, "resume.activate_by_user", actor_id=user.id, target=f"resume:{resume_id}", ip=get_client_ip(request))
    db.commit()
    return MessageOut(message="已设为当前简历")
