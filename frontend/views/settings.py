# ============================================================
# 用户设置：显示名/头像、简历版本、采集令牌、反馈、改密。
# 注意：登录账号（username）不可改；显示名（display_name）可改。
# ============================================================
from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import ui
from frontend.api_client import ApiClient, ApiError
from frontend.state import clear_login
from frontend.validators import password_error


def _render_account(api: ApiClient, user: dict[str, Any]) -> None:
    """显示名 + 头像。"""
    st.markdown("### 账号与头像")
    st.caption(f"登录账号：{user.get('username')}（不可修改）｜角色：{user.get('role')}")
    with st.form("display_name_form"):
        name = st.text_input("显示名", value=user.get("display_name") or user.get("username") or "", max_chars=64)
        submitted = st.form_submit_button("保存显示名", use_container_width=True)
    if submitted:
        try:
            new_user = api.update_display_name(name.strip())
            st.session_state.user = new_user
            st.success("显示名已更新")
            st.rerun()
        except ApiError as exc:
            st.error(exc.message)

    st.markdown("#### 头像")
    avatar = st.session_state.get("avatar_bytes")
    if avatar is None:
        try:
            avatar = api.avatar_bytes()
            st.session_state.avatar_bytes = avatar
        except ApiError as exc:
            st.warning(exc.message)
    if avatar:
        st.image(avatar, width=120)
    uploaded = st.file_uploader("上传新头像（PNG/JPG/WebP，≤2MB）", type=["png", "jpg", "jpeg", "webp"], key="avatar_upload")
    st.caption("头像会显示在左下角、首页问候区和这里。")
    if st.button("上传头像", disabled=uploaded is None):
        if uploaded is None:
            return
        try:
            result = api.upload_avatar(uploaded.name, uploaded.getvalue(), uploaded.type or "application/octet-stream")
            # 上传成功后立即取回新图，避免 rerun 时还拿着旧缓存。
            try:
                st.session_state.avatar_bytes = api.avatar_bytes()
            except ApiError:
                st.session_state.avatar_bytes = None
            st.toast(result.get("message", "头像已更新"), icon="🖼️")
            st.rerun()
        except ApiError as exc:
            st.error(exc.message)

    st.info("登录账号和显示名是两回事：账号只用于登录，显示名可以随时改。")


def _render_resumes(api: ApiClient) -> None:
    """简历上传/版本/回档。"""
    st.markdown("### 简历版本")
    st.caption("上传后文件会保存在本机；AI 解析只生成草稿，不会覆盖画像。")
    uploaded = st.file_uploader("上传 PDF/Word", type=["pdf", "docx"], key="settings_resume")
    if st.button("上传并解析", disabled=uploaded is None, key="settings_resume_btn"):
        if uploaded is None:
            return
        with st.spinner("解析简历中..."):
            try:
                result = api.upload_resume(uploaded.name, uploaded.getvalue(), uploaded.type or "application/octet-stream")
            except ApiError as exc:
                st.error(exc.message)
                return
        st.session_state["profile_prefill"] = result.get("parsed_profile") or {}
        if result.get("warning"):
            st.warning(result["warning"])
        st.success("已保存新版本；解析结果可在画像页确认。")
        st.rerun()

    if st.session_state.get("profile_prefill"):
        st.info("有一份简历解析草稿未应用到画像。")
        if st.button("去画像页确认预填", type="primary"):
            st.session_state.page = "onboarding"
            st.rerun()

    try:
        items = api.list_resumes()
    except ApiError as exc:
        st.error(exc.message)
        return
    if not items:
        st.caption("还没有上传简历。")
        return
    for item in items:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([2.2, 1.4, 1.2, 1.6])
            with c1:
                st.markdown(f"**{item.get('original_name')}**")
                st.caption(f"v{item.get('version')}｜{ui.human_size(item.get('size_bytes'))}｜{ui.format_datetime(item.get('created_at'))}")
            with c2:
                if item.get("is_active"):
                    st.markdown(ui.badge("当前使用", "#0f766e", "#ccfbf1"), unsafe_allow_html=True)
                else:
                    st.markdown(ui.badge("历史版本", "#64748b", "#f1f5f9"), unsafe_allow_html=True)
            with c3:
                if st.button("准备下载", key=f"resume_dl_{item['id']}", use_container_width=True):
                    try:
                        content, filename = api.download_resume(item["id"])
                        st.session_state[f"resume_file_{item['id']}"] = (content, filename)
                    except ApiError as exc:
                        st.error(exc.message)
                saved = st.session_state.get(f"resume_file_{item['id']}")
                if saved:
                    # download_button 需要数据在当前 rerun 里；先存 session 再渲染，
                    # 避免点“下载”后 rerun 把按钮自己弄没了。
                    st.download_button(
                        "保存文件",
                        data=saved[0],
                        file_name=saved[1],
                        key=f"resume_save_{item['id']}",
                        use_container_width=True,
                    )
            with c4:
                if not item.get("is_active"):
                    if st.button("设为当前", key=f"resume_activate_{item['id']}", use_container_width=True):
                        try:
                            api.activate_resume(item["id"])
                            st.success("已设为当前简历")
                            st.rerun()
                        except ApiError as exc:
                            st.error(exc.message)


def _render_extension(api: ApiClient) -> None:
    """扩展下载与使用；本机已关闭令牌校验，不再要求生成令牌。"""
    st.markdown("### 浏览器采集")
    st.success("本机已关闭令牌校验：不用生成令牌，扩展只填 API 地址即可。")

    st.markdown("#### 第 1 步：下载扩展")
    if st.button("下载扩展 ZIP", use_container_width=False):
        try:
            st.session_state["extension_zip"] = api.download_extension()
            st.toast("扩展 ZIP 已准备好", icon="📦")
        except ApiError as exc:
            st.error(exc.message)
    zip_bytes = st.session_state.get("extension_zip")
    if zip_bytes:
        st.download_button(
            "保存 careerzone-extension.zip",
            data=zip_bytes,
            file_name="careerzone-extension.zip",
            mime="application/zip",
            use_container_width=False,
        )
    st.caption("解压后得到 careerzone-extension 文件夹；Edge 扩展页加载这个文件夹。")

    with st.expander("第 2 步：安装与使用", expanded=False):
        st.markdown(
            """
            1. Edge 打开 **edge://extensions**，开启“开发人员模式”。
            2. 点“加载解压缩的扩展”，选择解压后的 **careerzone-extension** 文件夹。
            3. 点扩展图标，API 地址填 **http://127.0.0.1:8000**，保存。
            4. **要做两轮分析**：打开职位详情页，点“采集当前页”，覆盖列表页的摘要。
            5. 采集后回职位板搜索；扩展弹窗会显示新增/更新/重复率，也可以清空本机职位库。
            """
        )
    st.info("列表页采集只包含卡片摘要；两轮分析会检查 JD 是否完整，摘要不够会拒绝分析，避免 AI 编造。")


def _render_feedback(api: ApiClient) -> None:
    """提交反馈 + 查看管理员回复。"""
    st.markdown("### 意见反馈")
    with st.form("feedback_form"):
        category = st.selectbox("类型", ["功能建议", "网站评价", "Bug 反馈", "数据问题"])
        rating = st.slider("整体评分", 1, 5, 5)
        content = st.text_area("内容", height=120, max_chars=2000, placeholder="哪里不好用？希望加什么？")
        submitted = st.form_submit_button("提交反馈", type="primary", use_container_width=True)
    if submitted:
        if len(content.strip()) < 5:
            st.error("请至少写 5 个字")
            return
        try:
            api.create_feedback(category, content.strip(), int(rating))
            st.success("已提交，感谢反馈。")
        except ApiError as exc:
            st.error(exc.message)

    try:
        items = api.my_feedback()
    except ApiError as exc:
        st.warning(exc.message)
        return
    if not items:
        st.caption("还没有反馈记录。")
        return
    for item in items:
        with st.container(border=True):
            st.markdown(f"**{item.get('category')}** · 评分 {item.get('rating')}/5 · {item.get('status')}")
            st.write(item.get("content"))
            if item.get("admin_reply"):
                st.info(f"管理员回复（{item.get('level')}）：{item.get('admin_reply')}")
            else:
                st.caption("等待管理员处理。")


def _render_security(api: ApiClient) -> None:
    """修改密码；管理员密码固定，不走这里。"""
    st.markdown("### 修改密码")
    st.caption("修改成功后所有设备会退出登录，需要重新登录。")
    with st.form("change_password_form"):
        old = st.text_input("原密码", type="password", autocomplete="current-password")
        new = st.text_input("新密码", type="password", autocomplete="new-password")
        confirm = st.text_input("确认新密码", type="password", autocomplete="new-password")
        submitted = st.form_submit_button("修改密码", type="primary", use_container_width=True)
    if submitted:
        pwd_error = password_error(new)
        if pwd_error:
            st.error(pwd_error)
            return
        if new != confirm:
            st.error("两次输入的新密码不一致")
            return
        try:
            result = api.change_password(old, new)
            clear_login()
            st.success(result.get("message", "密码已修改"))
            st.info(result.get("detail", "请重新登录。"))
            st.rerun()
        except ApiError as exc:
            st.error(exc.message)
    st.caption("密码规则：6—20 位；纯数字、纯字母、混合都可以。")


def render_settings(api: ApiClient) -> None:
    """设置页：所有用户自助操作集中在这里。"""
    ui.page_header("设置", "账号、简历、采集令牌、反馈和安全。")
    user = st.session_state.get("user") or {}
    tabs = st.tabs(["账号与头像", "简历", "浏览器采集", "反馈", "安全"])
    with tabs[0]:
        _render_account(api, user)
    with tabs[1]:
        _render_resumes(api)
    with tabs[2]:
        _render_extension(api)
    with tabs[3]:
        _render_feedback(api)
    with tabs[4]:
        _render_security(api)
