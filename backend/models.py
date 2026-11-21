# ============================================================
# ORM 模型：数据库结构就是产品的“事实来源”。
# 设计原则：
# 1) 用户、职位、分析、行为分表，避免一张宽表后期无法扩展；
# 2) 所有敏感凭据只存哈希，绝不存明文；
# 3) 职位指纹唯一，重复采集只更新不新增；
# 4) 分析结果带 profile_hash，画像一变就自动失效重算。
# ============================================================
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base


def utcnow() -> datetime:
    """统一用 UTC 存时间，展示时再转 Asia/Shanghai。

    为什么不用 datetime.utcnow()：它返回 naive 时间且在新版本被标记弃用，
    这里显式保留 UTC 语义后再去掉 tzinfo，兼容 MySQL DATETIME。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    """用户/管理员共用一张表，用 role 区分。

    管理员不单独建表的原因：登录、审计、会话逻辑完全一致，
    分表会复制三套代码；用 role + is_password_fixed 限制权限更简单。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user", index=True)
    # pending（待审核）/ active / rejected / disabled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    is_password_fixed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    avatar_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # 浏览器扩展采集令牌：只存 SHA-256，界面只显示一次明文。
    import_key_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    import_key_hint: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    review_note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    approved_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    profile: Mapped[Optional[Profile]] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    resumes: Mapped[list[Resume]] = relationship(back_populates="user", cascade="all, delete-orphan")
    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Profile(Base):
    """求职画像：字段允许为空，前端用 completeness 提示，而不是强制一次填完。"""

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )

    real_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    city: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    target_cities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    target_roles: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    job_type: Mapped[str] = mapped_column(String(16), nullable=False, default="全职")
    salary_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    experience: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    education: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    skills: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    industries: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    preferred_company_sizes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    bio: Mapped[str] = mapped_column(Text, nullable=False, default="")
    github: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    portfolio: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    completeness: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="profile")


class Resume(Base):
    """简历文件 + 解析结果。

    文件实体放磁盘，数据库只存相对路径和哈希：
    这样备份数据库不会膨胀，也避免把用户文件塞进 BLOB 导致慢查询。
    """

    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    extracted_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    parsed_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    user: Mapped[User] = relationship(back_populates="resumes")


class Job(Base):
    """职位主表：所有来源统一结构，source 保留“来自哪个网站”。"""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_job_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    url: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    company: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    city: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    district: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    salary_text: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    salary_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    job_type: Mapped[str] = mapped_column(String(16), nullable=False, default="", index=True)
    experience: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    education: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    industry: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    company_size: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    address: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requirements: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    raw_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    # 常用筛选组合索引：城市 + 来源 + 状态，避免每次筛选都全表扫。
    __table_args__ = (
        Index("ix_jobs_city_source_status", "city", "source", "status"),
    )


class ImportBatch(Base):
    """一次浏览器采集的批次记录。

    为什么单独建表：管理员需要按“每一次采集”查看/恢复；
    只靠 jobs.created_by 无法区分用户 A 的第 1 批和第 2 批。
    """

    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="list")
    inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    job_ids_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)


class JobCapture(Base):
    """“某用户采过某职位”的关系表。

    关键：职位本身可以全局共享；清空只清当前用户的可见性，
    不删除 Job，因此用户 B 不会因为 A 清空而丢失职位。
    """

    __tablename__ = "job_captures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    batch_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("import_batches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    # 非空 = 该用户已从自己的视图清空；管理员可恢复。
    cleared_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)

    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="uq_capture_user_job"),
        Index("ix_captures_user_cleared", "user_id", "cleared_at"),
    )


class RestoreRequest(Base):
    """用户申请恢复被自己清空的职位；管理员批准后恢复。"""

    __tablename__ = "restore_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_ids_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    reviewed_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class JobScreening(Base):
    """列表页 AI 初筛结果（一次请求筛 20 条，控制 token 成本）。"""

    __tablename__ = "job_screenings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    fit_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recommendation: Mapped[str] = mapped_column(String(16), nullable=False, default="stretch")
    reason: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    gaps_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    risks_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    is_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("job_id", "user_id", "profile_hash", name="uq_screening_job_user_profile"),
    )


class JobAnalysis(Base):
    """详情页两轮深度分析：第一轮结构化 JD，第二轮做画像匹配。"""

    __tablename__ = "job_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    fit_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recommendation: Mapped[str] = mapped_column(String(16), nullable=False, default="stretch")
    headline: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    pass1_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    pass2_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("job_id", "user_id", "profile_hash", name="uq_analysis_job_user_profile"),
    )


class Bookmark(Base):
    """收藏：联合唯一，防止同一用户重复收藏同一岗位。"""

    __tablename__ = "bookmarks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_bookmark_user_job"),)


class JobView(Base):
    """浏览历史：一条记录代表一次“打开详情”，用于历史页和浏览次数。"""

    __tablename__ = "job_views"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    viewed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)

    __table_args__ = (Index("ix_views_user_time", "user_id", "viewed_at"),)


class JobImpression(Base):
    """曝光记录：用于“换一批”去重；看过/被展示过都算，避免反复推同一批。"""

    __tablename__ = "job_impressions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    batch_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shown_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)

    __table_args__ = (Index("ix_impressions_user_time", "user_id", "shown_at"),)


class SearchBatch(Base):
    """一次“AI 搜索/换一批”的快照，方便计算与上一页的重复率。"""

    __tablename__ = "search_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    filters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    job_ids_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    overlap_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    exhausted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class Feedback(Base):
    """意见反馈：管理员可评级、回复；用户只能看自己的。"""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="功能建议")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="一般")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="待处理")
    admin_reply: Mapped[str] = mapped_column(Text, nullable=False, default="")
    replied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class PasswordResetRequest(Base):
    """忘记密码申请：管理员批准后生成一次性短码，用户凭码改密。"""

    __tablename__ = "password_reset_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    note: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    code_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class AuthSession(Base):
    """服务端会话：JWT 一旦泄露无法主动撤销，所以这里用随机令牌 + 数据库哈希。"""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    user_agent: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    user: Mapped[User] = relationship(back_populates="sessions")


class ApiUsage(Base):
    """LLM 调用账本：Token 统计是管理员页的核心数据，不依赖外部平台账单。"""

    __tablename__ = "api_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="deepseek")
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_estimate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)


class AuditLog(Base):
    """审计日志：只记录“谁做了什么”，不记录密码、Token 明文。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)


class AdminSetting(Base):
    """管理员可改的键值配置；目前存密保问题和答案哈希。"""

    __tablename__ = "admin_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
