# ============================================================
# 登录/注册/忘记密码/管理员入口。
# 所有校验最终以后端为准；前端提示只是为了少一次往返。
# 管理员密码正确后还会停在“密保问答”，不会直接进后台。
# ============================================================
from __future__ import annotations

import streamlit as st

from frontend import ui
from frontend.api_client import ApiClient, ApiError
from frontend.state import set_login
from frontend.validators import password_error, username_error


def _handle_login_result(result: dict, api: ApiClient) -> bool:
    """处理登录返回：普通用户直接进；管理员进入二次验证。

    返回 True 表示已经完成登录（调用方 rerun），False 表示还需答题。
    """
    if result.get("requires_2fa"):
        st.session_state.admin_challenge = result.get("challenge_token")
        st.session_state.admin_question = result.get("question") or "请输入管理员密保答案"
        st.rerun()
    token = result.get("access_token")
    user = result.get("user")
    if token and user:
        set_login(token, user)
        api.set_token(token)
        st.rerun()
    return False


def _render_admin_challenge(api: ApiClient) -> None:
    """管理员第二步：只显示密保问题，不再要求输入密码。"""
    st.warning("管理员已通过密码校验，还需要回答密保问题。")
    st.markdown(f"**密保问题：** {st.session_state.get('admin_question') or '未设置'}")
    with st.form("admin_2fa_form"):
        answer = st.text_input("密保答案", type="password", autocomplete="off")
        submitted = st.form_submit_button("验证并进入后台", type="primary", use_container_width=True)
    if submitted:
        if not answer.strip():
            st.error("请输入密保答案")
            return
        try:
            result = api.admin_verify(st.session_state.get("admin_challenge") or "", answer.strip())
            token = result.get("access_token")
            user = result.get("user")
            if not token or not user:
                st.error("验证返回异常，请重新登录")
                return
            set_login(token, user)
            api.set_token(token)
            st.rerun()
        except ApiError as exc:
            st.error(exc.message)
    if st.button("返回重新登录", use_container_width=False):
        st.session_state.admin_challenge = None
        st.session_state.admin_question = ""
        st.rerun()


def _login_tab(api: ApiClient) -> None:
    """普通用户登录；管理员账号也会在这里触发二次验证。"""
    with st.form("login_form"):
        username = st.text_input("账号", placeholder="字母或数字，5—15 位", autocomplete="username")
        password = st.text_input("密码", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("登录", type="primary", use_container_width=True)
    if submitted:
        if not username.strip() or not password:
            st.error("请输入账号和密码")
            return
        try:
            result = api.login(username.strip(), password)
            _handle_login_result(result, api)
        except ApiError as exc:
            st.error(exc.message)
    st.caption("普通用户注册后需管理员审核；忘记密码也在管理员批准后重置。")
    _nudge_form(api)


def _register_tab(api: ApiClient) -> None:
    """注册表单：账号/密码规则在前端先提示，后端仍会再校验一次。"""
    st.caption("账号：5—15 位字母或数字，纯数字/纯字母均可；密码：6—20 位，纯数字/纯字母均可。")
    with st.form("register_form"):
        username = st.text_input("登录账号", placeholder="例如 12345678 或 Shenk2026", max_chars=15)
        display_name = st.text_input("显示名（可中文，不等于账号）", placeholder="例如 张三", max_chars=64)
        password = st.text_input("密码", type="password", autocomplete="new-password")
        st.caption("密码 6—20 位；纯数字、纯字母、混合都可以。")
        confirm = st.text_input("确认密码", type="password", autocomplete="new-password")
        submitted = st.form_submit_button("提交注册申请", type="primary", use_container_width=True)
    if submitted:
        account_error = username_error(username)
        if account_error:
            st.error(account_error)
            return
        pwd_error = password_error(password)
        if pwd_error:
            st.error(pwd_error)
            return
        if password != confirm:
            st.error("两次输入的密码不一致")
            return
        try:
            result = api.register(username.strip(), password, display_name.strip())
            st.session_state.pending_username = username.strip()
            st.success(result.get("message", "注册申请已提交"))
            st.info(result.get("detail", "管理员审核通过后即可登录。"))
            st.caption("右侧小游戏不是主线；等审核时无聊可以玩一局，玩完可以催管理员。")
        except ApiError as exc:
            st.error(exc.message)


def _nudge_form(api: ApiClient, compact: bool = True) -> None:
    """已有申请但刷新页面后，仍能输入账号催管理员。"""
    with st.expander("已提交注册？催一下管理员", expanded=False):
        st.caption("只对 pending 账号有效；不会绕过审核，也不会自动批准。")
        with st.form("nudge_form"):
            username = st.text_input("待审核账号", key="nudge_username", max_chars=32)
            submitted = st.form_submit_button("催管理员审批", use_container_width=True)
        if submitted:
            if not username.strip():
                st.error("请输入待审核账号")
                return
            try:
                result = api.nudge_approval(username.strip())
                st.success(result.get("message", "已提醒管理员"))
                if result.get("detail"):
                    st.caption(result["detail"])
                st.toast("已写入管理员反馈箱", icon="🔔")
            except ApiError as exc:
                st.error(exc.message)


def _render_pending_box(api: ApiClient) -> None:
    """注册成功后的等待区：明确告诉用户下一步，并提供催办按钮。"""
    pending = st.session_state.get("pending_username")
    if not pending:
        return
    with st.container(border=True):
        st.markdown("### 正在等管理员审批")
        st.markdown(f"账号 **{pending}** 已提交。管理员批准后，用这个账号和密码登录即可。")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("催管理员审批", type="primary", use_container_width=True):
                try:
                    result = api.nudge_approval(pending)
                    st.success(result.get("message", "已提醒管理员"))
                    if result.get("detail"):
                        st.caption(result["detail"])
                    st.toast("已提醒管理员", icon="🔔")
                except ApiError as exc:
                    st.error(exc.message)
        with c2:
            st.caption("小游戏不是主线；等得无聊可以玩一局，玩完再点左边催一下。")


def _forgot_tab(api: ApiClient) -> None:
    """忘记密码：先提交申请，管理员批准拿到短码后再重置。"""
    st.markdown("**第一步：提交申请**")
    with st.form("forgot_request_form"):
        username = st.text_input("账号", key="forgot_username", autocomplete="username")
        note = st.text_input("补充说明（可选）", placeholder="例如：原手机号停用")
        submitted = st.form_submit_button("提交找回申请", use_container_width=True)
    if submitted:
        try:
            result = api.forgot_password(username.strip(), note.strip())
            st.success(result.get("message", "申请已提交"))
            st.info(result.get("detail", "等待管理员批准。"))
        except ApiError as exc:
            st.error(exc.message)

    st.divider()
    st.markdown("**第二步：管理员批准后，用短码设置新密码**")
    with st.form("reset_password_form"):
        username2 = st.text_input("账号", key="reset_username", autocomplete="username")
        code = st.text_input("一次性短码", max_chars=32, autocomplete="one-time-code")
        new_password = st.text_input("新密码", type="password", autocomplete="new-password")
        st.caption("新密码 6—20 位；纯数字、纯字母、混合都可以。")
        confirm = st.text_input("确认新密码", type="password", autocomplete="new-password")
        submitted2 = st.form_submit_button("重置密码", type="primary", use_container_width=True)
    if submitted2:
        pwd_error = password_error(new_password)
        if pwd_error:
            st.error(pwd_error)
            return
        if new_password != confirm:
            st.error("两次输入的新密码不一致")
            return
        try:
            result = api.reset_password(username2.strip(), code.strip(), new_password)
            st.success(result.get("message", "密码已重置"))
            st.info(result.get("detail", "请用新密码登录。"))
        except ApiError as exc:
            st.error(exc.message)


def _admin_tab(api: ApiClient) -> None:
    """管理员入口：密码 + 密保问答；密码固定不可改。"""
    if st.session_state.get("admin_challenge"):
        _render_admin_challenge(api)
        return
    st.caption("管理员账号固定为 Shenk；密码正确后还需回答密保问题。密码不可修改。")
    with st.form("admin_login_form"):
        username = st.text_input("管理员账号", value="Shenk", max_chars=20)
        password = st.text_input("管理员密码", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("进入管理员验证", type="primary", use_container_width=True)
    if submitted:
        try:
            result = api.login(username.strip(), password)
            if not result.get("requires_2fa"):
                st.error("该账号不是管理员，请使用用户端登录")
                return
            _handle_login_result(result, api)
        except ApiError as exc:
            st.error(exc.message)


def render_auth(api: ApiClient) -> None:
    """认证页整体布局：左侧表单，右侧鱼缸/产品说明。"""
    ui.brand_header()
    left, right = st.columns([1.05, 1.0], gap="large")
    with left:
        st.markdown(
            """
            <div class="czx-hero">
              <div class="czx-kicker" style="color:#99f6e4">LOCAL-FIRST JOB SEARCH</div>
              <h2>把职位页面，变成你的投递判断</h2>
              <p>你自己打开招聘网站，AI 负责采集后的结构化、初筛和两轮匹配分析。数据只留在这台电脑。</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.write("")
        # 管理员二次验证时优先显示，避免用户又看到登录表单。
        if st.session_state.get("admin_challenge"):
            _render_admin_challenge(api)
            return
        tabs = st.tabs(["登录", "注册", "忘记密码", "管理员"])
        with tabs[0]:
            _login_tab(api)
        with tabs[1]:
            _register_tab(api)
        with tabs[2]:
            _forgot_tab(api)
        with tabs[3]:
            _admin_tab(api)
        st.caption("安全提示：本机部署默认只监听 127.0.0.1；不要把 8501/8000 端口直接暴露到公网。")
    with right:
        _render_pending_box(api)
        with st.container(border=True):
            st.markdown("### 审核流程")
            st.markdown(
                """
                1. 注册后账号是 **pending**，不能直接登录。
                2. 管理员在控制台“注册审核”通过。
                3. 通过后用同一账号密码登录。
                4. 登录后先填画像，再采集职位。
                """
            )
            st.caption("等审批时，下面小游戏可以打发时间；它不是主流程。")

        # 小游戏收进折叠区：不抢登录/注册的主视觉；等审批时默认展开。
        with st.expander("等审批？玩一局 Deepseek 大肥鱼", expanded=bool(st.session_state.get("pending_username"))):
            ui.render_fish_games(height=620)
            st.caption("游戏结束/接鱼得分后，记得点上方“催管理员审批”；游戏本身不会自动发消息。")

        with st.expander("这个网站到底怎么拿到职位？", expanded=False):
            st.markdown(
                """
                1. 你登录后，在设置页生成**采集令牌**。
                2. 下载扩展 ZIP，在 Edge 加载；你手动打开招聘页面。
                3. 点扩展“采集当前页”，只读取你眼前这一页的公开可见信息。
                4. 也可以不走扩展：在职位页粘贴 JD 文本，手动导入。

                **不做**：自动登录、自动翻页、破解验证码、绕过平台风控、批量抓取。
                平台条款仍可能限制自动化采集，使用者需自行判断并承担风险。
                """
            )
