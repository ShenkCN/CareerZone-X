# ============================================================
# Streamlit 会话状态：只存“当前浏览器标签页”的临时状态。
# 注意：Streamlit 刷新会丢 session_state，所以刷新后需要重新登录；
# 这是本机自用版的取舍，避免把 token 写进 URL/Cookie 带来额外泄露面。
# ============================================================
from __future__ import annotations

from typing import Any, Optional

import streamlit as st

DEFAULT_KEYS = {
    "token": None,
    "user": None,
    "page": "home",
    "selected_job_id": None,
    "jobs_result": None,
    "job_filters": {},
    "exclude_ids": [],
    "jobs_offset": 0,
    "admin_challenge": None,
    "admin_question": "",
    "avatar_bytes": None,
    "flash": None,
    "manual_result": None,
    "cookie_token": None,
    "clear_auth_cookie": False,
}

# 跨账号必须清掉的键/前缀：简历草稿、职位页、下载缓存都属于上一个用户。
_USER_SCOPED_KEYS = {
    "selected_job_id",
    "jobs_result",
    "job_filters",
    "exclude_ids",
    "jobs_offset",
    "jobs_initialized",
    "profile_bundle",
    "profile_prefill",
    "avatar_bytes",
    "similar_jobs",
    "detail_analysis",
    "admin_reset_code",
    "admin_export",
    "new_import_token",
    "bookmarks_offset",
    "history_offset",
    "manual_result",
}
_USER_SCOPED_PREFIXES = ("resume_file_", "sys_file_", "resume_save_", "sys_save_")


def _clear_user_scoped() -> None:
    """登录/退出时清理上一个账号的内容，防止串号看到别人的简历草稿。"""
    for key in list(st.session_state.keys()):
        if key in _USER_SCOPED_KEYS or key.startswith(_USER_SCOPED_PREFIXES):
            del st.session_state[key]


def init_state() -> None:
    """只补缺失的键，不覆盖当前页面已保存的数据。"""
    for key, value in DEFAULT_KEYS.items():
        if key not in st.session_state:
            st.session_state[key] = value


def set_login(token: str, user: dict[str, Any]) -> None:
    """登录成功后统一清理旧状态，防止上一个账号的职位结果串到新账号。"""
    _clear_user_scoped()
    st.session_state.token = token
    st.session_state.user = user
    st.session_state.page = "admin" if user.get("role") == "admin" else "home"
    st.session_state.selected_job_id = None
    st.session_state.jobs_result = None
    st.session_state.exclude_ids = []
    st.session_state.jobs_offset = 0
    st.session_state.admin_challenge = None
    st.session_state.admin_question = ""
    st.session_state.avatar_bytes = None
    st.session_state.flash = None
    # 让浏览器写入 Cookie，刷新后才能恢复登录。
    st.session_state.cookie_token = token
    st.session_state.clear_auth_cookie = False


def clear_login() -> None:
    """退出登录：清掉 token/用户/分析结果，保留页面偏好。"""
    _clear_user_scoped()
    for key in ("token", "user", "admin_challenge", "admin_question", "avatar_bytes"):
        st.session_state[key] = None if key != "admin_question" else ""
    st.session_state.jobs_result = None
    st.session_state.exclude_ids = []
    st.session_state.page = "home"
    st.session_state.cookie_token = None
    # 页面渲染后由 0 高度组件清除 Cookie。
    st.session_state.clear_auth_cookie = True


def set_page(page: str) -> None:
    """切页并清理上一次的详情选择，避免返回时打开错职位。"""
    st.session_state.page = page
    if page != "job_detail":
        st.session_state.selected_job_id = None


def set_flash(message: str, kind: str = "success") -> None:
    """跨 rerun 的一次性提示；渲染后清除，避免每次刷新都弹。"""
    st.session_state.flash = {"message": message, "kind": kind}


def consume_flash() -> Optional[dict[str, str]]:
    """取出并清空 flash；页面顶部统一渲染。"""
    value = st.session_state.get("flash")
    st.session_state.flash = None
    return value


def user_is_admin() -> bool:
    """统一的角色判断，避免页面各写各的。"""
    user = st.session_state.get("user") or {}
    return user.get("role") == "admin"
