# ============================================================
# Pydantic 契约：前端只依赖这些字段，后端改内部实现不影响 UI。
# 输入模型负责“拦脏数据”，输出模型负责“不泄露密码哈希等敏感列”。
# ============================================================
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 来源枚举同时用于前端展示和浏览器采集校验；新增来源必须同步这里与 URL 白名单。
SOURCE_LABELS = {
    "boss": "BOSS直聘",
    "liepin": "猎聘",
    "zhaopin": "智联招聘",
    "51job": "51JOB",
    "yupao": "鱼泡招聘",
    "official": "企业官网",
    "manual": "手动导入",
    "demo": "演示数据",
}
ALLOWED_SOURCES = set(SOURCE_LABELS)
ALLOWED_JOB_TYPES = {"全职", "兼职", "实习", "不限"}
ALLOWED_RECOMMENDATIONS = {"strong", "worth", "stretch", "skip"}


# ---------------------------- 认证 ----------------------------
class RegisterIn(BaseModel):
    # 账号规则也交给 validate_username，避免 Pydantic 先报英文/笼统错误。
    username: str = Field(min_length=1, max_length=32)
    # 密码规则交给路由里的 validate_password 统一报中文细节；
    # 这里不要用 min_length=8，否则短密码会先被 Pydantic 拦成“参数校验失败”。
    password: str = Field(min_length=1, max_length=200)
    display_name: str = Field(default="", max_length=64)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=20)
    password: str = Field(min_length=1, max_length=72)


class AdminVerifyIn(BaseModel):
    challenge_token: str = Field(min_length=10, max_length=512)
    answer: str = Field(min_length=1, max_length=128)


class NudgeIn(BaseModel):
    """待审核用户在注册页催管理员审批。"""

    username: str = Field(min_length=1, max_length=32)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    role: str
    status: str
    avatar_url: str = ""
    has_profile: bool = False
    profile_completeness: int = 0


class LoginOut(BaseModel):
    """登录返回统一结构：普通用户直接拿 token，管理员先拿 challenge。"""

    requires_2fa: bool = False
    challenge_token: Optional[str] = None
    question: Optional[str] = None
    access_token: Optional[str] = None
    token_type: str = "bearer"
    user: Optional[UserOut] = None


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=72)
    # 同样交给 validate_password，返回“缺大写/缺数字/长度”等具体原因。
    new_password: str = Field(min_length=1, max_length=200)


class ForgotPasswordIn(BaseModel):
    username: str = Field(min_length=1, max_length=20)
    note: str = Field(default="", max_length=255)


class ResetPasswordIn(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    code: str = Field(min_length=6, max_length=32)
    new_password: str = Field(min_length=1, max_length=200)


class MessageOut(BaseModel):
    message: str
    detail: str = ""


class UpdateDisplayNameIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)


# ---------------------------- 画像 ----------------------------
class ProfileIn(BaseModel):
    real_name: str = Field(default="", max_length=64)
    phone: str = Field(default="", max_length=32)
    email: str = Field(default="", max_length=128)
    city: str = Field(default="", max_length=64)
    target_cities: list[str] = Field(default_factory=list, max_length=12)
    target_roles: list[str] = Field(default_factory=list, max_length=12)
    job_type: str = Field(default="全职", max_length=16)
    salary_min: Optional[int] = Field(default=None, ge=0, le=100000)
    salary_max: Optional[int] = Field(default=None, ge=0, le=100000)
    experience: str = Field(default="", max_length=32)
    education: str = Field(default="", max_length=32)
    skills: list[str] = Field(default_factory=list, max_length=50)
    industries: list[str] = Field(default_factory=list, max_length=20)
    preferred_company_sizes: list[str] = Field(default_factory=list, max_length=10)
    bio: str = Field(default="", max_length=4000)
    github: str = Field(default="", max_length=255)
    portfolio: str = Field(default="", max_length=255)

    @field_validator("target_cities", "target_roles", "skills", "industries", "preferred_company_sizes")
    @classmethod
    def _clean_list(cls, values: list[str]) -> list[str]:
        # 去空、去重、限长；保留用户原始大小写（公司名/技能名可能大小写敏感）。
        seen: set[str] = set()
        cleaned: list[str] = []
        for value in values:
            text = str(value).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text[:64])
        return cleaned[:50]

    @field_validator("job_type")
    @classmethod
    def _check_job_type(cls, value: str) -> str:
        if value not in ALLOWED_JOB_TYPES:
            raise ValueError(f"求职类型必须是 {sorted(ALLOWED_JOB_TYPES)}")
        return value

    @model_validator(mode="after")
    def _check_salary(self):
        # 薪资区间允许只填一端；两端都填时最小值不能大于最大值。
        if self.salary_min is not None and self.salary_max is not None:
            if self.salary_min > self.salary_max:
                raise ValueError("最低薪资不能高于最高薪资")
        if self.email and "@" not in self.email:
            raise ValueError("邮箱格式不正确")
        return self


class ProfileOut(ProfileIn):
    model_config = ConfigDict(from_attributes=True)

    completeness: int = 0
    updated_at: Optional[datetime] = None


class ProfileBundle(BaseModel):
    profile: Optional[ProfileOut] = None
    display_name: str = ""
    avatar_url: str = ""
    completeness: int = 0


# ---------------------------- 简历 ----------------------------
class ResumeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    original_name: str
    mime_type: str
    size_bytes: int
    version: int
    is_active: bool
    created_at: datetime
    download_url: str = ""


class ResumeUploadOut(BaseModel):
    resume: ResumeOut
    parsed_profile: dict[str, Any] = Field(default_factory=dict)
    parse_mode: str = "rules"
    warning: str = ""


# ---------------------------- 职位 ----------------------------
class ScreeningOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fit_score: int
    recommendation: str
    reason: str
    gaps: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    model: str = ""
    is_fallback: bool = False


class AnalysisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fit_score: int
    recommendation: str
    headline: str
    pass1: dict[str, Any] = Field(default_factory=dict)
    pass2: dict[str, Any] = Field(default_factory=dict)
    model: str = ""
    prompt_version: str = ""
    is_fallback: bool = False
    created_at: Optional[datetime] = None


class JobOut(BaseModel):
    id: int
    source: str
    source_label: str
    url: str
    title: str
    company: str
    city: str
    district: str
    salary_text: str
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    job_type: str
    experience: str
    education: str
    industry: str
    company_size: str
    address: str
    tags: list[str] = Field(default_factory=list)
    view_count: int = 0
    captured_at: Optional[datetime] = None
    bookmarked: bool = False
    screening: Optional[ScreeningOut] = None


class JobDetailOut(BaseModel):
    job: JobOut
    description: str = ""
    requirements: str = ""
    analysis: Optional[AnalysisOut] = None


class JobSearchIn(BaseModel):
    keyword: str = Field(default="", max_length=100)
    source: str = Field(default="", max_length=32)
    city: str = Field(default="", max_length=64)
    job_type: str = Field(default="", max_length=16)
    salary_min: Optional[int] = Field(default=None, ge=0, le=100000)
    salary_max: Optional[int] = Field(default=None, ge=0, le=100000)
    experience: str = Field(default="", max_length=32)
    education: str = Field(default="", max_length=32)
    industry: str = Field(default="", max_length=128)
    company_size: str = Field(default="", max_length=64)
    offset: int = Field(default=0, ge=0, le=10000)
    exclude_ids: list[int] = Field(default_factory=list, max_length=500)
    screen: bool = True
    reset_exclusions: bool = False

    @field_validator("source")
    @classmethod
    def _check_source(cls, value: str) -> str:
        if value and value not in ALLOWED_SOURCES:
            raise ValueError("未知的职位来源")
        return value

    @field_validator("job_type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        if value and value not in ALLOWED_JOB_TYPES:
            raise ValueError("未知的求职类型")
        return value

    @model_validator(mode="after")
    def _check_salary(self):
        if self.salary_min is not None and self.salary_max is not None:
            if self.salary_min > self.salary_max:
                raise ValueError("薪资下限不能高于上限")
        return self


class JobSearchOut(BaseModel):
    items: list[JobOut]
    total: int
    offset: int
    limit: int
    overlap_rate: float = 0.0
    exhausted: bool = False
    batch_id: Optional[int] = None
    screen_status: str = "not_requested"
    screen_error: str = ""
    notice: str = ""
    profile_hash: str = ""


class ImportJobIn(BaseModel):
    """浏览器扩展/手动导入的单条职位；字段都可缺，服务端会补。"""

    source_job_id: str = Field(default="", max_length=128)
    url: str = Field(default="", max_length=1024)
    title: str = Field(default="", max_length=255)
    company: str = Field(default="", max_length=255)
    city: str = Field(default="", max_length=64)
    district: str = Field(default="", max_length=64)
    salary_text: str = Field(default="", max_length=128)
    job_type: str = Field(default="", max_length=16)
    experience: str = Field(default="", max_length=32)
    education: str = Field(default="", max_length=32)
    industry: str = Field(default="", max_length=128)
    company_size: str = Field(default="", max_length=64)
    address: str = Field(default="", max_length=512)
    description: str = Field(default="", max_length=30000)
    requirements: str = Field(default="", max_length=20000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    published_at: Optional[datetime] = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ImportRequest(BaseModel):
    source: str = Field(min_length=1, max_length=32)
    page_url: str = Field(default="", max_length=1024)
    mode: Literal["detail", "list"] = "detail"
    jobs: list[ImportJobIn] = Field(min_length=1, max_length=1000)

    @field_validator("source")
    @classmethod
    def _check_source(cls, value: str) -> str:
        if value not in ALLOWED_SOURCES:
            raise ValueError("未知的职位来源")
        return value


class ImportResult(BaseModel):
    inserted: int
    updated: int
    skipped: int
    job_ids: list[int]
    message: str


class ManualJobIn(BaseModel):
    source: str = Field(default="manual", max_length=32)
    url: str = Field(default="", max_length=1024)
    text: str = Field(min_length=30, max_length=30000)


class OfficialCollectIn(BaseModel):
    """官方 API 采集参数。"""

    query: str = Field(min_length=1, max_length=100)
    city: str = Field(default="", max_length=64)
    page: int = Field(default=1, ge=1, le=1000)
    page_size: int = Field(default=20, ge=1, le=200)


class JobIdsIn(BaseModel):
    """批量恢复/申请恢复的职位 ID 列表。"""

    job_ids: list[int] = Field(min_length=1, max_length=1000)
    note: str = Field(default="", max_length=500)


class SimilarJobOut(BaseModel):
    id: int
    title: str
    company: str
    city: str
    salary_text: str
    source: str


# ---------------------------- 行为 ----------------------------
class BookmarkOut(BaseModel):
    job_id: int
    bookmarked: bool
    message: str = ""


class HistoryItem(BaseModel):
    job: JobOut
    viewed_at: datetime


class ImportTokenOut(BaseModel):
    token: Optional[str] = None
    hint: str = ""
    message: str = ""
    required: bool = True


# ---------------------------- 反馈 ----------------------------
class FeedbackIn(BaseModel):
    category: str = Field(default="功能建议", max_length=32)
    content: str = Field(min_length=5, max_length=2000)
    rating: int = Field(default=5, ge=1, le=5)


class FeedbackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    content: str
    rating: int
    level: str
    status: str
    admin_reply: str
    created_at: datetime
    replied_at: Optional[datetime] = None


# ---------------------------- 管理员 ----------------------------
class ReviewIn(BaseModel):
    note: str = Field(default="", max_length=255)


class FeedbackReplyIn(BaseModel):
    level: str = Field(default="一般", max_length=16)
    reply: str = Field(min_length=1, max_length=2000)


class SecurityQuestionIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=72)
    question: str = Field(min_length=5, max_length=128)
    answer: str = Field(min_length=2, max_length=128)


class DashboardOut(BaseModel):
    counts: dict[str, Any]
    charts: dict[str, Any]
    top_jobs: list[dict[str, Any]]
    latest_feedback: list[dict[str, Any]]
    usage: dict[str, Any]
    generated_at: datetime


class AdminUserOut(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    status: str
    created_at: datetime
    approved_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None
    review_note: str = ""
    job_captures: int = 0
    job_cleared: int = 0
    bookmarks: int = 0
    views: int = 0
    activity: int = 0


class ResetRequestOut(BaseModel):
    id: int
    user_id: int
    username: str
    display_name: str
    note: str
    status: str
    created_at: datetime
    reviewed_at: Optional[datetime] = None


class ResetApproveOut(BaseModel):
    request_id: int
    code: Optional[str] = None
    message: str


class ExportOut(BaseModel):
    filename: str
    size_bytes: int
    download_url: str


class SystemOut(BaseModel):
    project_size_bytes: int
    storage_size_bytes: int
    database_size_bytes: int
    upload_count: int
    resume_count: int
    job_count: int
    user_count: int
    files: list[dict[str, Any]]
    db_backend: str
    vector_enabled: bool
    vector_count: int
    vector_backend: str = ""
