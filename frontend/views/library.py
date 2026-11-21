# ============================================================
# 收藏夹 / 浏览历史：复用职位卡片，保持操作一致。
# ============================================================
from __future__ import annotations

import streamlit as st

from frontend import ui
from frontend.api_client import ApiClient, ApiError


def _handle_action(api: ApiClient, action: str | None, job: dict) -> None:
    """卡片动作统一处理，避免两个页面各写一遍。"""
    if action == "detail":
        st.session_state.selected_job_id = job["id"]
        st.session_state.page = "job_detail"
        st.session_state["scroll_top"] = True
        st.rerun()
    if action == "bookmark":
        try:
            state = api.bookmark(job["id"])
            job["bookmarked"] = bool(state.get("bookmarked"))
            st.toast(state.get("message", "已更新"))
        except ApiError as exc:
            st.error(exc.message)
        st.rerun()


def render_bookmarks(api: ApiClient) -> None:
    """收藏列表，分页 20。"""
    ui.page_header("收藏夹", "先把值得回看的岗位收起来，再集中投递。")
    offset_key = "bookmarks_offset"
    if offset_key not in st.session_state:
        st.session_state[offset_key] = 0
    offset = int(st.session_state[offset_key])
    try:
        result = api.bookmarks(offset=offset, limit=20)
    except ApiError as exc:
        st.error(exc.message)
        return
    items = result.get("items") or []
    total = int(result.get("total") or 0)
    st.caption(f"共 {total} 条｜当前第 {offset + 1 if items else 0}—{offset + len(items)} 条")
    if not items:
        st.info("还没有收藏。去职位板点“收藏”，这里就会出现。")
        if st.button("去职位板"):
            st.session_state.page = "jobs"
            st.rerun()
        return
    for job in items:
        action = ui.render_job_card(job, key_prefix="bookmarks")
        _handle_action(api, action, job)
    c1, c2, _ = st.columns([1, 1, 3])
    with c1:
        if st.button("上一页", disabled=offset <= 0, use_container_width=True):
            st.session_state[offset_key] = max(0, offset - 20)
            st.rerun()
    with c2:
        if st.button("下一页", disabled=offset + 20 >= total, use_container_width=True):
            st.session_state[offset_key] = offset + 20
            st.rerun()


def _render_cleared(api: ApiClient) -> None:
    """已清空职位：只影响当前用户，可申请管理员恢复。"""
    st.divider()
    st.markdown("### 已清空职位")
    try:
        data = api.cleared_jobs()
    except ApiError as exc:
        st.warning(exc.message)
        return
    items = data.get("items") or []
    if not items:
        st.caption("没有已清空的职位。清空后 A 看不到；没有自己副本的人也会看不到；自己采过的保留。")
        return
    st.caption(f"共 {len(items)} 条。申请恢复后由管理员审批；也可以先申请部分。")
    labels = {
        int(item["job"]["id"]): f"{item['job']['title']}｜{item['job']['company']}"
        for item in items
        if item.get("job")
    }
    selected = st.multiselect(
        "选择要申请恢复的职位",
        options=list(labels.keys()),
        format_func=lambda job_id: labels.get(job_id, f"职位 #{job_id}"),
        key="restore_selected_jobs",
    )
    note = st.text_input("申请说明（可选）", key="restore_note", max_chars=200)
    c1, c2 = st.columns(2)
    with c1:
        if st.button("申请恢复选中", disabled=not selected, use_container_width=True):
            try:
                result = api.create_restore_request(selected, note)
                st.success(result.get("message", "已提交申请"))
                st.rerun()
            except ApiError as exc:
                st.error(exc.message)
    with c2:
        if st.button("申请恢复全部", use_container_width=True):
            try:
                result = api.create_restore_request(list(labels.keys()), note)
                st.success(result.get("message", "已提交申请"))
                st.rerun()
            except ApiError as exc:
                st.error(exc.message)


def render_history(api: ApiClient) -> None:
    """浏览历史，按最近打开时间倒序；同一岗位只显示一次。"""
    ui.page_header("浏览历史", "回看你看过哪些岗位，避免重复刷同一页。")
    offset_key = "history_offset"
    if offset_key not in st.session_state:
        st.session_state[offset_key] = 0
    offset = int(st.session_state[offset_key])
    try:
        result = api.history(offset=offset, limit=20)
    except ApiError as exc:
        st.error(exc.message)
        return
    items = result.get("items") or []
    total = int(result.get("total") or 0)
    st.caption(f"共 {total} 条｜当前第 {offset + 1 if items else 0}—{offset + len(items)} 条")
    if not items:
        st.info("还没有浏览记录。打开职位详情后，这里会自动记录。")
        _render_cleared(api)
        return
    for item in items:
        job = item.get("job") or {}
        st.caption("上次查看：" + ui.format_datetime(item.get("viewed_at")))
        action = ui.render_job_card(job, key_prefix="history")
        _handle_action(api, action, job)
    c1, c2, _ = st.columns([1, 1, 3])
    with c1:
        if st.button("上一页", disabled=offset <= 0, use_container_width=True, key="history_prev"):
            st.session_state[offset_key] = max(0, offset - 20)
            st.rerun()
    with c2:
        if st.button("下一页", disabled=offset + 20 >= total, use_container_width=True, key="history_next"):
            st.session_state[offset_key] = offset + 20
            st.rerun()
    _render_cleared(api)
