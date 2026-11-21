# ============================================================
# 用户首页：把“下一步该做什么”讲清楚，而不是堆功能。
# 主要展示画像完善度、当前简历、收藏/历史入口、热门职位。
# ============================================================
from __future__ import annotations

import streamlit as st

from frontend import ui
from frontend.api_client import ApiClient, ApiError


@st.dialog("欢迎使用 CareerZone X")
def _welcome_dialog() -> None:
    """第一次进首页时用弹窗讲清边界，避免用户以为它会自动爬岗位。"""
    st.markdown(
        """
        **这是一个本地求职工作台，不是自动爬虫。**
        - 先完善画像；
        - 职位需要你用扩展采集当前页，或手动粘贴 JD；
        - AI 只做初筛/分析，最终以原站和 HR 回复为准。
        """
    )
    if st.button("知道了，开始", type="primary", use_container_width=True):
        st.session_state.welcome_seen = True
        st.rerun()


def render_home(api: ApiClient) -> None:
    """用户登录后的默认页。"""
    if not st.session_state.get("welcome_seen"):
        # 先置位再打开，避免用户按 ESC 关闭后每次 rerun 又弹。
        st.session_state.welcome_seen = True
        _welcome_dialog()
    user = st.session_state.get("user") or {}
    display = user.get("display_name") or user.get("username") or "你"
    head_l, head_r = st.columns([1, 11])
    with head_l:
        avatar = st.session_state.get("avatar_bytes")
        if avatar:
            st.image(avatar, width=72)
        else:
            st.markdown('<div class="czx-avatar-placeholder">🐟</div>', unsafe_allow_html=True)
    with head_r:
        ui.page_header(f"你好，{display}", "今天不追求投得多，先确保每一份都投得明白。")

    try:
        bundle = api.get_profile()
        resumes = api.list_resumes()
        bookmarks = api.bookmarks(offset=0, limit=1)
    except ApiError as exc:
        st.error(exc.message)
        return

    profile = bundle.get("profile") or {}
    completeness = int(bundle.get("completeness") or 0)
    active_resume = next((item for item in resumes if item.get("is_active")), None)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("画像完善度", f"{completeness}%")
    c2.metric("当前简历", "已上传" if active_resume else "未上传")
    c3.metric("收藏职位", int(bookmarks.get("total") or 0))
    c4.metric("目标岗位", len(profile.get("target_roles") or []))

    st.progress(min(100, completeness) / 100)
    if completeness < 60:
        st.warning("画像还不完整。AI 仍可做模糊匹配，但补齐目标城市/薪资/技能后，判断会准很多。")
    if not active_resume:
        st.info("还没有上传简历。上传 PDF/Word 后，AI 可以预填画像，也能在设置里回看版本。")

    st.markdown("### 下一步")
    steps = st.columns(3)
    with steps[0]:
        with st.container(border=True):
            st.markdown("**1. 补齐画像**")
            st.caption("目标岗位、城市、薪资、技能；填一半也能开始。")
            if st.button("去完善画像", key="home_profile", use_container_width=True):
                st.session_state.page = "onboarding"
                st.rerun()
    with steps[1]:
        with st.container(border=True):
            st.markdown("**2. 采集一页职位**")
            st.caption("打开招聘网站，用扩展采集当前页；或粘贴 JD 手动导入。")
            if st.button("去职位板", key="home_jobs", type="primary", use_container_width=True):
                st.session_state.page = "jobs"
                st.rerun()
    with steps[2]:
        with st.container(border=True):
            st.markdown("**3. 看 AI 判断**")
            st.caption("列表先初筛；点进详情做两轮分析，再决定是否投。")
            if st.button("看浏览历史", key="home_history", use_container_width=True):
                st.session_state.page = "history"
                st.rerun()

    st.markdown("### 最近被浏览最多的职位")
    try:
        top = api.top_viewed(limit=5).get("items") or []
    except ApiError as exc:
        st.warning(exc.message)
        top = []
    if not top:
        st.caption("还没有职位数据。去职位板采集第一页，或在设置里手动粘贴 JD。")
    for job in top:
        with st.container(border=True):
            left, right = st.columns([3.2, 1])
            with left:
                st.markdown(f"**{job.get('title', '未知职位')}** · {job.get('company', '未知公司')}")
                st.caption(f"{job.get('city', '')}｜{ui.format_salary(job)}｜{job.get('source_label', '')}｜浏览 {job.get('view_count', 0)} 次")
            with right:
                if st.button("查看", key=f"home_top_{job['id']}", use_container_width=True):
                    st.session_state.selected_job_id = job["id"]
                    st.session_state.page = "job_detail"
                    st.session_state["scroll_top"] = True
                    st.rerun()

    st.divider()
    with st.expander("为什么不做“全自动爬虫”？", expanded=False):
        st.markdown(
            """
            BOSS、猎聘、智联、51JOB、鱼泡的页面和接口受平台条款约束。
            自动登录、翻页、绕验证码、批量抓取，既容易封号，也可能违反平台规则。
            本项目采用**用户手动打开页面 → 点扩展采集当前页**的方式：
            只处理你本来就能看到的信息，不碰账号密码，不高频请求。
            """
        )
