# ============================================================
# Streamlit 入口：会话恢复、角色分流、侧边栏导航。
# 认证页和业务页分开渲染；管理员永远不会看到用户职位板。
# ============================================================
from __future__ import annotations

import streamlit as st

from frontend import ui
from frontend.api_client import ApiClient, ApiError
from frontend.state import (
    clear_login,
    consume_flash,
    init_state,
    set_login,
    set_page,
    user_is_admin,
)
from frontend.views import admin as admin_view
from frontend.views import auth as auth_view
from frontend.views import guide as guide_view
from frontend.views import home as home_view
from frontend.views import job_detail as detail_view
from frontend.views import jobs as jobs_view
from frontend.views import library as library_view
from frontend.views import onboarding as onboarding_view
from frontend.views import settings as settings_view
from frontend.views import system as system_view

st.set_page_config(
    page_title="CareerZone X",
    page_icon="🐟",
    layout="wide",
    # auto：桌面展开、手机自动收起，避免手机首屏被侧边栏挡住。
    initial_sidebar_state="auto",
)

init_state()
ui.inject_css()


def _sidebar_brand(user: dict) -> None:
    """侧边栏品牌 + 当前用户；管理员和用户都显示，但内容不同。"""
    st.sidebar.markdown(
        '<div style="display:flex;align-items:center;gap:9px;margin:4px 0 14px 0">'
        '<div style="width:38px;height:38px;border-radius:12px;background:#ccfbf1;color:#0b3b3a;'
        'display:flex;align-items:center;justify-content:center;font-size:22px">🐟</div>'
        '<div><div style="font-size:19px;font-weight:800;color:#fff">CareerZone X</div>'
        '<div style="font-size:11px;color:#99f6e4">LOCAL JOB WORKBENCH</div></div></div>',
        unsafe_allow_html=True,
    )
    avatar = st.session_state.get("avatar_bytes")
    if avatar:
        st.sidebar.image(avatar, width=64)
    else:
        # 没上传时也显示默认头像，避免左下角空着、用户以为“头像功能没用”。
        st.sidebar.markdown('<div class="czx-sidebar-avatar">🐟</div>', unsafe_allow_html=True)
        st.sidebar.caption("默认头像 · 可在设置上传")
    st.sidebar.markdown(
        f"**{user.get('display_name') or user.get('username')}**  \n"
        f"<span style='font-size:11px;color:#99f6e4'>{'管理员' if user.get('role') == 'admin' else '个人用户'} · "
        f"{'固定密码' if user.get('role') == 'admin' else '本机数据'}</span>",
        unsafe_allow_html=True,
    )
    st.sidebar.divider()


def _load_avatar(api: ApiClient) -> None:
    """只在第一次渲染时取头像，避免每个 rerun 都请求二进制。"""
    user = st.session_state.get("user") or {}
    if st.session_state.get("avatar_bytes") is None and user.get("avatar_url"):
        try:
            st.session_state.avatar_bytes = api.avatar_bytes()
        except ApiError:
            st.session_state.avatar_bytes = None


def _logout(api: ApiClient) -> None:
    """先通知后端撤销会话，再清本地状态；后端失败也允许本地退出。"""
    try:
        api.logout()
    except ApiError:
        pass
    clear_login()
    st.rerun()


def _render_user(user: dict, api: ApiClient) -> None:
    """用户端导航 + 页面路由。"""
    pages = {
        "首页": "home",
        "职位板": "jobs",
        "收藏夹": "bookmarks",
        "浏览历史": "history",
        "设置": "settings",
        "使用说明": "guide",
    }
    current = st.session_state.get("page") or "home"
    if current == "job_detail":
        index = 1
    elif current == "onboarding":
        index = 0
    else:
        values = list(pages.values())
        index = values.index(current) if current in values else 0
    selected = st.sidebar.radio("导航", list(pages.keys()), index=index, label_visibility="collapsed")
    if selected != list(pages.keys())[index]:
        set_page(pages[selected])
        st.rerun()

    st.sidebar.caption("不是自动爬虫：你打开页面，扩展只采集当前页。")
    if st.sidebar.button("退出登录", use_container_width=True):
        _logout(api)

    page = st.session_state.get("page") or "home"
    if page == "home":
        home_view.render_home(api)
    elif page == "onboarding":
        onboarding_view.render_onboarding(api)
    elif page == "jobs":
        jobs_view.render_jobs(api)
    elif page == "job_detail":
        detail_view.render_job_detail(api)
    elif page == "bookmarks":
        library_view.render_bookmarks(api)
    elif page == "history":
        library_view.render_history(api)
    elif page == "settings":
        settings_view.render_settings(api)
    elif page == "guide":
        guide_view.render_guide()
    else:
        home_view.render_home(api)


def _render_admin(user: dict, api: ApiClient) -> None:
    """管理员端：只有控制台和系统后端，不渲染用户职位板。"""
    pages = {"控制台": "admin", "系统后端": "system"}
    current = st.session_state.get("page") or "admin"
    values = list(pages.values())
    index = values.index(current) if current in values else 0
    selected = st.sidebar.radio("管理员导航", list(pages.keys()), index=index, label_visibility="collapsed")
    if selected != list(pages.keys())[index]:
        set_page(pages[selected])
        st.rerun()
    st.sidebar.caption("管理员密码固定；密保问题可在控制台修改。")
    if st.sidebar.button("退出管理员", use_container_width=True):
        _logout(api)

    page = st.session_state.get("page") or "admin"
    if page == "system":
        system_view.render_system(api)
    else:
        admin_view.render_admin(api)


# ============================================================
# 启动流程
# ============================================================
# 退出登录时先清浏览器 Cookie，避免刷新后又用旧 Cookie 自动登录。
just_cleared_cookie = False
if st.session_state.get("clear_auth_cookie"):
    ui.clear_auth_cookie()
    st.session_state.clear_auth_cookie = False
    just_cleared_cookie = True

# 只有“没有内存 token”且不是刚退出时才读 Cookie；
# 否则当前请求的 Cookie 还在，会立刻把用户又登录回去。
if not st.session_state.get("token") and not just_cleared_cookie:
    cookie_token = st.context.cookies.get("czx_session") if st.context.cookies else None
    if cookie_token:
        try:
            cookie_user = ApiClient(cookie_token).me()
        except ApiError:
            # Cookie 过期/被撤销：删掉它，回到登录页，不弹出吓人的错误。
            ui.clear_auth_cookie()
        else:
            set_login(cookie_token, cookie_user)

# 登录成功后由 0/1 高度组件写入 Cookie；放在业务页渲染前，刷新即可恢复。
if st.session_state.get("cookie_token"):
    ui.set_auth_cookie(st.session_state.cookie_token)
    st.session_state.cookie_token = None

api = ApiClient(st.session_state.get("token"))

if not st.session_state.get("token"):
    auth_view.render_auth(api)
    st.stop()

# 用 /me 验证 token 是否仍有效；比只看 session_state 更安全。
try:
    current_user = api.me()
except ApiError as exc:
    if exc.status_code in (401, 403):
        clear_login()
        st.warning("登录已失效，请重新登录。")
        st.rerun()
    st.error(exc.message)
    if st.button("重试连接"):
        st.rerun()
    st.stop()

st.session_state.user = current_user
_load_avatar(api)

_sid = _sidebar_brand(current_user)
flash = consume_flash()
if flash:
    if flash.get("kind") == "error":
        st.error(flash.get("message"))
    elif flash.get("kind") == "warning":
        st.warning(flash.get("message"))
    else:
        st.success(flash.get("message"))

if user_is_admin():
    _render_admin(current_user, api)
else:
    _render_user(current_user, api)
