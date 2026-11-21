# ============================================================
# 画像页：用户填写目标岗位/城市/薪资/技能，或上传简历预填。
# 允许“填一半”：只要目标岗位或技能有一个，就能开始匹配。
# ============================================================
from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import ui
from frontend.api_client import ApiClient, ApiError
from frontend.state import set_flash

CITIES = ["苏州", "上海", "杭州", "南京", "无锡", "重庆", "广州", "深圳", "北京", "成都", "武汉"]
ROLES = [
    # 互联网/AI（来自用户提供的 BOSS 职位分类截图）
    "算法工程师", "算法研究员", "深度学习工程师", "机器学习工程师", "NLP 算法工程师",
    "计算机视觉工程师", "大模型算法工程师", "推荐算法工程师", "搜索算法工程师",
    "语音算法工程师", "自动驾驶系统工程师", "数据挖掘工程师", "数据标注/AI 训练师",
    # 产品
    "AI 产品经理", "产品经理", "产品专员/助理", "产品总监", "用户研究",
    "电商产品经理", "数据产品经理", "移动产品经理", "金融产品经理",
    "跨境电商产品开发", "游戏策划", "系统策划", "游戏数值策划",
    # 电子/电气/通信
    "电子工程师", "硬件工程师", "通信工程师", "射频工程师",
    # 客服/运营
    "客服专员", "客服主管", "客服经理", "网络客服", "电话客服", "售后客服", "售前客服",
    "内容运营", "新媒体运营", "直播运营", "视频运营", "微信运营",
    "电商运营", "跨境电商运营", "品类运营", "淘宝运营", "天猫运营",
    "京东运营", "拼多多运营", "亚马逊运营", "阿里国际站运营",
    "运营助理/专员", "产品运营", "用户运营", "商家运营", "数据/策略运营",
    "社群运营", "游戏运营", "网站运营", "内容审核", "外卖运营",
    "线下拓展运营", "商场运营", "文案编辑", "网站编辑", "医学编辑",
    "运营经理/主管", "运营总监", "COO", "客服总监",
    # 销售
    "销售专员", "电话销售", "网络销售", "渠道销售", "大客户代表",
    "客户经理", "BD 经理", "销售工程师", "销售经理/主管", "销售总监",
    "销售 VP", "城市经理", "区域总监", "销售助理", "销售运营",
    "商务专员", "商务经理", "商务总监", "招商", "客户成功", "业务跟单",
    "外贸业务员", "外贸经理", "货代/物流销售", "海外销售",
    "课程顾问", "留学顾问", "汽车销售", "汽车配件销售",
    "置业顾问", "地产中介", "地产招商",
    "导购", "服装导购", "珠宝销售", "美容顾问", "化妆品导购",
    "家居导购", "会籍顾问", "旅游顾问",
    "医药代表", "医疗器械销售", "药店店员", "药店店长",
    "医美咨询", "健康顾问", "口腔咨询师",
    "广告销售", "会展活动销售", "会议活动销售",
    # 人力/行政/法务
    "人力资源专员/助理", "人力资源经理/主管", "人力资源总监",
    "招聘", "HRBP", "培训", "员工关系", "组织发展", "企业文化",
    "薪酬绩效", "猎头顾问", "行政专员/助理", "行政经理/主管",
    "行政总监", "前台", "后勤", "经理助理", "文员", "企业党建",
    "档案管理", "商务司机", "项目专员/助理", "录入员",
    "法务专员/助理", "法务经理/主管", "法务总监", "法律顾问", "律师",
    # 财务/审计/税务
    "会计", "总账会计", "成本会计", "出纳", "审计", "税务",
    # 技工/普工/生产制造
    "普工/操作工", "包装工", "学徒工", "生产计划", "质量工程师",
    "工艺工程师", "设备工程师",
    # 零售/生活服务
    "店员/营业员", "收银", "店长",
    # 云计算/IT（原有方向，保留）
    "云计算工程师", "云运维", "系统运维", "网络工程师", "实施工程师",
    "驻场运维", "桌面运维", "数据库管理员", "测试工程师", "Python 开发",
    "Java 开发", "技术支持", "IT 专员", "AI 应用交付", "AI 应用开发",
    "AI 实施工程师", "RAG 工程师", "知识库工程师",
]
SKILLS = [
    "Python", "FastAPI", "RAG", "Chroma", "DeepSeek", "Prompt", "Streamlit",
    "Docker", "Linux", "MySQL", "Git", "Nginx", "Redis", "Java", "SpringBoot", "Vue",
    "云计算", "Windows Server", "VMware", "Kubernetes", "Shell", "SQL",
    "网络基础", "阿里云", "华为云", "Zabbix", "Ansible",
]
INDUSTRIES = ["互联网", "人工智能", "企业服务", "制造业", "餐饮/零售", "云计算", "外包/集成商"]
SIZES = ["0-20人", "20-99人", "100-499人", "500-999人", "1000-9999人", "10000人以上"]
EXPERIENCES = ["", "应届", "1年以内", "1-3年", "3-5年", "5-10年"]
EDUCATIONS = ["", "不限", "大专", "本科", "硕士"]


def _merge_draft(profile: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    """简历解析结果只补空字段，不覆盖用户手填内容。"""
    merged = dict(profile or {})
    scalar_fields = ["real_name", "phone", "email", "city", "job_type", "experience", "education", "bio", "github", "portfolio"]
    for key in scalar_fields:
        if not merged.get(key) and draft.get(key):
            merged[key] = draft[key]
    for key in ["target_cities", "target_roles", "skills", "industries", "preferred_company_sizes"]:
        current = list(merged.get(key) or [])
        for item in draft.get(key) or []:
            text = str(item).strip()
            if text and text not in current:
                current.append(text)
        merged[key] = current
    for key in ["salary_min", "salary_max"]:
        if merged.get(key) is None and draft.get(key) is not None:
            merged[key] = draft[key]
    return merged


def _options(base: list[str], current: list[str]) -> list[str]:
    """下拉选项 = 常用项 + 当前值，避免历史数据不在选项里被清空。"""
    result = list(base)
    for item in current:
        if item and item not in result:
            result.append(item)
    return result


def _render_resume_upload(api: ApiClient) -> None:
    """简历上传作为“预填工具”，不直接保存到画像。"""
    with st.expander("上传 PDF/Word 简历，自动预填（可选）", expanded=False):
        st.caption("支持 .pdf / .docx，最大 10MB；扫描版 PDF 无文字，需要先 OCR。AI 结果只做草稿。")
        uploaded = st.file_uploader("选择简历", type=["pdf", "docx"], key="onboarding_resume")
        if st.button("上传并解析", key="onboarding_resume_btn", disabled=uploaded is None):
            if uploaded is None:
                return
            with st.spinner("解析简历中。首次调用 DeepSeek 可能需要几十秒..."):
                try:
                    result = api.upload_resume(uploaded.name, uploaded.getvalue(), uploaded.type or "application/octet-stream")
                except ApiError as exc:
                    st.error(exc.message)
                    return
            draft = result.get("parsed_profile") or {}
            base = st.session_state.get("profile_bundle", {}).get("profile") or {}
            st.session_state["profile_prefill"] = _merge_draft(base, draft)
            if result.get("warning"):
                st.warning(result["warning"])
            st.success(f"已解析（{result.get('parse_mode')}）。表单已预填，请核对后保存。")
            st.rerun()


def render_onboarding(api: ApiClient) -> None:
    """画像页主入口；支持从设置页/简历页跳转。"""
    ui.page_header("我的求职画像", "不要求一次填完。目标岗位/技能至少写一个，AI 才能开始匹配。")
    try:
        bundle = api.get_profile()
    except ApiError as exc:
        st.error(exc.message)
        return
    st.session_state["profile_bundle"] = bundle
    profile = bundle.get("profile") or {}
    prefill = st.session_state.pop("profile_prefill", None)
    if prefill:
        profile = prefill
        st.info("已用简历解析结果预填；请逐项核对，尤其是手机号、邮箱、薪资和工作经验。")

    completeness = int(bundle.get("completeness") or 0)
    st.progress(min(100, completeness) / 100, text=f"画像完善度 {completeness}%")

    # 用户反馈“保存后还要滚到底部才能去职位板”，把入口放到顶部。
    top_action, top_hint = st.columns([1.1, 4])
    with top_action:
        if st.button("去职位板 →", type="primary", use_container_width=True):
            st.session_state.page = "jobs"
            st.rerun()
    with top_hint:
        st.caption("保存画像后从这里直接进职位板；也可以先上传简历预填。")

    _render_resume_upload(api)

    with st.form("profile_form"):
        st.markdown("### 基本信息")
        c1, c2, c3 = st.columns(3)
        with c1:
            real_name = st.text_input("姓名", value=profile.get("real_name", ""), max_chars=64)
        with c2:
            phone = st.text_input("手机号", value=profile.get("phone", ""), max_chars=32)
        with c3:
            email = st.text_input("邮箱", value=profile.get("email", ""), max_chars=128)

        c4, c5 = st.columns(2)
        with c4:
            city = st.text_input("现居城市", value=profile.get("city", ""), max_chars=64)
        with c5:
            job_type = st.selectbox(
                "求职类型",
                ["全职", "兼职", "实习", "不限"],
                index=["全职", "兼职", "实习", "不限"].index(profile.get("job_type") or "全职"),
            )

        st.markdown("### 目标方向")
        target_roles = st.multiselect(
            "目标岗位（可自定义，输入后回车）",
            options=_options(ROLES, profile.get("target_roles") or []),
            default=profile.get("target_roles") or [],
            accept_new_options=True,
            help="至少填一个；越具体越好，例如“AI 应用交付”“RAG 工程师”。",
        )
        target_cities = st.multiselect(
            "目标城市（可多选）",
            options=_options(CITIES, profile.get("target_cities") or []),
            default=profile.get("target_cities") or [],
            accept_new_options=True,
        )
        skills = st.multiselect(
            "技能关键词（可自定义）",
            options=_options(SKILLS, profile.get("skills") or []),
            default=profile.get("skills") or [],
            accept_new_options=True,
        )

        c6, c7, c8 = st.columns(3)
        with c6:
            salary_min = st.number_input(
                "期望月薪下限（元，0=不填）",
                min_value=0, max_value=100000, step=500,
                value=int(profile.get("salary_min") or 0),
            )
        with c7:
            salary_max = st.number_input(
                "期望月薪上限（元，0=不填）",
                min_value=0, max_value=100000, step=500,
                value=int(profile.get("salary_max") or 0),
            )
        with c8:
            experience = st.selectbox(
                "工作经验",
                EXPERIENCES,
                index=EXPERIENCES.index(profile.get("experience") or "") if (profile.get("experience") or "") in EXPERIENCES else 0,
            )

        c9, c10 = st.columns(2)
        with c9:
            education = st.selectbox(
                "最高学历",
                EDUCATIONS,
                index=EDUCATIONS.index(profile.get("education") or "") if (profile.get("education") or "") in EDUCATIONS else 0,
            )
        with c10:
            company_sizes = st.multiselect(
                "偏好公司规模",
                options=_options(SIZES, profile.get("preferred_company_sizes") or []),
                default=profile.get("preferred_company_sizes") or [],
            )

        industries = st.multiselect(
            "偏好行业",
            options=_options(INDUSTRIES, profile.get("industries") or []),
            default=profile.get("industries") or [],
            accept_new_options=True,
        )
        bio = st.text_area(
            "自我描述 / 项目亮点",
            value=profile.get("bio", ""),
            max_chars=4000,
            height=130,
            placeholder="例如：1.5 年企业 IT 交付，做过 RAG 知识库，能独立写 FastAPI + Streamlit + Docker。",
        )
        c11, c12 = st.columns(2)
        with c11:
            github = st.text_input("GitHub / 作品集链接", value=profile.get("github", ""), max_chars=255)
        with c12:
            portfolio = st.text_input("其他链接（可选）", value=profile.get("portfolio", ""), max_chars=255)

        submitted = st.form_submit_button("保存画像", type="primary", use_container_width=True)

    if submitted:
        if int(salary_min) and int(salary_max) and int(salary_min) > int(salary_max):
            st.error("最低薪资不能高于最高薪资")
            return
        payload = {
            "real_name": real_name.strip(),
            "phone": phone.strip(),
            "email": email.strip(),
            "city": city.strip(),
            "target_cities": target_cities,
            "target_roles": target_roles,
            "job_type": job_type,
            "salary_min": int(salary_min) or None,
            "salary_max": int(salary_max) or None,
            "experience": experience,
            "education": education,
            "skills": skills,
            "industries": industries,
            "preferred_company_sizes": company_sizes,
            "bio": bio.strip(),
            "github": github.strip(),
            "portfolio": portfolio.strip(),
        }
        try:
            result = api.save_profile(payload)
        except ApiError as exc:
            st.error(exc.message)
            return
        # 不要先 st.success 再 rerun：rerun 会把这条提示清掉，用户就以为“没反应”。
        # 用跨 rerun 的 flash，下一次页面渲染时显示，并明确告诉他下一步。
        completeness = result.get("completeness", 0)
        if not payload["target_roles"] and not payload["skills"]:
            set_flash(
                f"画像已保存，完善度 {completeness}%。但还没填目标岗位/技能，AI 无法匹配；请至少补一个，再去职位板。",
                "warning",
            )
        else:
            set_flash(
                f"画像已保存，完善度 {completeness}%。下一步：去职位板采集 1 页职位，或手动粘贴 JD。",
                "success",
            )
        st.session_state["profile_bundle"] = {"profile": result, "completeness": completeness}
        st.rerun()


