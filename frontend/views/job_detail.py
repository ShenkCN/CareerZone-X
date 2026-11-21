# ============================================================
# 职位详情：先原站信息，再 AI 两轮分析。
# 顺序不能反：用户必须先看到“原岗位写了什么”，再看 AI 的判断，
# 避免把模型结论当成招聘方原文。
# ============================================================
from __future__ import annotations

import html
import re
from typing import Any

import streamlit as st

from frontend import ui
from frontend.api_client import ApiClient, ApiError


def _format_jd(text: str) -> str:
    """把采集到的一整段 JD 恢复成便于阅读的段落/编号。"""
    value = (text or "").strip()
    value = re.sub(r"(?<!^)(岗位职责|任职要求|岗位要求|加分项|职位描述|工作职责)", r"\n\n\1\n", value)
    value = re.sub(r"(?<!^)(\d{1,2}[.、])", r"\n\1", value)
    value = re.sub(r"(?<!^)([•·])", r"\n\1", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _render_jd_block(text: str, label: str, key: str) -> None:
    """长 JD 自动换行 + 预览 + 展开/收起，避免一整面字。"""
    if not text:
        return
    formatted = _format_jd(text)
    chars = len(text)
    st.markdown(f"**{label}**")
    expanded = st.toggle(f"展开全文（{chars} 字）", key=key)
    if expanded:
        safe = html.escape(formatted).replace("\n", "<br>")
        st.markdown(f'<div class="czx-jd-box">{safe}</div>', unsafe_allow_html=True)
    else:
        preview = formatted[:700] + ("…" if len(formatted) > 700 else "")
        safe = html.escape(preview).replace("\n", "<br>")
        st.markdown(f'<div class="czx-jd-preview">{safe}</div>', unsafe_allow_html=True)


def _render_original(job: dict[str, Any], description: str, requirements: str) -> None:
    """原站信息区：不改写、不总结，只做排版。"""
    st.markdown("### 原站职位信息")
    st.caption("以下内容来自你采集/粘贴的职位页面，未经过 AI 改写。投递前请回原站核对。")
    if description:
        _render_jd_block(description, "职位描述（原文）", f"jd_expand_{job.get('id')}")
    else:
        st.info("没有采集到职位描述，可能只采集了列表卡片；建议打开详情页重新采集。")
    if requirements:
        _render_jd_block(requirements, "任职要求（原文）", f"req_expand_{job.get('id')}")
    if len((description + requirements).strip()) < 80:
        st.warning(
            "当前只有列表卡片摘要，没有完整 JD。"
            "请打开原站详情页，用扩展重新“采集当前页”覆盖，或手动粘贴 JD 后再做两轮分析。"
        )


def _render_analysis(analysis: dict[str, Any]) -> None:
    """渲染两轮分析；兼容模型缺字段/类型不一致。"""
    pass1 = analysis.get("pass1") or {}
    pass2 = analysis.get("pass2") or {}
    score = int(analysis.get("fit_score") or 0)
    rec = analysis.get("recommendation")
    meta = ui.RECOMMENDATION_META.get(rec or "", None)

    st.markdown("### AI 两轮分析")
    st.caption(
        f"模型：{analysis.get('model', '')}｜Prompt：{analysis.get('prompt_version', '')}｜"
        f"生成时间：{ui.format_datetime(analysis.get('created_at'))}"
    )
    c1, c2 = st.columns([1, 2.4])
    with c1:
        color = meta["color"] if meta else "#0f766e"
        st.markdown(
            f'<div class="czx-score" style="color:{color}">{score}<span style="font-size:16px">/100</span></div>'
            f'{ui.recommendation_badge(rec)}',
            unsafe_allow_html=True,
        )
        st.progress(min(100, max(0, score)) / 100)
    with c2:
        st.markdown(f"**{html.escape(analysis.get('headline') or '暂无结论')}**")
        if pass2.get("job_nature"):
            st.caption("岗位本质：" + str(pass2.get("job_nature")))
        if pass2.get("confidence") is not None:
            st.caption(f"置信度：{float(pass2.get('confidence') or 0):.0%}（信息不足时会降低）")

    if pass2.get("apply_advice"):
        st.info("投递建议：" + str(pass2.get("apply_advice")))

    matched = pass2.get("matched_points") or []
    gaps = pass2.get("gaps") or []
    risks = pass2.get("risk_flags") or []
    c3, c4 = st.columns(2)
    with c3:
        st.markdown("#### 为什么匹配")
        if matched:
            for item in matched:
                if isinstance(item, dict):
                    st.markdown(f"- **{item.get('point', '')}**：{item.get('evidence', '')}")
                else:
                    st.markdown(f"- {item}")
        else:
            st.caption("模型没有给出明确匹配点。")
    with c4:
        st.markdown("#### 缺口与风险")
        if gaps:
            for item in gaps:
                if isinstance(item, dict):
                    st.markdown(f"- **{item.get('gap', '')}**（{item.get('severity', '')}）：{item.get('advice', '')}")
                else:
                    st.markdown(f"- {item}")
        if risks:
            for item in risks:
                st.markdown(f"- ⚠️ {item}")
        if not gaps and not risks:
            st.caption("没有发现明显缺口；仍需以原站要求为准。")

    if pass2.get("salary_comment"):
        st.markdown("#### 薪资判断")
        st.write(pass2.get("salary_comment"))

    c5, c6 = st.columns(2)
    with c5:
        st.markdown("#### 简历/打招呼角度")
        for item in pass2.get("resume_angle") or []:
            st.markdown(f"- {item}")
        if not pass2.get("resume_angle"):
            st.caption("暂无建议。")
    with c6:
        st.markdown("#### 面试该问什么")
        for item in pass2.get("questions_to_ask") or []:
            st.markdown(f"- {item}")
        if not pass2.get("questions_to_ask"):
            st.caption("暂无建议。")

    with st.expander("第一轮：AI 从 JD 抽取了什么", expanded=False):
        st.markdown(f"**摘要：** {pass1.get('summary', '—')}")
        for label, key in [
            ("职责", "responsibilities"),
            ("硬性要求", "requirements"),
            ("加分项", "nice_to_have"),
            ("技能关键词", "skills"),
            ("原文风险", "red_flags"),
        ]:
            values = pass1.get(key) or []
            if values:
                st.markdown(f"**{label}**")
                for item in values:
                    st.markdown(f"- {item}")
    st.warning("AI 会出错：薪资、学历、经验等硬条件请以原站和 HR 回复为准；不要仅凭分数放弃或投递。")


def render_job_detail(api: ApiClient) -> None:
    """详情页主入口。"""
    if st.session_state.get("scroll_top"):
        st.session_state.scroll_top = False
        ui.scroll_to_top()
    job_id = st.session_state.get("selected_job_id")
    if not job_id:
        st.info("没有选中的职位")
        if st.button("返回职位板"):
            st.session_state.page = "jobs"
            st.rerun()
        return

    if st.button("← 返回职位板"):
        st.session_state.page = "jobs"
        st.session_state.selected_job_id = None
        st.rerun()

    try:
        detail = api.job_detail(int(job_id))
    except ApiError as exc:
        st.error(exc.message)
        return
    job = detail.get("job") or {}
    if not job:
        st.error("职位数据为空")
        return

    st.markdown(
        f'<div class="czx-job-title" style="font-size:28px">{html.escape(job.get("title") or "")}</div>'
        f'<div class="czx-job-company">{html.escape(job.get("company") or "")} · '
        f'{html.escape(job.get("city") or "")} {html.escape(job.get("district") or "")}</div>',
        unsafe_allow_html=True,
    )
    tags = " ".join(ui.badge(tag) for tag in ui.job_tags(job))
    st.markdown(
        f'{tags} {ui.badge(job.get("source_label") or "未知来源", "#475569", "#f1f5f9")}',
        unsafe_allow_html=True,
    )
    c1, c2, c3 = st.columns([1.1, 1.1, 2.4])
    with c1:
        st.markdown(f'<div class="czx-salary">{html.escape(ui.format_salary(job))}</div>', unsafe_allow_html=True)
    with c2:
        st.caption(f"浏览 {job.get('view_count', 0)} 次")
    with c3:
        link = ui.safe_url(job.get("url") or "")
        if link:
            st.link_button("去原站查看 / 投递", link, use_container_width=True)
        else:
            st.caption("没有原站链接")

    b1, b2 = st.columns([1, 4])
    with b1:
        label = "已收藏" if job.get("bookmarked") else "收藏"
        if st.button(label, use_container_width=True):
            try:
                state = api.bookmark(int(job_id))
                st.session_state["detail_bookmark"] = state.get("bookmarked")
                st.toast(state.get("message", "已更新"))
                st.rerun()
            except ApiError as exc:
                st.error(exc.message)

    st.divider()
    _render_original(job, detail.get("description") or "", detail.get("requirements") or "")

    st.divider()
    jd_ready = len(((detail.get("description") or "") + (detail.get("requirements") or "")).strip()) >= 80
    analysis = detail.get("analysis")
    if analysis:
        if not jd_ready:
            st.warning("这份分析可能是基于列表摘要生成的，证据不足；请采集详情页后重新分析。")
        _render_analysis(analysis)
        if st.button("重新分析（消耗 Token）", use_container_width=False, disabled=not jd_ready):
            with st.spinner("重新跑两轮分析..."):
                try:
                    api.analyze_job(int(job_id), force=True)
                except ApiError as exc:
                    st.error(exc.message)
                    return
            # 不把分析塞 session：rerun 后重新从数据库取，避免缓存与库不一致。
            st.rerun()
    else:
        st.markdown("### AI 两轮分析")
        st.caption("第一轮把 JD 结构化；第二轮结合你的画像找匹配点、缺口和风险。")
        if not jd_ready:
            st.error("缺少完整 JD，不能开始两轮分析；请先采集原站详情页或手动粘贴 JD。")
        if st.button("开始两轮分析", type="primary", disabled=not jd_ready):
            with st.spinner("正在分析：第一轮抽取 JD，第二轮匹配画像..."):
                try:
                    api.analyze_job(int(job_id), force=False)
                except ApiError as exc:
                    st.error(exc.message)
                    return
            # 不把分析塞 session：rerun 后重新从数据库取，避免缓存与库不一致。
            st.rerun()

    st.divider()
    if st.button("找相似职位（RAG）", use_container_width=False):
        with st.spinner("检索相似职位..."):
            try:
                similar = api.similar_jobs(int(job_id), limit=5)
            except ApiError as exc:
                st.error(exc.message)
                similar = []
        st.session_state["similar_jobs"] = similar
    similar = st.session_state.get("similar_jobs") or []
    if similar:
        st.markdown("#### 相似职位")
        for item in similar:
            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                with c1:
                    st.markdown(f"**{item.get('title')}** · {item.get('company')}")
                    st.caption(f"{item.get('city')}｜{item.get('salary_text')}｜{item.get('source')}")
                with c2:
                    if st.button("查看", key=f"similar_{item['id']}", use_container_width=True):
                        st.session_state.selected_job_id = item["id"]
                        st.rerun()

    st.caption("提示：本页的 AI 结论不是招聘方承诺；投递前请核对原站信息，保护个人隐私。")
