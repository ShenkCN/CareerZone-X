# ============================================================
# 职位服务：导入去重、筛选、分页、曝光/浏览/收藏。
# 这是产品最核心的数据层；路由保持很薄，方便直接对服务写测试。
# ============================================================
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from backend.models import (
    Bookmark,
    Job,
    JobCapture,
    JobImpression,
    JobScreening,
    JobView,
    Profile,
    SearchBatch,
    User,
    utcnow,
)
from backend.schemas import SOURCE_LABELS, JobSearchIn
from backend.services.company import clean_company
from backend.services.matching import heuristic_screen
from backend.services.text import (
    allowed_host_for_source,
    clean_jd_text,
    clean_text,
    make_fingerprint,
    normalize_recommendation,
    parse_salary,
    source_label,
)

# 列表页/导航链接常被误当成职位名；这些不是岗位，导入时直接跳过。
_JUNK_TITLE_EXACT = {
    "查看更多信息", "职位搜索", "搜索", "首页", "登录", "注册", "立即沟通",
    "查看全部", "更多", "下一页", "上一页", "收藏", "投递", "申请职位",
}


def _is_junk_title(title: str) -> bool:
    """判断标题是不是页面按钮/导航，而不是真实职位。"""
    normalized = re.sub(r"\s+", "", title or "")
    if len(normalized) < 2:
        return True
    if normalized in _JUNK_TITLE_EXACT:
        return True
    return any(word in normalized for word in ("查看更多", "查看全部", "职位搜索"))


def _normalize_job_type(value: str, title: str = "") -> str:
    """把各种写法收敛成四种；岗位标题里的“实习”作为兜底。"""
    text = (value or "").strip()
    if text in {"全职", "兼职", "实习"}:
        return text
    merged = f"{text} {title}"
    if "实习" in merged:
        return "实习"
    if "兼职" in merged:
        return "兼职"
    if "全职" in merged:
        return "全职"
    return ""


def _prefer(new: Any, old: Any) -> Any:
    """更新时不用空值覆盖已有的好数据。"""
    if new is None:
        return old
    if isinstance(new, str) and not new.strip():
        return old
    # 自动补全只抓 JD 时可能拿不到公司名；不要用占位符覆盖真实公司。
    if new == "未知公司" and old not in (None, "", "未知公司"):
        return old
    if isinstance(new, list) and not new:
        return old
    return new


def _merge_tags(old: list[str], new: list[str]) -> list[str]:
    """标签合并去重，保留顺序；限制 30 个，避免脏页面塞几千个。"""
    result: list[str] = []
    for item in list(old or []) + list(new or []):
        text = str(item).strip()[:32]
        if text and text not in result:
            result.append(text)
    return result[:30]


def upsert_job(db: Session, payload: dict[str, Any], user_id: Optional[int] = None) -> tuple[Job, bool]:
    """新增或更新一条职位，返回 (job, 是否新建)。

    去重顺序：
    1) 相同 URL 直接更新（详情页二次采集会补全描述）；
    2) 否则用 source+title+company+city+url 指纹匹配。
    """
    source = str(payload.get("source") or "").strip().lower()
    if source not in SOURCE_LABELS:
        raise ValueError("未知的职位来源")
    url = str(payload.get("url") or "").strip()[:1024]
    if url and not allowed_host_for_source(source, url):
        raise ValueError("职位链接与来源不匹配，已拒绝导入")

    title = clean_text(payload.get("title"), 255)
    company = clean_text(payload.get("company"), 255) or "未知公司"
    if not title:
        raise ValueError("职位名称不能为空")
    if _is_junk_title(title):
        raise ValueError("职位名称像页面导航，已跳过")
    city = clean_text(payload.get("city"), 64)
    fingerprint = make_fingerprint(source, title, company, city, url)

    existing: Optional[Job] = None
    if url:
        existing = db.execute(
            select(Job).where(Job.url == url, Job.source == source).limit(1)
        ).scalar_one_or_none()
    if existing is None:
        existing = db.execute(
            select(Job).where(Job.fingerprint == fingerprint).limit(1)
        ).scalar_one_or_none()

    salary_text = clean_text(payload.get("salary_text"), 128)
    salary_min, salary_max = parse_salary(salary_text)
    if payload.get("salary_min") is not None:
        try:
            salary_min = int(payload["salary_min"])
        except (TypeError, ValueError):
            pass
    if payload.get("salary_max") is not None:
        try:
            salary_max = int(payload["salary_max"])
        except (TypeError, ValueError):
            pass

    published_at = payload.get("published_at")
    if isinstance(published_at, datetime) and published_at.tzinfo is not None:
        from datetime import timezone

        published_at = published_at.astimezone(timezone.utc).replace(tzinfo=None)

    values = {
        "source": source,
        "source_job_id": clean_text(payload.get("source_job_id"), 128),
        "url": url,
        "title": title,
        "company": company,
        "city": city,
        "district": clean_text(payload.get("district"), 64),
        "salary_text": salary_text,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "job_type": _normalize_job_type(str(payload.get("job_type") or ""), title),
        "experience": clean_text(payload.get("experience"), 32),
        "education": clean_text(payload.get("education"), 32),
        "industry": clean_text(payload.get("industry"), 128),
        "company_size": clean_text(payload.get("company_size"), 64),
        "address": clean_text(payload.get("address"), 512),
        "description": clean_jd_text(payload.get("description"), 30000),
        "requirements": clean_jd_text(payload.get("requirements"), 20000),
        "tags": _merge_tags([], [str(t) for t in (payload.get("tags") or [])]),
        "published_at": published_at,
        "raw_json": payload.get("raw") or {},
    }

    if existing is not None:
        for field, new_value in values.items():
            if field in {"source", "source_job_id"}:
                continue
            if field == "tags":
                existing.tags = _merge_tags(existing.tags or [], new_value)
            else:
                setattr(existing, field, _prefer(new_value, getattr(existing, field)))
        existing.captured_at = utcnow()
        # URL 从空补全后指纹会变；只在没有冲突时更新。
        new_fp = make_fingerprint(existing.source, existing.title, existing.company, existing.city, existing.url)
        if new_fp != existing.fingerprint:
            conflict = db.execute(
                select(Job.id).where(Job.fingerprint == new_fp, Job.id != existing.id)
            ).first()
            if conflict is None:
                existing.fingerprint = new_fp
        db.flush()
        return existing, False

    job = Job(
        **values,
        fingerprint=fingerprint,
        status="active",
        view_count=0,
        created_by=user_id,
        captured_at=utcnow(),
    )
    db.add(job)
    db.flush()
    return job, True


def job_to_dict(
    job: Job,
    screening: Optional[JobScreening] = None,
    bookmarked: bool = False,
) -> dict[str, Any]:
    """输出职位卡片需要的全部字段；不把 3 万字描述塞进列表接口。"""
    screening_data = None
    if screening is not None:
        screening_data = {
            "fit_score": screening.fit_score,
            "recommendation": screening.recommendation,
            "reason": screening.reason,
            "gaps": list(screening.gaps_json or []),
            "risks": list(screening.risks_json or []),
            "model": screening.model,
            "is_fallback": screening.is_fallback,
        }
    return {
        "id": job.id,
        "source": job.source,
        "source_label": source_label(job.source),
        "url": job.url,
        "title": job.title,
        "company": clean_company(job.company),
        "city": job.city,
        "district": job.district,
        "salary_text": job.salary_text,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "job_type": job.job_type,
        "experience": job.experience,
        "education": job.education,
        "industry": job.industry,
        "company_size": job.company_size,
        "address": job.address,
        "tags": list(job.tags or []),
        "view_count": job.view_count,
        "captured_at": job.captured_at,
        "bookmarked": bookmarked,
        "screening": screening_data,
    }


def _keyword_conditions(keyword: str):
    """关键词按空格拆成多个词，每个词都必须命中某个字段（AND of OR）。"""
    tokens = [token for token in keyword.replace("，", " ").replace(",", " ").split() if token]
    conditions = []
    for token in tokens[:6]:
        like = f"%{token}%"
        conditions.append(
            or_(
                Job.title.like(like),
                Job.company.like(like),
                Job.city.like(like),
                Job.description.like(like),
                Job.requirements.like(like),
                Job.industry.like(like),
            )
        )
    return conditions


def build_filter_conditions(filters: JobSearchIn) -> list[Any]:
    """把前端筛选器翻译成 SQL 条件；None/空字符串表示不筛。"""
    conditions: list[Any] = [Job.status == "active"]
    if filters.keyword.strip():
        conditions.extend(_keyword_conditions(filters.keyword.strip()))
    if filters.source:
        conditions.append(Job.source == filters.source)
    if filters.city.strip():
        # 用户经常写“苏州/上海”或“苏州,上海”；不能把它当成一个完整字符串去 LIKE。
        # 按 / , ， 、 空格拆成多个城市，城市或地址命中任意一个即可。
        city_tokens = [item for item in re.split(r"[/,，、\s]+", filters.city.strip()) if item]
        city_conditions = []
        for token in city_tokens:
            city_like = f"%{token}%"
            city_conditions.append(or_(Job.city.like(city_like), Job.address.like(city_like)))
        if city_conditions:
            conditions.append(or_(*city_conditions))
    # 前端下拉框的“不限”是显示文案，不能当成真实值去等值/LIKE 查询。
    if filters.job_type and filters.job_type != "不限":
        conditions.append(Job.job_type == filters.job_type)
    if filters.experience.strip() and filters.experience.strip() != "不限":
        conditions.append(Job.experience.like(f"%{filters.experience.strip()}%"))
    if filters.education.strip() and filters.education.strip() != "不限":
        conditions.append(Job.education.like(f"%{filters.education.strip()}%"))
    if filters.industry.strip():
        conditions.append(Job.industry.like(f"%{filters.industry.strip()}%"))
    if filters.company_size.strip() and filters.company_size.strip() != "不限":
        conditions.append(Job.company_size.like(f"%{filters.company_size.strip()}%"))
    if filters.salary_min is not None:
        # 有薪资筛选时，薪资未知的岗位不进入结果，保证筛选语义明确。
        conditions.append(Job.salary_max.is_not(None))
        conditions.append(Job.salary_max >= filters.salary_min)
    if filters.salary_max is not None:
        conditions.append(Job.salary_min.is_not(None))
        conditions.append(Job.salary_min <= filters.salary_max)
    return conditions


def screening_map(
    db: Session,
    user_id: int,
    job_ids: Iterable[int],
    profile_hash: str,
) -> dict[int, JobScreening]:
    """一次查出这一页的初筛结果，避免 N+1 查询。"""
    ids = list(job_ids)
    if not ids:
        return {}
    rows = db.execute(
        select(JobScreening).where(
            JobScreening.user_id == user_id,
            JobScreening.profile_hash == profile_hash,
            JobScreening.job_id.in_(ids),
        )
    ).scalars().all()
    return {row.job_id: row for row in rows}


def get_bookmarked_ids(db: Session, user_id: int, job_ids: Iterable[int]) -> set[int]:
    """一次查出当前页哪些已收藏。"""
    ids = list(job_ids)
    if not ids:
        return set()
    return set(
        db.execute(
            select(Bookmark.job_id).where(Bookmark.user_id == user_id, Bookmark.job_id.in_(ids))
        ).scalars().all()
    )


def query_jobs(
    db: Session,
    filters: JobSearchIn,
    *,
    user: User,
    profile: Optional[Profile],
    profile_hash: str,
    exclude_ids: Optional[list[int]] = None,
    limit: int = 20,
) -> tuple[list[Job], int, bool]:
    """筛选 + 排序 + 截取一页。

    排序策略：已有 AI 初筛分优先；没有则用规则分预排。
    为了不让排序在 Python 侧丢掉全部结果，这里先取最多 1200 条候选，
    个人使用的数据量足够；数据变大后再把分数落表用 SQL 排序。
    """
    conditions = build_filter_conditions(filters)
    # 当前用户清空的职位只对当前用户隐藏；Job 本身保留给其他用户。
    cleared = (
        select(JobCapture.id)
        .where(
            JobCapture.user_id == user.id,
            JobCapture.job_id == Job.id,
            JobCapture.cleared_at.is_not(None),
        )
        .exists()
    )
    conditions.append(~cleared)
    # 全局可见性：职位至少有一个“未清空”的采集关系。
    # A 清空后，若 B 没有自己的采集关系，A 的这批也会从 B 的视图消失；
    # 若 B 采过同一个职位，B 的 active capture 会继续让它可见。
    active_capture = (
        select(JobCapture.id)
        .where(
            JobCapture.job_id == Job.id,
            JobCapture.cleared_at.is_(None),
        )
        .exists()
    )
    conditions.append(active_capture)
    exclude = set(exclude_ids or []) | set(filters.exclude_ids or [])
    if exclude:
        conditions.append(Job.id.not_in(list(exclude)[:500]))

    total = int(
        db.scalar(select(func.count(Job.id)).where(and_(*conditions))) or 0
    )
    candidates = db.execute(
        select(Job)
        .where(and_(*conditions))
        .order_by(desc(Job.captured_at), desc(Job.id))
        .limit(1200)
    ).scalars().all()

    scores = screening_map(db, user.id, [job.id for job in candidates], profile_hash)
    if scores:
        candidates.sort(
            key=lambda job: (
                scores.get(job.id).fit_score if scores.get(job.id) else 0,
                job.captured_at or datetime.min,
            ),
            reverse=True,
        )
    elif profile is not None:
        candidates.sort(key=lambda job: heuristic_screen(profile, job)["fit_score"], reverse=True)

    page = candidates[filters.offset: filters.offset + limit]
    exhausted = len(candidates) <= filters.offset
    return page, total, exhausted


def list_impression_ids(db: Session, user_id: int, days: int = 45) -> list[int]:
    """取最近 N 天的曝光 ID，用于“换一批”排除。

    只取最近 45 天：太老的职位可以重新出现，否则池子会越用越空。
    """
    since = utcnow() - timedelta(days=days)
    return list(
        db.execute(
            select(JobImpression.job_id)
            .where(JobImpression.user_id == user_id, JobImpression.shown_at >= since)
            .distinct()
        ).scalars().all()
    )


def record_impressions(db: Session, user_id: int, job_ids: list[int], batch_id: Optional[int]) -> None:
    """记录本页曝光；不 commit，和批次写入保持同一事务。"""
    for job_id in job_ids:
        db.add(JobImpression(user_id=user_id, job_id=job_id, batch_id=batch_id))


def get_last_batch(db: Session, user_id: int) -> Optional[SearchBatch]:
    """取最近一次批次，用于计算与上一页的重复率。"""
    return db.execute(
        select(SearchBatch)
        .where(SearchBatch.user_id == user_id)
        .order_by(desc(SearchBatch.created_at), desc(SearchBatch.id))
        .limit(1)
    ).scalar_one_or_none()


def create_batch(
    db: Session,
    user_id: int,
    filters: JobSearchIn,
    job_ids: list[int],
    overlap_rate: float,
    exhausted: bool,
) -> SearchBatch:
    """保存批次快照；换一批时前端不必自己维护历史。"""
    batch = SearchBatch(
        user_id=user_id,
        filters_json=filters.model_dump(),
        job_ids_json=job_ids,
        overlap_rate=round(overlap_rate, 4),
        exhausted=exhausted,
    )
    db.add(batch)
    db.flush()
    return batch


def calculate_overlap(previous: Optional[SearchBatch], current_ids: list[int]) -> float:
    """重复率 = 交集 / 当前页数量；没有上一页就是 0。"""
    if previous is None or not current_ids:
        return 0.0
    previous_ids = set(previous.job_ids_json or [])
    current = set(current_ids)
    return len(previous_ids & current) / max(1, len(current))


def get_job(db: Session, job_id: int) -> Optional[Job]:
    """按 id 取职位；路由层负责 404。"""
    return db.get(Job, job_id)


def record_view(db: Session, user: User, job: Job) -> bool:
    """记录一次浏览；10 分钟内重复刷新不重复计数，避免浏览次数被 F5 刷高。"""
    threshold = utcnow() - timedelta(minutes=10)
    recent = db.execute(
        select(JobView.id)
        .where(
            JobView.user_id == user.id,
            JobView.job_id == job.id,
            JobView.viewed_at >= threshold,
        )
        .limit(1)
    ).first()
    if recent is not None:
        return False
    db.add(JobView(user_id=user.id, job_id=job.id, viewed_at=utcnow()))
    job.view_count = int(job.view_count or 0) + 1
    db.flush()
    return True


def toggle_bookmark(db: Session, user: User, job: Job) -> bool:
    """收藏/取消收藏，返回操作后的状态。"""
    existing = db.execute(
        select(Bookmark).where(Bookmark.user_id == user.id, Bookmark.job_id == job.id)
    ).scalar_one_or_none()
    if existing is not None:
        db.delete(existing)
        db.flush()
        return False
    db.add(Bookmark(user_id=user.id, job_id=job.id))
    db.flush()
    return True


def is_bookmarked(db: Session, user_id: int, job_id: int) -> bool:
    """详情页/列表页都要用；单条查询开销可接受。"""
    return db.execute(
        select(Bookmark.id).where(Bookmark.user_id == user_id, Bookmark.job_id == job_id)
    ).first() is not None


def list_bookmarks(db: Session, user: User, profile_hash: str, offset: int = 0, limit: int = 20):
    """收藏列表按收藏时间倒序，保持与职位卡片一致的输出。"""
    rows = db.execute(
        select(Bookmark, Job)
        .join(Job, Job.id == Bookmark.job_id)
        .where(Bookmark.user_id == user.id)
        .order_by(desc(Bookmark.created_at))
        .offset(offset)
        .limit(limit)
    ).all()
    jobs = [job for _, job in rows]
    scores = screening_map(db, user.id, [job.id for job in jobs], profile_hash)
    bookmarked_ids = {job.id for _, job in rows}
    items = [job_to_dict(job, scores.get(job.id), job.id in bookmarked_ids) for job in jobs]
    total = int(db.scalar(select(func.count(Bookmark.id)).where(Bookmark.user_id == user.id)) or 0)
    return items, total


def list_history(db: Session, user: User, profile_hash: str, offset: int = 0, limit: int = 20):
    """浏览历史按时间倒序；同一岗位多次打开保留最近一次，避免历史页重复。"""
    subq = (
        select(JobView.job_id, func.max(JobView.viewed_at).label("last_viewed"))
        .where(JobView.user_id == user.id)
        .group_by(JobView.job_id)
        .subquery()
    )
    rows = db.execute(
        select(Job, subq.c.last_viewed)
        .join(subq, subq.c.job_id == Job.id)
        .order_by(desc(subq.c.last_viewed))
        .offset(offset)
        .limit(limit)
    ).all()
    jobs = [row[0] for row in rows]
    scores = screening_map(db, user.id, [job.id for job in jobs], profile_hash)
    bookmarked_ids = get_bookmarked_ids(db, user.id, [job.id for job in jobs])
    items = [
        {
            "job": job_to_dict(job, scores.get(job.id), job.id in bookmarked_ids),
            "viewed_at": row[1],
        }
        for job, row in zip(jobs, rows)
    ]
    total = int(
        db.scalar(
            select(func.count(func.distinct(JobView.job_id))).where(JobView.user_id == user.id)
        )
        or 0
    )
    return items, total


def top_viewed_jobs(db: Session, user_id: int, limit: int = 6) -> list[dict[str, Any]]:
    """浏览次数最高的岗位；个人数据量小，直接按 view_count 排序。"""
    jobs = db.execute(
        select(Job).where(Job.status == "active").order_by(desc(Job.view_count), desc(Job.id)).limit(limit)
    ).scalars().all()
    return [job_to_dict(job) for job in jobs]


def get_analysis_recommendation(value: str) -> str:
    """统一建议代码，避免历史脏数据让前端映射不到颜色。"""
    return normalize_recommendation(value)
