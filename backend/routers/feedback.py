# ============================================================
# 用户反馈路由：提交功能建议/网站评价，查看自己的处理进度。
# 管理员回复在 admin.py，用户端只看不写回复。
# ============================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.deps import get_client_ip, get_current_user, write_audit
from backend.models import Feedback, User
from backend.schemas import FeedbackIn, FeedbackOut, MessageOut

router = APIRouter(prefix="/api/feedback", tags=["反馈"])


@router.post("", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
def create_feedback(
    payload: FeedbackIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageOut:
    """只允许普通用户提交；内容长度/评分由 Pydantic 先挡一层。"""
    if user.role != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请使用用户端提交反馈")
    item = Feedback(
        user_id=user.id,
        category=payload.category[:32],
        content=payload.content.strip(),
        rating=payload.rating,
        status="待处理",
    )
    db.add(item)
    db.flush()
    write_audit(db, "feedback.create", actor_id=user.id, target=f"feedback:{item.id}", ip=get_client_ip(request))
    db.commit()
    return MessageOut(message="反馈已提交", detail="管理员回复后会在这里看到。")


@router.get("/mine", response_model=list[FeedbackOut])
def my_feedback(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[FeedbackOut]:
    """用户只能看到自己的反馈，避免跨用户读取。"""
    if user.role != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请使用用户端访问")
    items = db.execute(
        select(Feedback).where(Feedback.user_id == user.id).order_by(Feedback.created_at.desc())
    ).scalars().all()
    return [FeedbackOut.model_validate(item) for item in items]
