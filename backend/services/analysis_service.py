# ============================================================
# AI 分析服务：列表批量初筛 + 详情两轮深度分析。
# 为什么分两层：
# - 列表 20 条逐条调两次模型太慢/太贵，用一次批量初筛解决“先看哪条”；
# - 用户点进详情后再做“JD 结构化 + 画像匹配”两轮，保证结论可解释。
# ============================================================
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Job, JobAnalysis, JobScreening, Profile, User
from backend.services.company import clean_company
from backend.services.job_service import screening_map
from backend.services.llm import LLMError, LLMService
from backend.services.matching import heuristic_screen
from backend.services.profile_service import build_profile_text, current_profile_hash
from backend.services.prompts import PROMPT_VERSION, render_prompt
from backend.services.text import clamp_score, clean_jd_text, clean_text, normalize_recommendation

logger = logging.getLogger(__name__)

# 初筛只把每个职位的前 700 字发给模型，控制 20 条一次请求的 token。
SCREEN_DESC_CHARS = 700


def _jobs_payload(jobs: list[Job]) -> str:
    """把 ORM 职位压成模型可读的紧凑 JSON，避免把公司内部字段发出去。"""
    payload = []
    for job in jobs:
        payload.append(
            {
                "job_id": job.id,
                "title": job.title,
                "company": clean_company(job.company),
                "city": job.city,
                "salary": job.salary_text,
                "job_type": job.job_type,
                "experience": job.experience,
                "education": job.education,
                "industry": job.industry,
                "company_size": job.company_size,
                "tags": list(job.tags or [])[:10],
                "description": clean_text(job.description or job.requirements, SCREEN_DESC_CHARS),
            }
        )
    return json.dumps(payload, ensure_ascii=False)


def _safe_string_list(value: Any, max_items: int = 5, max_len: int = 200) -> list[str]:
    """模型可能返回字符串/列表/对象；统一成字符串列表，防止前端渲染报错。"""
    if isinstance(value, str):
        return [clean_text(value, max_len)] if value.strip() else []
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            # gaps 里常见 {"gap": "...", "severity": "..."}，取最有信息量的一列。
            text = item.get("gap") or item.get("risk") or item.get("point") or item.get("advice") or ""
        else:
            text = item
        text = clean_text(text, max_len)
        if text and text not in result:
            result.append(text)
    return result[:max_items]


def _safe_dict_list(value: Any, max_items: int = 4) -> list[dict[str, Any]]:
    """规范化模型返回的对象数组；只保留字符串字段，避免嵌套任意 JSON。"""
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        cleaned = {
            str(key)[:32]: clean_text(val, 300)
            for key, val in item.items()
            if isinstance(val, (str, int, float))
        }
        if cleaned:
            result.append(cleaned)
    return result[:max_items]


def _upsert_screening(
    db: Session,
    *,
    job: Job,
    user: User,
    profile_hash: str,
    data: dict[str, Any],
    model: str,
    is_fallback: bool,
) -> JobScreening:
    """按 (job,user,profile_hash) 更新或插入一条初筛。"""
    row = db.execute(
        select(JobScreening).where(
            JobScreening.job_id == job.id,
            JobScreening.user_id == user.id,
            JobScreening.profile_hash == profile_hash,
        )
    ).scalar_one_or_none()
    if row is None:
        row = JobScreening(job_id=job.id, user_id=user.id, profile_hash=profile_hash)
        db.add(row)
    row.fit_score = clamp_score(data.get("fit_score"), 0)
    row.recommendation = normalize_recommendation(data.get("recommendation"))
    row.reason = clean_text(data.get("reason"), 500)
    row.gaps_json = _safe_string_list(data.get("gaps"), 3, 160)
    row.risks_json = _safe_string_list(data.get("risks"), 2, 160)
    row.model = model[:64]
    row.is_fallback = bool(is_fallback)
    db.flush()
    return row


def screen_jobs(
    db: Session,
    user: User,
    profile: Profile,
    jobs: list[Job],
    llm: LLMService,
    *,
    force: bool = False,
) -> tuple[str, str]:
    """对本页职位做批量初筛。

    返回 (状态, 错误信息)：
    - "ai"：模型成功；
    - "ai_partial"：模型只返回部分，缺的用规则补齐；
    - "rules"：未配置模型，使用规则；
    - "fallback"：模型失败，使用规则并附带错误。
    已存在的同画像初筛直接复用，避免翻页/刷新重复烧 token。
    """
    if not jobs:
        return "not_requested", ""
    phash = current_profile_hash(profile)
    existing = screening_map(db, user.id, [job.id for job in jobs], phash)
    missing = [job for job in jobs if force or job.id not in existing]
    if not missing:
        return "cached", ""

    # 未配置 Key：明确走规则，不伪装成 AI。
    if not llm.available:
        for job in missing:
            _upsert_screening(
                db,
                job=job,
                user=user,
                profile_hash=phash,
                data=heuristic_screen(profile, job),
                model="rules-v1",
                is_fallback=True,
            )
        return "rules", "未配置 DeepSeek API Key，本页使用规则初筛。"

    # 一页最多 1000 条；DeepSeek 全量分析会非常慢/贵，所以只初筛前 300 条，
    # 其余标“尚未分析”，用户点详情时再做两轮深度分析。
    max_screen = 300
    screen_batch = missing[:max_screen]
    skipped = len(missing) - len(screen_batch)
    # 每 40 条一组调用，减少 1000 条页面的往返；组内失败只影响这一组。
    chunk_size = 40
    chunks = [screen_batch[i:i + chunk_size] for i in range(0, len(screen_batch), chunk_size)]
    ai_count = 0
    fallback_count = 0
    errors: list[str] = []
    profile_text = build_profile_text(profile)

    def _call_chunk(chunk: list[Job]):
        """线程里只做 HTTP，不碰 SQLAlchemy Session；每个线程独立 Session。"""
        try:
            service = LLMService(llm.settings)
            prompt = render_prompt(
                "job_screen_batch",
                PROFILE=profile_text,
                JOBS=_jobs_payload(chunk),
            )
            result = service.chat_json(
                system="你是严谨的求职匹配分析师，只输出 JSON。",
                user=prompt,
                endpoint="job_screen_batch",
                user_id=user.id,
                temperature=0.2,
                # 40 条结果可能接近 4K；给足输出空间，避免 JSON 被截断。
                max_tokens=8192,
            )
            return chunk, result, None
        except Exception as exc:  # noqa: BLE001 - 线程异常必须转成该组兜底
            return chunk, None, exc

    # 3 路并发：40 条/组时 160 条只需约 2 轮网络等待，而不是 4 轮串行。
    results = []
    workers = min(3, max(1, len(chunks)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="czx-screen") as pool:
        futures = [pool.submit(_call_chunk, chunk) for chunk in chunks]
        for future in futures:
            results.append(future.result())

    for chunk, result, error in results:
        if error is not None or result is None:
            logger.warning("批量初筛失败（%s 条）：%s", len(chunk), error)
            errors.append(str(error or "模型调用失败"))
            fallback_count += len(chunk)
            for job in chunk:
                _upsert_screening(
                    db,
                    job=job,
                    user=user,
                    profile_hash=phash,
                    data=heuristic_screen(profile, job),
                    model="rules-v1",
                    is_fallback=True,
                )
            continue

        try:
            data = result.data
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise LLMError("批量初筛返回格式错误", "invalid_json")

            by_id: dict[int, dict[str, Any]] = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    job_id = int(item.get("job_id"))
                except (TypeError, ValueError):
                    continue
                by_id[job_id] = item

            for job in chunk:
                item = by_id.get(job.id)
                used_fallback = item is None
                if used_fallback:
                    fallback_count += 1
                    item = heuristic_screen(profile, job)
                else:
                    ai_count += 1
                _upsert_screening(
                    db,
                    job=job,
                    user=user,
                    profile_hash=phash,
                    data=item,
                    model="rules-v1" if used_fallback else result.model,
                    is_fallback=used_fallback,
                )
        except LLMError as exc:
            logger.warning("批量初筛结果解析失败（%s 条）：%s", len(chunk), exc.code)
            errors.append(str(exc))
            fallback_count += len(chunk)
            for job in chunk:
                _upsert_screening(
                    db,
                    job=job,
                    user=user,
                    profile_hash=phash,
                    data=heuristic_screen(profile, job),
                    model="rules-v1",
                    is_fallback=True,
                )

    if ai_count == 0:
        detail = errors[0] if errors else "模型未返回有效结果"
        return "fallback", detail
    if skipped:
        detail = f"本次只初筛前 {len(screen_batch)} 条；剩余 {skipped} 条点详情按需分析。"
        if errors:
            detail += f" 其中 {fallback_count} 条规则兜底。"
        return "ai_capped", detail
    if fallback_count or errors:
        detail = f"{fallback_count} 条使用规则兜底"
        if errors:
            detail += f"；{errors[0]}"
        return "ai_partial", detail
    return "ai", ""


def _job_text(job: Job) -> str:
    """给两轮分析用的 JD 文本；保留换行，方便模型区分职责/要求。"""
    parts = [
        f"职位：{job.title}",
        f"公司：{clean_company(job.company)}",
        f"城市：{job.city} {job.district}",
        f"薪资：{job.salary_text}",
        f"类型：{job.job_type}",
        f"经验：{job.experience}",
        f"学历：{job.education}",
        f"行业：{job.industry}",
        f"规模：{job.company_size}",
        f"标签：{'、'.join(job.tags or [])}",
        "\n【职位描述】\n" + clean_jd_text(job.description),
        "\n【任职要求】\n" + clean_jd_text(job.requirements),
    ]
    return clean_text("\n".join(parts), 20000)


def _normalize_pass1(data: dict[str, Any]) -> dict[str, Any]:
    """只保留详情页真正展示的字段，避免模型多返回的键污染数据库。"""
    return {
        "title": clean_text(data.get("title"), 255),
        "company": clean_text(data.get("company"), 255),
        "city": clean_text(data.get("city"), 64),
        "salary_text": clean_text(data.get("salary_text"), 128),
        "job_type": clean_text(data.get("job_type"), 16),
        "experience": clean_text(data.get("experience"), 32),
        "education": clean_text(data.get("education"), 32),
        "industry": clean_text(data.get("industry"), 128),
        "company_size": clean_text(data.get("company_size"), 64),
        "responsibilities": _safe_string_list(data.get("responsibilities"), 6, 200),
        "requirements": _safe_string_list(data.get("requirements"), 6, 200),
        "nice_to_have": _safe_string_list(data.get("nice_to_have"), 4, 200),
        "skills": _safe_string_list(data.get("skills"), 12, 64),
        "red_flags": _safe_string_list(data.get("red_flags"), 4, 200),
        "summary": clean_text(data.get("summary"), 200),
    }


def _normalize_pass2(data: dict[str, Any]) -> dict[str, Any]:
    """规范化第二轮匹配结果；所有前端会用到的键都给默认值。"""
    confidence = data.get("confidence", 0.5)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "fit_score": clamp_score(data.get("fit_score"), 0),
        "recommendation": normalize_recommendation(data.get("recommendation")),
        "headline": clean_text(data.get("headline"), 255),
        "job_nature": clean_text(data.get("job_nature"), 200),
        "matched_points": _safe_dict_list(data.get("matched_points"), 4),
        "gaps": _safe_dict_list(data.get("gaps"), 4),
        "risk_flags": _safe_string_list(data.get("risk_flags"), 4, 200),
        "salary_comment": clean_text(data.get("salary_comment"), 200),
        "apply_advice": clean_text(data.get("apply_advice"), 300),
        "resume_angle": _safe_string_list(data.get("resume_angle"), 3, 200),
        "questions_to_ask": _safe_string_list(data.get("questions_to_ask"), 4, 200),
        "confidence": round(confidence, 2),
    }


def deep_analyze(
    db: Session,
    user: User,
    profile: Profile,
    job: Job,
    llm: LLMService,
    *,
    force: bool = False,
) -> JobAnalysis:
    """对单个职位做两轮分析；成功返回记录，失败抛 LLMError。

    缓存键包含 profile_hash：画像改了会自动重算，不会拿旧结论误导用户。
    """
    if not llm.available:
        raise LLMError("未配置 DeepSeek API Key，暂时无法做深度分析", "not_configured")

    raw_jd = f"{job.description or ''}\n{job.requirements or ''}".strip()
    raw_meta = job.raw_json if isinstance(job.raw_json, dict) else {}
    # 列表卡片的正文可能很长（标题+标签+公司），不能只靠长度判断；
    # 明确是 list 模式的一律不允许做“两轮深度分析”。
    if ((raw_meta.get("mode") == "list" and not raw_meta.get("hasDetail")) or len(raw_jd) < 80):
        raise LLMError(
            "这条职位只有列表摘要，缺少完整 JD。请打开原站详情页，用扩展重新采集，"
            "或在职位板手动粘贴 JD，再做两轮分析。",
            "jd_too_short",
        )

    phash = current_profile_hash(profile)
    existing = db.execute(
        select(JobAnalysis).where(
            JobAnalysis.job_id == job.id,
            JobAnalysis.user_id == user.id,
            JobAnalysis.profile_hash == phash,
        )
    ).scalar_one_or_none()
    if existing is not None and not force:
        return existing

    job_text = _job_text(job)
    # 第一轮：把原始 JD 变成稳定结构；这一步只抽事实，不判断匹配。
    pass1_result = llm.chat_json(
        system="你是招聘信息结构化抽取器，只输出 JSON。",
        user=render_prompt("job_extract", JOB_TEXT=job_text),
        endpoint="job_extract",
        user_id=user.id,
        temperature=0.1,
        max_tokens=2600,
    )
    if not isinstance(pass1_result.data, dict):
        raise LLMError("第一轮 JD 解析返回格式错误", "invalid_json")
    pass1 = _normalize_pass1(pass1_result.data)

    # 第二轮：带着画像和结构化 JD 做匹配，要求给出证据、缺口和风险。
    pass2_result = llm.chat_json(
        system="你是严谨、诚实的求职顾问，只输出 JSON。",
        user=render_prompt(
            "job_match",
            PROFILE=build_profile_text(profile),
            JOB_STRUCTURED=json.dumps(pass1, ensure_ascii=False, indent=2),
            JOB_TEXT=job_text[:9000],
        ),
        endpoint="job_match",
        user_id=user.id,
        temperature=0.2,
        max_tokens=3200,
    )
    if not isinstance(pass2_result.data, dict):
        raise LLMError("第二轮匹配返回格式错误", "invalid_json")
    pass2 = _normalize_pass2(pass2_result.data)

    if existing is None:
        existing = JobAnalysis(job_id=job.id, user_id=user.id, profile_hash=phash)
        db.add(existing)
    existing.fit_score = pass2["fit_score"]
    existing.recommendation = pass2["recommendation"]
    existing.headline = pass2["headline"]
    existing.pass1_json = pass1
    existing.pass2_json = pass2
    existing.model = pass1_result.model[:64]
    existing.prompt_version = PROMPT_VERSION
    existing.tokens_in = pass1_result.prompt_tokens + pass2_result.prompt_tokens
    existing.tokens_out = pass1_result.completion_tokens + pass2_result.completion_tokens
    existing.is_fallback = False
    db.flush()
    return existing


def analysis_to_dict(row: Optional[JobAnalysis]) -> Optional[dict[str, Any]]:
    """详情页输出；前端拿 pass1/pass2 分别渲染“原 JD 摘要”和“AI 判断”。"""
    if row is None:
        return None
    return {
        "fit_score": row.fit_score,
        "recommendation": row.recommendation,
        "headline": row.headline,
        "pass1": row.pass1_json or {},
        "pass2": row.pass2_json or {},
        "model": row.model,
        "prompt_version": row.prompt_version,
        "is_fallback": row.is_fallback,
        "created_at": row.created_at,
    }
