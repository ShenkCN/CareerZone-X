# ============================================================
# 本地匹配规则：AI 不可用时的兜底，也是列表预排序的基线。
# 重要：规则分不等于 AI 分，前端会明确标注 is_fallback，
# 不能让用户误以为“没调模型”也是 DeepSeek 的判断。
# ============================================================
from __future__ import annotations

import re
from typing import Any, Optional

from backend.models import Job, Profile
from backend.services.text import clean_text

# 学历从低到高；用于判断岗位要求是否高于用户学历。
_EDU_ORDER = {"不限": 0, "高中": 1, "中专": 1, "大专": 2, "本科": 3, "硕士": 4, "博士": 5}
_SENIOR_WORDS = ("总监", "经理", "主管", "专家", "资深", "架构师", "负责人", "5-10年", "10年以上")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]{1,20}|[\u4e00-\u9fff]{2,8}")


def _edu_level(text: str) -> int:
    """从“本科及以上”等文本中取最高学历等级；识别不了当 0（不限）。"""
    for key, level in sorted(_EDU_ORDER.items(), key=lambda item: -item[1]):
        if key and key in (text or ""):
            return level
    return 0


def _keywords(profile: Profile) -> list[str]:
    """把目标岗位/技能拆成可匹配关键词，去重并限制数量。"""
    values: list[str] = []
    for item in list(profile.target_roles or []) + list(profile.skills or []):
        text = str(item).strip()
        if not text:
            continue
        values.append(text.lower())
        # 长中文岗位名再拆 2—4 字片段，提升“AI应用交付”匹配“AI交付”的召回。
        if re.fullmatch(r"[\u4e00-\u9fff]{4,}", text):
            for size in (4, 3, 2):
                for idx in range(0, min(len(text) - size + 1, 6)):
                    values.append(text[idx:idx + size])
    # 去重但保持顺序，避免 set 导致提示词顺序随机。
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result[:60]


def _job_text(job: Job) -> str:
    """所有可匹配字段拼成一篇文章；截断防止超长文本拖慢规则计算。"""
    return " ".join(
        [
            job.title or "",
            job.company or "",
            job.city or "",
            job.description or "",
            job.requirements or "",
            " ".join(job.tags or []),
        ]
    ).lower()[:12000]


def heuristic_screen(profile: Optional[Profile], job: Job) -> dict[str, Any]:
    """返回与 JobScreening 兼容的字典：分数、建议、理由、缺口、风险。"""
    if profile is None:
        return {
            "fit_score": 0,
            "recommendation": "skip",
            "reason": "尚未填写求职画像",
            "gaps": ["请先填写目标岗位或技能"],
            "risks": [],
            "is_fallback": True,
        }

    text = _job_text(job)
    score = 42
    matched: list[str] = []
    gaps: list[str] = []
    risks: list[str] = []

    # 1) 岗位名/技能命中：权重最高，因为这是“你想做什么”的直接信号。
    hits = [keyword for keyword in _keywords(profile) if keyword and keyword in text]
    if hits:
        score += min(28, 8 + len(hits) * 3)
        matched.append("关键词命中：" + "、".join(hits[:5]))
    else:
        score -= 8
        gaps.append("岗位描述与目标岗位/技能的直接重合较少")

    # 2) 城市：目标城市列表优先，其次现居城市；不限城市则不加不减。
    target_cities = [str(c).strip() for c in (profile.target_cities or []) if str(c).strip()]
    if target_cities:
        if any(city and city in (job.city or "") for city in target_cities):
            score += 12
            matched.append(f"城市匹配：{job.city}")
        else:
            score -= 10
            gaps.append(f"岗位在 {job.city or '未知城市'}，不在目标城市")
    elif profile.city and profile.city in (job.city or ""):
        score += 6
        matched.append(f"同城：{job.city}")

    # 3) 薪资：只看区间是否重叠；岗位未写薪资不扣分，避免误伤。
    if profile.salary_min and job.salary_max:
        if job.salary_max >= profile.salary_min:
            score += 10
            matched.append("薪资区间有交集")
        else:
            score -= 12
            gaps.append("岗位薪资上限低于你的最低期望")
    if job.salary_min and profile.salary_max and job.salary_min > profile.salary_max:
        score -= 8
        gaps.append("岗位薪资下限高于你的期望上限")

    # 4) 求职类型：实习/兼职/全职不一致时明显降分。
    if profile.job_type and profile.job_type != "不限" and job.job_type:
        if profile.job_type == job.job_type:
            score += 2
        else:
            score -= 20
            gaps.append(f"岗位是{job.job_type}，与你选择的{profile.job_type}不一致")

    # 5) 学历：岗位要求高于用户时列为硬缺口，但不直接判死（小公司常可放宽）。
    user_edu = _edu_level(profile.education)
    job_edu = _edu_level(job.education)
    if job_edu and user_edu and job_edu > user_edu:
        score -= 12
        gaps.append(f"岗位要求{job.education}，当前学历为{profile.education}")
        risks.append("学历可能被 HR 初筛卡住，建议优先找“大专可”或内推")
    elif job_edu and user_edu and job_edu <= user_edu:
        score += 2

    # 6) 经验：用户填“1-3年”而岗位写“5-10年”时提示冲刺风险。
    if any(word in (job.experience or "") for word in ("5-10年", "10年以上", "5年以上")):
        if "5" not in (profile.experience or ""):
            score -= 10
            risks.append("岗位经验要求偏高，简历需要用项目交付结果补足")

    # 7) 职级：主管/经理/专家岗位对 22 岁候选人通常不现实。
    if any(word in (job.title or "") for word in _SENIOR_WORDS):
        score -= 12
        risks.append("岗位职级偏高，除非有强匹配的团队管理证据")

    # 8) 行业偏好：命中加分，不命中不扣分（转行本来就会跨行业）。
    if profile.industries and any(str(ind) in (job.industry or "") for ind in profile.industries):
        score += 5
        matched.append("行业偏好命中")

    score = max(0, min(100, score))
    if score >= 78:
        recommendation = "strong"
    elif score >= 62:
        recommendation = "worth"
    elif score >= 42:
        recommendation = "stretch"
    else:
        recommendation = "skip"

    reason_parts = matched[:2] if matched else ["基础条件部分匹配"]
    if gaps:
        reason_parts.append("需注意：" + gaps[0])
    return {
        "fit_score": int(score),
        "recommendation": recommendation,
        "reason": clean_text("；".join(reason_parts), 500),
        "gaps": gaps[:5],
        "risks": risks[:5],
        "is_fallback": True,
    }


def heuristic_rank(profile: Optional[Profile], jobs: list[Job]) -> list[tuple[Job, dict[str, Any]]]:
    """批量规则打分并按分数降序；用于 AI 初筛前的预排序和断网兜底。"""
    scored = [(job, heuristic_screen(profile, job)) for job in jobs]
    scored.sort(key=lambda item: item[1]["fit_score"], reverse=True)
    return scored
