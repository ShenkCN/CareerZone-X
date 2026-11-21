# ============================================================
# 职位板：筛选、20 条/页、AI 初筛、换一批、手动导入。
# 关键交互：
# - 换一批 = 带着本页 ID 排除，服务端再排除历史曝光；
# - 重复率/是否池子空了由服务端算，前端只展示；
# - 搜索结果存 session_state，收藏/详情操作后不会丢整页。
# ============================================================
from __future__ import annotations

from typing import Any
from urllib.parse import quote

import streamlit as st

from frontend import ui
from frontend.api_client import ApiClient, ApiError

SOURCES_FALLBACK = [
    {"value": "boss", "label": "BOSS直聘"},
    {"value": "liepin", "label": "猎聘"},
    {"value": "zhaopin", "label": "智联招聘"},
    {"value": "51job", "label": "51JOB"},
    {"value": "yupao", "label": "鱼泡招聘"},
    {"value": "official", "label": "企业官网"},
    {"value": "manual", "label": "手动导入"},
]
EXPERIENCES = ["", "应届", "1年以内", "1-3年", "3-5年", "5-10年"]
EDUCATIONS = ["", "不限", "大专", "本科", "硕士"]
JOB_TYPES = ["", "全职", "兼职", "实习", "不限"]
SIZES = ["", "0-20人", "20-99人", "100-499人", "500-999人", "1000-9999人", "10000人以上"]


def _fmt_optional(value: str) -> str:
    """空字符串在筛选器里表示“不限”，不要显示 Streamlit 默认的 Choose an option。"""
    return value if value else "不限"


def _sources(api: ApiClient) -> list[dict[str, str]]:
    """来源清单缓存 10 分钟，避免每次 rerun 都请求。"""
    cached = st.session_state.get("sources_cache")
    if cached:
        return cached
    try:
        items = api.sources()
    except ApiError:
        items = SOURCES_FALLBACK
    st.session_state["sources_cache"] = items
    return items


def _build_filters(values: dict[str, Any]) -> dict[str, Any]:
    """把表单值转成后端 JobSearchIn；0 薪资表示不筛。"""
    def _unlimited(value: Any) -> str:
        # 下拉框里“不限”是显示文案，也可能作为值存在；不能拿它去等值查询。
        text = str(value or "").strip()
        return "" if text in {"", "不限"} else text

    return {
        "keyword": (values.get("keyword") or "").strip(),
        "source": values.get("source") or "",
        "city": (values.get("city") or "").strip(),
        "job_type": _unlimited(values.get("job_type")),
        "salary_min": int(values["salary_min"]) if values.get("salary_min") else None,
        "salary_max": int(values["salary_max"]) if values.get("salary_max") else None,
        "experience": _unlimited(values.get("experience")),
        "education": _unlimited(values.get("education")),
        "industry": (values.get("industry") or "").strip(),
        "company_size": _unlimited(values.get("company_size")),
        "screen": bool(values.get("screen", True)),
        "offset": 0,
        "exclude_ids": [],
        "reset_exclusions": True,
    }


def _run_search(
    api: ApiClient,
    filters: dict[str, Any],
    *,
    exclude_ids: list[int] | None = None,
    reset_exclusions: bool = True,
    screen: bool = True,
    offset: int = 0,
) -> dict[str, Any] | None:
    """调用搜索接口并写入 session_state；失败返回 None。"""
    payload = dict(filters)
    payload.update(
        {
            "offset": offset,
            "exclude_ids": exclude_ids or [],
            "screen": screen,
            "reset_exclusions": reset_exclusions,
        }
    )
    with st.spinner("AI 正在初筛这一页（首次会调用 DeepSeek，可能需要几十秒）..." if screen else "加载职位中..."):
        try:
            result = api.search_jobs(payload)
        except ApiError as exc:
            st.error(exc.message)
            return None
    st.session_state.jobs_result = result
    st.session_state.job_filters = filters
    st.session_state.jobs_offset = offset
    count = len(result.get("items") or [])
    st.toast(
        f"搜索完成：本页 {count} 条，职位库共 {result.get('total', 0)} 条" if count else "搜索完成：0 条。本机职位库为空时，请先采集/粘贴职位。",
        icon="✅" if count else "⚠️",
    )
    return result


def _status_text(result: dict[str, Any]) -> None:
    """把后端的初筛状态翻译成人话，并明确规则兜底。"""
    status = result.get("screen_status")
    if status == "ai":
        st.caption("✅ 本页由 DeepSeek 批量初筛；点进详情可做两轮深度分析。")
    elif status == "cached":
        st.caption("⚡ 本页使用已有初筛缓存，没有重复消耗 Token。")
    elif status == "ai_capped":
        st.warning(result.get("screen_error") or "本页职位较多，只初筛了前 300 条；其余点详情按需分析。")
    elif status == "ai_partial":
        st.warning(result.get("screen_error") or "模型漏判了部分职位，已用规则补齐。")
    elif status == "rules":
        st.info(result.get("screen_error") or "未配置 DeepSeek，本页使用规则初筛。")
    elif status == "fallback":
        st.warning(result.get("screen_error") or "AI 初筛失败，本页使用规则兜底。")
    else:
        st.caption("本页未请求 AI 初筛。")
    if result.get("notice"):
        st.info(result["notice"])
    overlap = float(result.get("overlap_rate") or 0)
    if overlap:
        st.caption(f"与上一批重复率：{overlap * 100:.1f}%（目标 ≤20%）")


def _render_empty_help(api: ApiClient, filters: dict[str, Any], total: int, profile: dict[str, Any]) -> None:
    """0 条结果时，不要只留一句“没职位”；要告诉用户为什么、下一步点哪。"""
    if total == 0:
        st.warning(
            "本机职位库现在是 0 条。搜索只检索你已经采集/粘贴到本机的职位，"
            "不会自动访问招聘网站——所以点搜索不会凭空变出岗位。"
        )
        st.markdown(
            """
            **先做这三步：**
            1. 在“设置 → 采集令牌”生成令牌；
            2. Edge 加载 extension 文件夹；
            3. 手动打开招聘网站，点扩展“采集当前页”；或直接粘贴 JD。
            """
        )
    else:
        st.info("当前筛选下没有新的职位；可以放宽城市/薪资条件，或换一批。")

    # 搜索词要跟着画像/关键词走，不能永远写死“云计算”。
    role_values = [str(item).strip() for item in (profile.get("target_roles") or []) if str(item).strip()]
    skill_values = [str(item).strip() for item in (profile.get("skills") or []) if str(item).strip()]
    query = (
        (filters.get("keyword") or "").strip()
        or (role_values[0] if role_values else "")
        or (skill_values[0] if skill_values else "")
        or "计算机"
    )
    q = quote(query)
    st.markdown(f"#### 打开招聘网站搜索“{query}”")
    st.caption("搜索词取自本页关键词；没有关键词时取画像里的第一个目标岗位/技能。")
    st.caption("下面只是打开网站，不会自动采集；打开后请用扩展采集当前页，或复制 JD 手动导入。")
    links = [
        ("BOSS直聘", f"https://www.zhipin.com/web/geek/jobs?query={q}"),
        ("猎聘", f"https://www.liepin.com/zhaopin/?key={q}"),
        ("智联招聘", f"https://sou.zhaopin.com/?kw={q}"),
        ("51JOB", f"https://we.51job.com/pc/search?keyword={q}"),
        ("鱼泡招聘", "https://www.yupao.com/"),
    ]
    cols = st.columns(len(links))
    for col, (label, url) in zip(cols, links):
        with col:
            st.link_button(label, url, use_container_width=True)

    c1, c2 = st.columns(2)
    with c1:
        if st.button("去设置生成采集令牌", use_container_width=True):
            st.session_state.page = "settings"
            st.rerun()
    with c2:
        st.caption("没有扩展也能用：在下方“手动粘贴 JD”里贴职位原文。")


def _render_manual_import(api: ApiClient) -> None:
    """不装扩展也能用：粘贴 JD 文本，AI 抽取成一条职位。"""
    with st.expander("手动粘贴 JD（不装扩展也能用）", expanded=False):
        st.caption("把你看到的职位描述整段粘贴进来。AI 只做结构化，不访问原网站。")
        with st.form("manual_job_form"):
            text = st.text_area("职位原文", height=180, placeholder="职位名称、公司、薪资、职责、要求……")
            url = st.text_input("原站链接（可选）", placeholder="https://www.zhipin.com/job_detail/...")
            source = st.selectbox(
                "来源",
                ["manual", "official", "boss", "liepin", "zhaopin", "51job", "yupao"],
                format_func=lambda value: {"manual": "手动导入", "official": "企业官网"}.get(value, value.upper()),
            )
            submitted = st.form_submit_button("解析并加入职位库", use_container_width=True)
        if submitted:
            if len(text.strip()) < 30:
                st.error("请至少粘贴 30 个字的职位描述")
                return
            with st.spinner("解析 JD 中..."):
                try:
                    job = api.manual_import(text.strip(), url.strip(), source)
                except ApiError as exc:
                    st.error(exc.message)
                    return
            st.success(f"已加入：{job.get('title')} · {job.get('company')}")
            st.session_state.jobs_initialized = False
            st.rerun()


def render_jobs(api: ApiClient) -> None:
    """职位板主视图。"""
    ui.page_header("职位板", "先看 AI 初筛，再点进详情做两轮分析；最终以原站信息为准。")

    try:
        bundle = api.get_profile()
    except ApiError as exc:
        st.error(exc.message)
        return
    profile = bundle.get("profile") or {}
    if not (profile.get("target_roles") or profile.get("skills")):
        st.warning("请先在“我的画像”填写目标岗位或技能，AI 才有判断依据。")
        if st.button("去填写画像", type="primary"):
            st.session_state.page = "onboarding"
            st.rerun()
        return

    sources = _sources(api)
    source_values = [item["value"] for item in sources]
    source_labels = {item["value"]: item["label"] for item in sources}
    last_filters = st.session_state.get("job_filters") or {}

    # 扩展采集是另一个进程，Streamlit 不会自动收到事件；
    # 这里读最近一次导入统计，让用户看到“采到了多少、重复多少”。
    try:
        last_import = api.last_import()
    except ApiError:
        last_import = {"found": False}
    if last_import.get("found"):
        duplicate = float(last_import.get("duplicate_rate") or 0) * 100
        st.info(
            f"上次采集（{ui.format_datetime(last_import.get('created_at'))}）："
            f"新增 {last_import.get('inserted', 0)}｜更新 {last_import.get('updated', 0)}｜"
            f"跳过 {last_import.get('skipped', 0)}｜本批重复率 {duplicate:.0f}%。"
            " 采集后点下方“搜索”刷新职位板。"
        )
        st.caption("重复率只按“这一批”计算：100% = 本批全部已存在；不是整个职位库的重复率。")

    # 用版本号作为 form key；清空时换 key，让所有输入框回到默认值。
    filter_version = int(st.session_state.get("filter_version") or 0)
    with st.form(f"job_filter_form_{filter_version}"):
        # 只把最常用的三项放首屏；其余筛选收进展开区，避免表单占满一屏。
        # 第一行直接放“关键词/来源/城市/搜索”，少一整行按钮高度。
        c1, c2, c3, c4 = st.columns([2.2, 1, 1, 0.9])
        with c1:
            keyword = st.text_input("关键词", value=last_filters.get("keyword", ""), placeholder="岗位/公司/技能，支持模糊搜索")
        with c2:
            source = st.selectbox(
                "来源",
                [""] + source_values,
                index=([""] + source_values).index(last_filters.get("source", "")) if last_filters.get("source", "") in source_values else 0,
                format_func=lambda value: "全部来源" if not value else source_labels.get(value, value),
            )
        with c3:
            city = st.text_input("城市", value=last_filters.get("city", ""), placeholder="苏州/上海，留空=不限")
        with c4:
            st.markdown("<div style='height:27px'></div>", unsafe_allow_html=True)
            submitted = st.form_submit_button("搜索", type="primary", use_container_width=True)

        with st.expander("更多筛选（类型/薪资/经验/学历/行业/规模）", expanded=False):
            c4, c5, c6 = st.columns(3)
            with c4:
                job_type = st.selectbox(
                    "求职类型", JOB_TYPES,
                    index=JOB_TYPES.index(last_filters.get("job_type", "")) if last_filters.get("job_type", "") in JOB_TYPES else 0,
                    format_func=_fmt_optional,
                )
            with c5:
                salary_min = st.number_input("薪资下限（元/月，0=不限）", min_value=0, max_value=100000, step=500, value=int(last_filters.get("salary_min") or 0))
            with c6:
                salary_max = st.number_input("薪资上限（元/月，0=不限）", min_value=0, max_value=100000, step=500, value=int(last_filters.get("salary_max") or 0))

            c7, c8, c9, c10 = st.columns(4)
            with c7:
                experience = st.selectbox(
                    "经验", EXPERIENCES,
                    index=EXPERIENCES.index(last_filters.get("experience", "")) if last_filters.get("experience", "") in EXPERIENCES else 0,
                    format_func=_fmt_optional,
                )
            with c8:
                education = st.selectbox(
                    "学历", EDUCATIONS,
                    index=EDUCATIONS.index(last_filters.get("education", "")) if last_filters.get("education", "") in EDUCATIONS else 0,
                    format_func=_fmt_optional,
                )
            with c9:
                industry = st.text_input("公司行业", value=last_filters.get("industry", ""), placeholder="人工智能/企业服务")
            with c10:
                company_size = st.selectbox(
                    "公司规模", SIZES,
                    index=SIZES.index(last_filters.get("company_size", "")) if last_filters.get("company_size", "") in SIZES else 0,
                    format_func=_fmt_optional,
                )
            screen = st.toggle("本页使用 DeepSeek 初筛（关闭可省 Token）", value=True)

    if st.button("清空筛选", use_container_width=False):
        st.session_state.filter_version = filter_version + 1
        st.session_state.job_filters = {}
        st.session_state.jobs_result = None
        st.session_state.jobs_initialized = False
        st.session_state.exclude_ids = []
        st.rerun()

    filters = _build_filters(
        {
            "keyword": keyword, "source": source, "city": city, "job_type": job_type,
            "salary_min": salary_min, "salary_max": salary_max, "experience": experience,
            "education": education, "industry": industry, "company_size": company_size,
            "screen": screen,
        }
    )
    if submitted:
        if filters["salary_min"] and filters["salary_max"] and filters["salary_min"] > filters["salary_max"]:
            st.error("薪资下限不能高于上限")
            return
        _run_search(api, filters, reset_exclusions=True, screen=bool(screen), offset=0)
        st.session_state.jobs_initialized = True

    result = st.session_state.get("jobs_result")
    # 首次进入先秒出列表，不自动跑 AI：200+ 条初筛要等几十秒，
    # 用户点“搜索”时再按开关决定是否初筛（默认开）。
    if result is None and not st.session_state.get("jobs_initialized"):
        result = _run_search(api, filters, reset_exclusions=True, screen=False, offset=0)
        st.session_state.jobs_initialized = True

    if result is None:
        _render_manual_import(api)
        return

    items = result.get("items") or []
    # 用一行紧凑统计替代三个大 metric，给职位卡让出首屏空间。
    st.markdown(
        f'<div class="czx-result-line">筛选结果 <b>{int(result.get("total") or 0)}</b>'
        f'<span>·</span>本页 <b>{len(items)}</b>'
        f'<span>·</span>与上批重复 <b>{float(result.get("overlap_rate") or 0) * 100:.0f}%</b></div>'
        f'<div class="czx-muted" style="margin:-2px 0 8px 0">换一批会尽量排除已展示职位；池子空了会重新列出。</div>',
        unsafe_allow_html=True,
    )
    _status_text(result)

    if not items:
        _render_empty_help(api, st.session_state.get("job_filters") or filters, int(result.get("total") or 0), profile)
        _render_manual_import(api)
        return

    for job in items:
        action = ui.render_job_card(job, key_prefix="jobs")
        if action == "detail":
            st.session_state.selected_job_id = job["id"]
            st.session_state.page = "job_detail"
            st.session_state["scroll_top"] = True
            st.rerun()
        if action == "bookmark":
            try:
                state = api.bookmark(job["id"])
                job["bookmarked"] = bool(state.get("bookmarked"))
                st.toast(state.get("message", "已更新收藏"))
            except ApiError as exc:
                st.error(exc.message)
            st.rerun()

    st.divider()
    b1, b2, b3, b4 = st.columns([1.2, 1.2, 1.2, 3])
    with b1:
        if st.button("换一批", type="primary", use_container_width=True):
            current_ids = [int(job["id"]) for job in items]
            previous = list(st.session_state.get("exclude_ids") or [])
            excludes = list(dict.fromkeys(previous + current_ids))
            new_result = _run_search(
                api,
                st.session_state.get("job_filters") or filters,
                exclude_ids=excludes,
                reset_exclusions=False,
                screen=bool((st.session_state.get("job_filters") or {}).get("screen", True)),
                offset=0,
            )
            if new_result is not None:
                st.session_state.exclude_ids = excludes
                st.rerun()
    with b2:
        if st.button("重新搜索", use_container_width=True):
            st.session_state.exclude_ids = []
            _run_search(api, filters, reset_exclusions=True, screen=bool(screen), offset=0)
            st.rerun()
    with b3:
        if st.button("刷新页面数据", use_container_width=True):
            st.rerun()
    with b4:
        st.caption(f"批次 ID：{result.get('batch_id')}｜画像哈希：{str(result.get('profile_hash') or '')[:10]}")

    _render_manual_import(api)

    with st.expander("采集更多职位（浏览器扩展/合规边界）", expanded=False):
        st.markdown(
            """
            1. 打开 **设置 → 采集令牌**，生成令牌（只显示一次）。
            2. Edge 打开 edge://extensions，开启开发人员模式，加载本项目的 extension 文件夹。
            3. 在扩展里填 API 地址和令牌。
            4. 手动打开 BOSS/猎聘/智联/51JOB/鱼泡页面，点扩展“采集当前页”。

            扩展只读取当前页；不会自动登录、翻页、破解验证码。平台条款仍可能限制自动化采集。
            """
        )
