# ============================================================
# FastAPI 依赖：认证、角色校验、审计。
# 为什么用依赖而不是在每个路由里复制：权限判断只写一次，
# 新增接口时只要写 Depends(require_admin) 就不会漏掉。
# ============================================================
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config import Settings, get_settings
from backend.database import get_db
from backend.models import AuditLog, AuthSession, User, utcnow
from backend.security import sha256_text

logger = logging.getLogger(__name__)

# auto_error=False：没有 Authorization 时返回我们自己的中文 401，而不是框架默认英文。
bearer_scheme = HTTPBearer(auto_error=False)


def get_client_ip(request: Request) -> str:
    """本机部署只信任 socket 对端地址。

    为什么不直接读 X-Forwarded-For：客户端可以伪造该头，
    用它做限流/审计会被绕过；将来放到 Nginx 后，再显式配置可信代理。
    """
    return (request.client.host if request.client else "")[:64]


def write_audit(
    db: Session,
    action: str,
    *,
    actor_id: Optional[int] = None,
    target: str = "",
    detail: Optional[dict[str, Any]] = None,
    ip: str = "",
) -> None:
    """只 add 不 commit：让审计和业务写在同一个事务里，避免“操作成功但日志丢了”。"""
    db.add(
        AuditLog(
            actor_id=actor_id,
            action=action[:64],
            target=target[:128],
            detail_json=detail or {},
            ip=ip[:64],
        )
    )


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """解析 Bearer 令牌 → 查会话哈希 → 返回用户。"""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")

    token_hash = sha256_text(credentials.credentials)
    row = db.execute(
        select(AuthSession, User)
        .join(User, User.id == AuthSession.user_id)
        .where(AuthSession.token_hash == token_hash)
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效，请重新登录")

    session, user = row
    now = utcnow()
    if session.revoked or session.expires_at <= now:
        # 过期会话顺手撤销，数据库不会无限增长；这里 commit 是安全的，因为只是清理。
        if not session.revoked:
            session.revoked = True
            db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期，请重新登录")

    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号未通过审核或已被停用")

    # 管理员固定会话只有 4 小时；用户会话 12 小时。这里再做一次角色一致性检查，
    # 防止数据库被手工改成 role=admin 后旧会话直接获得管理员权限。
    if user.role == "admin" and settings.app_env == "production" and not user.is_password_fixed:
        logger.error("管理员账号 %s 的固定密码标记异常", user.id)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="管理员配置异常，请联系维护者")

    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """管理员路由的硬闸门；普通用户即使知道 URL 也拿不到数据。"""
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅管理员可访问")
    return user


def require_user(user: User = Depends(get_current_user)) -> User:
    """业务路由只允许普通用户，管理员不占用用户端资源。"""
    if user.role != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请使用用户端访问")
    return user


def get_user_by_import_key(
    x_import_key: str = Header(default="", alias="X-Import-Key"),
    authorization: str = Header(default="", alias="Authorization"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """浏览器扩展鉴权：优先用当前登录会话 Cookie 里的 Bearer token。

    多用户时不能再用“第一个 active 用户”兜底；否则 A/B 的采集会混到同一个人。
    扩展从 8501 的 czx_session Cookie 读 token，作为 Authorization 发过来。
    """
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        token_hash = sha256_text(token)
        row = db.execute(
            select(AuthSession, User)
            .join(User, User.id == AuthSession.user_id)
            .where(AuthSession.token_hash == token_hash)
        ).first()
        if row is not None:
            session, user = row
            if not session.revoked and session.expires_at > utcnow() and user.status == "active":
                return user
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="采集登录态已失效，请重新登录网站")

    if settings.import_require_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="采集需要网站登录态，请先登录 CareerZone X")

    if not settings.import_token_required:
        # 先认令牌（如果扩展还带着旧令牌），再退回单用户归属。
        if x_import_key:
            key_hash = sha256_text(x_import_key)
            token_user = db.execute(
                select(User).where(User.import_key_hash == key_hash, User.status == "active")
            ).scalar_one_or_none()
            if token_user is not None:
                return token_user
        user = db.execute(
            select(User)
            .where(User.role == "user", User.status == "active")
            .order_by(User.id.asc())
            .limit(1)
        ).scalars().first()
        if user is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="没有可用的普通用户，请先注册并审核")
        return user

    if not x_import_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少采集令牌")
    key_hash = sha256_text(x_import_key)
    user = db.execute(
        select(User).where(User.import_key_hash == key_hash, User.status == "active")
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="采集令牌无效或已吊销")
    return user


def count_pending(db: Session) -> int:
    """管理员导航上的待办数；只数 pending，不把 rejected 混进去。"""
    return int(db.scalar(select(func.count(User.id)).where(User.status == "pending")) or 0)


def to_user_out(user: User, completeness: int = 0) -> dict[str, Any]:
    """把 ORM 用户转成安全输出；只拼可公开字段，绝不返回 password_hash。"""
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name or user.username,
        "role": user.role,
        "status": user.status,
        "avatar_url": "/api/profile/avatar" if user.avatar_path else "",
        "has_profile": bool(completeness),
        "profile_completeness": int(completeness or 0),
    }


def naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """把可能带时区的时间统一成 naive UTC，避免 MySQL 比较时报错。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
