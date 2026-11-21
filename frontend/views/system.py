# ============================================================
# 系统后端页（仅管理员）：磁盘/数据库/文件/审计/职位原始数据。
# 这是“运维视角”，不是用户功能；用来排查数据与存储问题。
# ============================================================
from __future__ import annotations

import streamlit as st

from frontend import ui
from frontend.api_client import ApiClient, ApiError


def _storage(api: ApiClient) -> None:
    """磁盘/数据库/向量索引概览。"""
    try:
        info = api.admin_system()
    except ApiError as exc:
        st.error(exc.message)
        return
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("项目占用", ui.human_size(info.get("project_size_bytes")))
    c2.metric("storage 占用", ui.human_size(info.get("storage_size_bytes")))
    c3.metric("数据库占用", ui.human_size(info.get("database_size_bytes")))
    c4.metric("向量条数", info.get("vector_count", -1))
    st.caption(
        f"数据库：{info.get('db_backend')}｜用户 {info.get('user_count')}｜职位 {info.get('job_count')}｜"
        f"上传文件 {info.get('upload_count')}｜简历 {info.get('resume_count')}｜"
        f"向量：{info.get('vector_backend') or '未初始化'}"
    )
    if info.get("vector_count", 0) == -1:
        st.warning("向量索引未就绪；点“重建向量索引”会重试。若本机拦截 torch/regex DLL，会自动使用 hash-384。")
    c5, c6 = st.columns(2)
    with c5:
        if st.button("重建向量索引", type="primary"):
            try:
                result = api.admin_reindex()
                st.success(result.get("message", "已开始"))
                st.caption(result.get("detail", ""))
            except ApiError as exc:
                st.error(exc.message)
    with c6:
        st.caption("备份建议：停止写入后复制 storage/ + MySQL dump；不要只备份代码。")
    with st.expander("磁盘占用说明", expanded=False):
        st.markdown(
            """
            - **项目占用**：代码、依赖、storage 的总和（排除 .venv 缓存）。
            - **storage**：上传简历、头像、Chroma 索引、导出包、日志。
            - **数据库**：MySQL 的 data_length + index_length。
            - **向量条数**：Chroma 里已索引的职位数；首次重建会下载 bge 模型。
            """
        )


def _files(api: ApiClient) -> None:
    """上传文件清单 + 简历回档。"""
    try:
        info = api.admin_system()
    except ApiError as exc:
        st.error(exc.message)
        return
    files = info.get("files") or []
    if not files:
        st.info("还没有上传文件。")
        return
    rows = [
        {
            "类型": item.get("kind"),
            "用户ID": item.get("user_id"),
            "文件名": item.get("name"),
            "大小": ui.human_size(item.get("size_bytes")),
            "版本": item.get("version"),
            "当前": "是" if item.get("is_active") else "否",
            "时间": ui.format_datetime(item.get("created_at")),
        }
        for item in files
    ]
    ui.html_table(rows)
    st.markdown("#### 简历回档 / 下载")
    for item in files:
        if item.get("kind") != "resume" or not item.get("id"):
            continue
        with st.container(border=True):
            c1, c2, c3 = st.columns([2.4, 1.2, 1.2])
            with c1:
                st.markdown(f"**{item.get('name')}**")
                st.caption(f"用户 {item.get('user_id')}｜v{item.get('version')}｜{ui.human_size(item.get('size_bytes'))}")
            with c2:
                if st.button("准备下载", key=f"sys_dl_{item['id']}", use_container_width=True):
                    try:
                        content, filename = api.download_resume(int(item["id"]))
                        st.session_state[f"sys_file_{item['id']}"] = (content, filename)
                    except ApiError as exc:
                        st.error(exc.message)
                if st.session_state.get(f"sys_file_{item['id']}"):
                    content, filename = st.session_state[f"sys_file_{item['id']}"]
                    st.download_button(
                        "保存文件",
                        data=content,
                        file_name=filename,
                        key=f"sys_save_{item['id']}",
                        use_container_width=True,
                    )
            with c3:
                if not item.get("is_active"):
                    if st.button("回档到此版本", key=f"sys_activate_{item['id']}", use_container_width=True):
                        try:
                            api.admin_rollback_resume(int(item["id"]))
                            st.success("已回档")
                            st.rerun()
                        except ApiError as exc:
                            st.error(exc.message)


def _jobs(api: ApiClient) -> None:
    """职位原始清单；检查采集质量。"""
    try:
        data = api.admin_jobs(offset=0, limit=100)
    except ApiError as exc:
        st.error(exc.message)
        return
    items = data.get("items") or []
    st.caption(f"共 {data.get('total', 0)} 条，显示最近 100 条")
    if not items:
        st.info("还没有职位。去用户端采集或手动导入。")
        return
    rows = [
        {
            "ID": item.get("id"),
            "来源": item.get("source_label"),
            "职位": item.get("title"),
            "公司": item.get("company"),
            "城市": item.get("city"),
            "薪资": item.get("salary_text"),
            "浏览": item.get("view_count"),
            "采集时间": ui.format_datetime(item.get("captured_at")),
        }
        for item in items
    ]
    ui.html_table(rows)
    with st.expander("原始链接", expanded=False):
        for item in items[:30]:
            st.markdown(f"- [{item.get('title')}]({ui.safe_url(item.get('url')) or '#'})")


def _audit(api: ApiClient) -> None:
    """最近审计事件。"""
    try:
        data = api.admin_audit(limit=100)
    except ApiError as exc:
        st.error(exc.message)
        return
    items = data.get("items") or []
    if not items:
        st.info("还没有审计记录。")
        return
    rows = [
        {
            "时间": ui.format_datetime(item.get("created_at")),
            "动作": item.get("action"),
            "对象": item.get("target"),
            "操作人ID": item.get("actor_id"),
            "IP": item.get("ip"),
        }
        for item in items
    ]
    ui.html_table(rows)


def render_system(api: ApiClient) -> None:
    """系统后端页主入口。"""
    ui.page_header("系统后端", "只给管理员看：项目占用、文件、数据库、向量索引、审计。")
    tabs = st.tabs(["存储与索引", "上传文件", "职位数据", "审计日志"])
    with tabs[0]:
        _storage(api)
    with tabs[1]:
        _files(api)
    with tabs[2]:
        _jobs(api)
    with tabs[3]:
        _audit(api)
