# ============================================================
# 认证路由：注册审核、登录、管理员二次验证、忘记密码。
# 安全要点：
# - 登录成功/失败都写审计；
# - 管理员密码正确后还不算登录，必须再答密保；
# - 忘记密码不直接重置，必须管理员批准生成一次性短码。
# ============================================================
from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config import Settings, get_settings
from backend.database import get_db
from backend.deps import (
    bearer_scheme,
    get_client_ip,
    get_current_user,
    to_user_out,
    write_audit,
)
from backend.models import AdminSetting, AuthSession, Feedback, PasswordResetRequest, Profile, User, utcnow
from backend.schemas import (
    AdminVerifyIn,
    ChangePasswordIn,
    ForgotPasswordIn,
    LoginIn,
    LoginOut,
    MessageOut,
    NudgeIn,
    RegisterIn,
    ResetPasswordIn,
    UserOut,
)
from backend.security import (
    hash_password,
    make_admin_challenge,
    new_session_token,
    normalize_security_answer,
    read_admin_challenge,
    sha256_text,
    validate_password,
    validate_username,
    verify_password,
)
from backend.services.profile_service import calculate_completeness

router = APIRouter(prefix="/api/auth", tags=["认证"])

# 用户不存在时也跑一次 Argon2 校验，避免通过响应时间枚举账号是否存在。
_DUMMY_HASH = hash_password("Dummy-Password-For-Timing-Only-9")


def _user_out(db: Session, user: User) -> UserOut:
    """拼装前端需要的用户信息，附带画像完善度。"""
    profile = db.execute(select(Profile).where(Profile.user_id == user.id)).scalar_one_or_none()
    completeness = calculate_completeness(profile)
    return UserOut(**to_user_out(user, completeness))


def _create_session(db: Session, user: User, request: Request, settings: Settings) -> str:
    """创建服务端会话，返回给前端的明文令牌（数据库只存哈希）。"""
    raw, token_hash = new_session_token()
    hours = settings.session_hours_admin if user.role == "admin" else settings.session_hours_user
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=utcnow() + timedelta(hours=hours),
            ip=get_client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:255],
        )
    )
    return raw


def _generic_login_error() -> HTTPException:
    """账号/密码错误统一文案，不告诉攻击者“账号存在但密码错”。"""
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号或密码错误")


@router.post("/register", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, request: Request, db: Session = Depends(get_db)) -> MessageOut:
    """注册只创建 pending 账号，必须管理员批准后才能登录。"""
    try:
        username = validate_username(payload.username)
        password = validate_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    # MySQL 默认 collation 不区分大小写；SQLite 测试环境显式 lower 比较。
    exists = db.execute(
        select(User.id).where(func.lower(User.username) == username.lower())
    ).first()
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该账号已被占用")

    user = User(
        username=username,
        display_name=(payload.display_name or username).strip()[:64],
        password_hash=hash_password(password),
        role="user",
        status="pending",
    )
    db.add(user)
    db.flush()
    write_audit(db, "user.register", actor_id=user.id, target=f"user:{user.id}", ip=get_client_ip(request))
    db.commit()
    return MessageOut(message="注册申请已提交", detail="管理员审核通过后即可登录。")


@router.post("/nudge-approval", response_model=MessageOut)
def nudge_approval(payload: NudgeIn, request: Request, db: Session = Depends(get_db)) -> MessageOut:
    """待审核用户催管理员审批。

    这是公开接口（用户还没登录），但不泄露账号是否存在：
    无论账号存不存在，返回文案都保持克制；只有 pending 用户会真的写一条反馈。
    """
    user = db.execute(
        select(User).where(func.lower(User.username) == payload.username.strip().lower())
    ).scalar_one_or_none()
    if user is None or user.role != "user":
        return MessageOut(message="如果该账号正在等待审核，管理员会看到提醒")
    if user.status == "active":
        return MessageOut(message="该账号已通过审核，请直接登录")
    if user.status != "pending":
        return MessageOut(message="该账号当前不在待审核状态，请联系管理员")

    recent_since = utcnow() - timedelta(minutes=10)
    recent = db.execute(
        select(Feedback.id).where(
            Feedback.user_id == user.id,
            Feedback.category == "注册审批催促",
            Feedback.status == "待处理",
            Feedback.created_at >= recent_since,
        ).limit(1)
    ).first()
    if recent is not None:
        return MessageOut(message="已经提醒过管理员了，请稍等一会儿", detail="审核通过后即可登录。")

    db.add(
        Feedback(
            user_id=user.id,
            category="注册审批催促",
            content=f"用户 {user.username} 在注册页点击“催管理员审批”，希望尽快审核。",
            rating=5,
            level="重要",
            status="待处理",
        )
    )
    write_audit(
        db,
        "auth.nudge_approval",
        actor_id=user.id,
        target=f"user:{user.id}",
        ip=get_client_ip(request),
    )
    db.commit()
    return MessageOut(message="已提醒管理员", detail="管理员控制台会出现催办反馈；审核通过后即可登录。")


@router.post("/login", response_model=LoginOut)
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> LoginOut:
    """普通用户直接拿 token；管理员先拿 5 分钟挑战令牌。"""
    username = payload.username.strip()
    user = db.execute(
        select(User).where(func.lower(User.username) == username.lower())
    ).scalar_one_or_none()

    if user is None:
        # 不存在的账号也做一次哈希校验，响应时间与真实账号接近。
        verify_password(payload.password, _DUMMY_HASH)
        write_audit(db, "auth.login_failed", target=username[:64], ip=get_client_ip(request))
        db.commit()
        raise _generic_login_error()

    now = utcnow()
    if user.locked_until is not None and user.locked_until > now:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"失败次数过多，账号已锁定 {settings.login_lock_minutes} 分钟，请稍后再试。",
        )

    if not verify_password(payload.password, user.password_hash):
        user.failed_login_count = int(user.failed_login_count or 0) + 1
        if user.failed_login_count >= settings.login_max_attempts:
            user.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
            user.failed_login_count = 0
        write_audit(db, "auth.login_failed", actor_id=user.id, target=f"user:{user.id}", ip=get_client_ip(request))
        db.commit()
        raise _generic_login_error()

    if user.status != "active":
        detail = "账号正在等待管理员审核" if user.status == "pending" else "账号未通过审核或已被停用"
        write_audit(db, "auth.login_blocked", actor_id=user.id, target=f"user:{user.id}", ip=get_client_ip(request))
        db.commit()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now

    if user.role == "admin":
        # 管理员二次验证：密码正确只发挑战，不创建会话。
        question_row = db.execute(
            select(AdminSetting).where(AdminSetting.key == "security_question")
        ).scalar_one_or_none()
        question = question_row.value if question_row else settings.admin_security_question
        write_audit(db, "admin.password_ok", actor_id=user.id, target=f"user:{user.id}", ip=get_client_ip(request))
        db.commit()
        return LoginOut(
            requires_2fa=True,
            challenge_token=make_admin_challenge(settings, user.id),
            question=question,
        )

    token = _create_session(db, user, request, settings)
    write_audit(db, "auth.login", actor_id=user.id, target=f"user:{user.id}", ip=get_client_ip(request))
    db.commit()
    return LoginOut(access_token=token, user=_user_out(db, user))


@router.post("/admin/verify", response_model=LoginOut)
def admin_verify(
    payload: AdminVerifyIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> LoginOut:
    """管理员密保验证；挑战令牌 5 分钟有效，答错只返回通用错误。"""
    user_id = read_admin_challenge(settings, payload.challenge_token)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="验证已过期，请重新登录")

    user = db.get(User, user_id)
    if user is None or user.role != "admin" or user.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="管理员账号状态异常")

    answer_row = db.execute(
        select(AdminSetting).where(AdminSetting.key == "security_answer_hash")
    ).scalar_one_or_none()
    if answer_row is None or not verify_password(normalize_security_answer(payload.answer), answer_row.value):
        write_audit(db, "admin.2fa_failed", actor_id=user.id, target=f"user:{user.id}", ip=get_client_ip(request))
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="密保答案错误")

    token = _create_session(db, user, request, settings)
    user.last_login_at = utcnow()
    write_audit(db, "admin.login", actor_id=user.id, target=f"user:{user.id}", ip=get_client_ip(request))
    db.commit()
    return LoginOut(access_token=token, user=_user_out(db, user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> UserOut:
    """前端刷新后可用 token 恢复用户信息；token 失效会返回 401。"""
    return _user_out(db, user)


@router.post("/logout", response_model=MessageOut)
def logout(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> MessageOut:
    """撤销当前令牌；即使客户端没删 token，服务端也已失效。"""
    if credentials and credentials.credentials:
        token_hash = sha256_text(credentials.credentials)
        session = db.execute(
            select(AuthSession).where(AuthSession.token_hash == token_hash)
        ).scalar_one_or_none()
        if session:
            session.revoked = True
            db.commit()
    return MessageOut(message="已退出登录")


@router.post("/change-password", response_model=MessageOut)
def change_password(
    payload: ChangePasswordIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageOut:
    """修改密码后撤销该用户全部会话，强制所有端重新登录。"""
    if user.is_password_fixed or user.role == "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="管理员密码固定，不可修改")
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="原密码错误")
    try:
        new_password = validate_password(payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if verify_password(new_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="新密码不能与原密码相同")

    user.password_hash = hash_password(new_password)
    db.execute(
        AuthSession.__table__.update().where(AuthSession.user_id == user.id).values(revoked=True)
    )
    write_audit(db, "auth.change_password", actor_id=user.id, target=f"user:{user.id}", ip=get_client_ip(request))
    db.commit()
    return MessageOut(message="密码已修改", detail="所有设备已退出登录，请用新密码重新登录。")


@router.post("/forgot-password", response_model=MessageOut)
def forgot_password(
    payload: ForgotPasswordIn,
    request: Request,
    db: Session = Depends(get_db),
) -> MessageOut:
    """提交忘记密码申请；无论账号是否存在都返回同一句话，避免枚举。"""
    user = db.execute(
        select(User).where(func.lower(User.username) == payload.username.strip().lower())
    ).scalar_one_or_none()
    if user is not None and user.role == "user" and user.status == "active":
        existing = db.execute(
            select(PasswordResetRequest).where(
                PasswordResetRequest.user_id == user.id,
                PasswordResetRequest.status.in_(["pending", "approved"]),
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                PasswordResetRequest(
                    user_id=user.id,
                    note=payload.note.strip()[:255],
                    status="pending",
                )
            )
            write_audit(
                db,
                "auth.forgot_request",
                actor_id=user.id,
                target=f"user:{user.id}",
                ip=get_client_ip(request),
            )
            db.commit()
    return MessageOut(message="如果账号存在，申请已提交", detail="等待管理员批准后，用一次性短码设置新密码。")


@router.post("/reset-password", response_model=MessageOut)
def reset_password(
    payload: ResetPasswordIn,
    request: Request,
    db: Session = Depends(get_db),
) -> MessageOut:
    """用管理员批准后生成的一次性短码重置密码；短码 30 分钟过期。"""
    try:
        new_password = validate_password(payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    user = db.execute(
        select(User).where(func.lower(User.username) == payload.username.strip().lower())
    ).scalar_one_or_none()
    if user is None or user.role != "user":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="短码无效或已过期")

    request_row = db.execute(
        select(PasswordResetRequest)
        .where(
            PasswordResetRequest.user_id == user.id,
            PasswordResetRequest.status == "approved",
        )
        .order_by(PasswordResetRequest.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    now = utcnow()
    if (
        request_row is None
        or not request_row.code_hash
        or not request_row.expires_at
        or request_row.expires_at <= now
        or not secrets.compare_digest(sha256_text(payload.code.strip().upper()), request_row.code_hash)
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="短码无效或已过期")

    user.password_hash = hash_password(new_password)
    user.failed_login_count = 0
    user.locked_until = None
    request_row.status = "used"
    request_row.reviewed_at = now
    db.execute(
        AuthSession.__table__.update().where(AuthSession.user_id == user.id).values(revoked=True)
    )
    write_audit(db, "auth.reset_password", actor_id=user.id, target=f"user:{user.id}", ip=get_client_ip(request))
    db.commit()
    return MessageOut(message="密码已重置", detail="请用新密码登录。")
