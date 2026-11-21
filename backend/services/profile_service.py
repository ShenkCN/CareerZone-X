# ============================================================
# 求职画像服务：完善度、序列化、检索文本、缓存哈希。
# 画像允许“填一半”，所以这里不做强制全量校验；只有目标岗位/技能
# 至少有一个时才允许发起职位匹配，否则 AI 没有判断依据。
# ============================================================
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Profile, User
from backend.services.text import profile_hash


def calculate_completeness(profile: Optional[Profile]) -> int:
    """按“对匹配最有用的字段”加权，而不是按字段数量平均。

    目标岗位/城市/薪资/技能权重最高；联系方式只占少量，
    因为 AI 匹配不需要手机号，填了也不应该显著提高完善度。
    """
    if profile is None:
        return 0
    score = 0
    if profile.target_roles:
        score += 20
    if profile.target_cities:
        score += 10
    if profile.city:
        score += 5
    if profile.salary_min or profile.salary_max:
        score += 10
    if profile.job_type:
        score += 5
    if profile.experience:
        score += 5
    if profile.education:
        score += 5
    if profile.skills:
        score += 15
    if profile.bio:
        score += 10
    if profile.real_name:
        score += 3
    if profile.phone:
        score += 2
    if profile.email:
        score += 2
    if profile.github or profile.portfolio:
        score += 3
    return max(0, min(100, score))


def ensure_profile(db: Session, user: User) -> Profile:
    """取画像；没有就建一条空记录并 flush（拿 id），由调用方统一 commit。"""
    profile = db.execute(select(Profile).where(Profile.user_id == user.id)).scalar_one_or_none()
    if profile is None:
        profile = Profile(user_id=user.id)
        db.add(profile)
        db.flush()
    return profile


def get_profile(db: Session, user: User) -> Optional[Profile]:
    """只读查询，不在 GET 请求里偷偷建数据。"""
    return db.execute(select(Profile).where(Profile.user_id == user.id)).scalar_one_or_none()


def profile_to_dict(profile: Optional[Profile]) -> dict[str, Any]:
    """把 ORM 转成 Pydantic 可吃的字典；缺画像时给前端一套空表单。"""
    if profile is None:
        return {
            "real_name": "",
            "phone": "",
            "email": "",
            "city": "",
            "target_cities": [],
            "target_roles": [],
            "job_type": "全职",
            "salary_min": None,
            "salary_max": None,
            "experience": "",
            "education": "",
            "skills": [],
            "industries": [],
            "preferred_company_sizes": [],
            "bio": "",
            "github": "",
            "portfolio": "",
            "completeness": 0,
            "updated_at": None,
        }
    return {
        "real_name": profile.real_name,
        "phone": profile.phone,
        "email": profile.email,
        "city": profile.city,
        "target_cities": list(profile.target_cities or []),
        "target_roles": list(profile.target_roles or []),
        "job_type": profile.job_type,
        "salary_min": profile.salary_min,
        "salary_max": profile.salary_max,
        "experience": profile.experience,
        "education": profile.education,
        "skills": list(profile.skills or []),
        "industries": list(profile.industries or []),
        "preferred_company_sizes": list(profile.preferred_company_sizes or []),
        "bio": profile.bio,
        "github": profile.github,
        "portfolio": profile.portfolio,
        "completeness": calculate_completeness(profile),
        "updated_at": profile.updated_at,
    }


def current_profile_hash(profile: Profile) -> str:
    """根据数据库当前值算哈希，保证和写入分析时用的是同一套字段。"""
    return profile_hash(profile_to_dict(profile))


def build_profile_text(profile: Profile) -> str:
    """拼成给 embedding / Prompt 用的自然语言画像。

    为什么不用 JSON：中文 embedding 对自然语言句子的语义更稳定，
    而且 Prompt 里人读也更清楚，便于排查“为什么推荐了这个岗位”。
    """
    parts = [
        f"目标岗位：{'、'.join(profile.target_roles or []) or '未填写'}",
        f"目标城市：{'、'.join(profile.target_cities or []) or profile.city or '不限'}",
        f"求职类型：{profile.job_type or '不限'}",
        f"期望月薪：{profile.salary_min or '?'}—{profile.salary_max or '?'} 元",
        f"工作经验：{profile.experience or '未填写'}",
        f"学历：{profile.education or '未填写'}",
        f"技能：{'、'.join(profile.skills or []) or '未填写'}",
        f"行业偏好：{'、'.join(profile.industries or []) or '不限'}",
        f"公司规模偏好：{'、'.join(profile.preferred_company_sizes or []) or '不限'}",
        f"自我介绍：{profile.bio or '未填写'}",
    ]
    return "\n".join(parts)


def can_search(profile: Optional[Profile]) -> bool:
    """是否具备最小检索条件。

    目标岗位和技能至少有一个：只填手机号/姓名不足以让 AI 找岗位。
    其他字段缺失没关系，模糊匹配会扩大召回。
    """
    if profile is None:
        return False
    return bool(profile.target_roles or profile.skills)


def merge_resume_draft(profile: Profile, draft: dict[str, Any]) -> bool:
    """把简历解析结果合并进画像，只填空白字段，绝不覆盖用户手填内容。

    返回是否发生变化，方便前端提示“已自动补全 N 项”。
    """
    changed = False
    simple_fields = ["real_name", "phone", "email", "city", "job_type", "experience", "education", "bio"]
    for field in simple_fields:
        value = draft.get(field)
        if isinstance(value, str) and value.strip() and not getattr(profile, field, ""):
            setattr(profile, field, value.strip()[:4000])
            changed = True

    list_fields = ["target_cities", "target_roles", "skills", "industries"]
    for field in list_fields:
        values = draft.get(field)
        if isinstance(values, list) and values:
            current = list(getattr(profile, field) or [])
            for item in values:
                text = str(item).strip()[:64]
                if text and text not in current:
                    current.append(text)
                    changed = True
            setattr(profile, field, current[:50])
    return changed
