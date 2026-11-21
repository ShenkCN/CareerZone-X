# ============================================================
# 管理员控制台：审批、密码重置、反馈、数据总览、导出、密保。
# 管理员不经过用户首页；app.py 在登录后按 role 分流到这里。
# ============================================================
from __future__ import annotations

from typing import Any

import plotly.express as px
import streamlit as st

from frontend import ui
from frontend.api_client import ApiClient, ApiError


def _charts(dashboard: dict[str, Any]) -> None:
    """四张图：来源分布、Token 趋势、浏览来源、匹配分分布。"""
    charts = dashboard.get("charts") or {}
    c1, c2 = st.columns(2)
    with c1:
        sources = charts.get("sources") or []
        if sources:
            fig = px.pie(
                sources, names="label", values="count", hole=0.45,
                title="职位来源分布",
                color_discrete_sequence=["#0f766e", "#e76f51", "#f59e0b", "#3b82f6", "#8b5cf6", "#64748b"],
            )
            fig.update_layout(margin=dict(l=10, r=10, t=40, b=10), height=300, showlegend=True)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("暂无职位来源数据")
    with c2:
        daily = charts.get("daily") or []
        if daily:
            fig = px.line(daily, x="date", y="tokens", markers=True, title="近 14 天 Token 消耗")
            fig.update_layout(margin=dict(l=10, r=10, t=40, b=10), height=300)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("暂无 Token 数据")
    c3, c4 = st.columns(2)
    with c3:
        views = charts.get("views") or []
        if views:
            fig = px.bar(views, x="label", y="count", title="浏览来源（扇形数据的柱状呈现）", color="label")
            fig.update_layout(margin=dict(l=10, r=10, t=40, b=10), height=300, showlegend=False)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("暂无浏览数据")
    with c4:
        hist = charts.get("fit_histogram") or []
        if hist:
            fig = px.bar(hist, x="bucket", y="count", title="初筛匹配分分布")
            fig.update_layout(margin=dict(l=10, r=10, t=40, b=10), height=300)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("暂无初筛数据")


def _overview(api: ApiClient) -> None:
    """总览：数字卡片 + 图表 + 热门职位 + 最新反馈。"""
    try:
        dashboard = api.admin_dashboard()
    except ApiError as exc:
        st.error(exc.message)
        return
    counts = dashboard.get("counts") or {}
    usage = dashboard.get("usage") or {}
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("用户", counts.get("users", 0))
    c2.metric("待审核", counts.get("pending_users", 0))
    c3.metric("职位", counts.get("jobs", 0))
    c4.metric("浏览", counts.get("views", 0))
    c5.metric("总 Token", f"{int(usage.get('total_tokens', 0)):,}")
    c6.metric("API 调用", usage.get("total_calls", 0))
    st.caption(
        f"今日 Token：{int(usage.get('today_tokens', 0)):,}｜今日调用：{usage.get('today_calls', 0)}｜"
        f"成功率：{float(usage.get('success_rate', 0)) * 100:.1f}%｜平均延迟：{usage.get('avg_latency_ms', 0)}ms｜"
        f"估算费用：¥{float(usage.get('total_cost_estimate', 0)):.4f}（单价为 0 时仅统计 Token）"
    )
    if counts.get("pending_nudges"):
        st.warning(f"有 {counts['pending_nudges']} 条注册审批催促，去“注册审核”处理。")
    _charts(dashboard)

    c7, c8 = st.columns(2)
    with c7:
        st.markdown("#### 浏览最多的职位")
        for job in dashboard.get("top_jobs") or []:
            st.markdown(f"- **{job.get('title')}** · {job.get('company')}（{job.get('source')}）浏览 {job.get('view_count')}")
    with c8:
        st.markdown("#### 最新反馈")
        for item in dashboard.get("latest_feedback") or []:
            st.markdown(f"- [{item.get('category')}] {item.get('content')}（{item.get('status')}）")
    st.caption("数据生成时间：" + ui.format_datetime(dashboard.get("generated_at")))


def _registrations(api: ApiClient) -> None:
    """待审核用户：批准或拒绝，备注可选。"""
    try:
        users = api.admin_users("pending")
    except ApiError as exc:
        st.error(exc.message)
        return
    if not users:
        st.success("没有待审核的注册申请。")
        return
    for user in users:
        with st.expander(f"{user.get('username')}｜{user.get('display_name')}｜{ui.format_datetime(user.get('created_at'))}", expanded=True):
            note = st.text_input("审核备注（可选）", key=f"review_note_{user['id']}")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("通过", type="primary", key=f"approve_{user['id']}", use_container_width=True):
                    try:
                        api.approve_user(user["id"], note)
                        st.success("已通过")
                        st.rerun()
                    except ApiError as exc:
                        st.error(exc.message)
            with c2:
                if st.button("拒绝", key=f"reject_{user['id']}", use_container_width=True):
                    try:
                        api.reject_user(user["id"], note)
                        st.warning("已拒绝")
                        st.rerun()
                    except ApiError as exc:
                        st.error(exc.message)


def _account_management(api: ApiClient) -> None:
    """账号管理：用户信息、采集/收藏/活跃度、批次和恢复。"""
    try:
        users = api.admin_users("")
    except ApiError as exc:
        st.error(exc.message)
        return
    if not users:
        st.info("还没有用户。")
        return
    rows = []
    for user in users:
        rows.append(
            {
                "账号": user.get("username"),
                "显示名": user.get("display_name") or "",
                "角色": "管理员" if user.get("role") == "admin" else "用户",
                "状态": user.get("status"),
                "注册": ui.format_datetime(user.get("created_at")),
                "最后登录": ui.format_datetime(user.get("last_login_at")) if user.get("last_login_at") else "从未",
                "采集": user.get("job_captures", 0),
                "已清空": user.get("job_cleared", 0),
                "收藏": user.get("bookmarks", 0),
                "浏览": user.get("views", 0),
                "活跃度": user.get("activity", 0),
            }
        )
    ui.html_table(rows, max_height=380)

    st.divider()
    st.markdown("### 用户采集与恢复")
    for user in users:
        if user.get("role") == "admin":
            continue
        with st.expander(
            f"{user.get('username')}｜采集 {user.get('job_captures', 0)}｜收藏 {user.get('bookmarks', 0)}｜浏览 {user.get('views', 0)}",
            expanded=False,
        ):
            try:
                data = api.admin_user_captures(user["id"])
            except ApiError as exc:
                st.error(exc.message)
                continue
            batches = data.get("batches") or []
            captures = data.get("captures") or []
            st.caption(f"最后登录：{ui.format_datetime(user.get('last_login_at')) if user.get('last_login_at') else '从未'}")
            if batches:
                batch_rows = [
                    {
                        "批次": item.get("id"),
                        "来源": item.get("source"),
                        "模式": item.get("mode"),
                        "新增": item.get("inserted"),
                        "更新": item.get("updated"),
                        "跳过": item.get("skipped"),
                        "条数": item.get("count"),
                        "时间": ui.format_datetime(item.get("created_at")),
                    }
                    for item in batches
                ]
                ui.html_table(batch_rows, max_height=220)
            else:
                st.caption("暂无采集批次。")
            if not captures:
                continue
            labels = {
                int(item["job_id"]): f"[{item['job_id']}] {item['title']}｜{item['company']}"
                for item in captures
            }
            selected = st.multiselect(
                "选择要恢复的职位",
                options=list(labels.keys()),
                format_func=lambda job_id, mapping=labels: mapping.get(job_id, f"职位 #{job_id}"),
                key=f"admin_restore_select_{user['id']}",
            )
            c1, c2 = st.columns(2)
            with c1:
                if st.button("恢复选中", disabled=not selected, key=f"admin_restore_sel_{user['id']}", use_container_width=True):
                    try:
                        result = api.admin_restore_jobs(user["id"], selected)
                        st.success(result.get("message", "已恢复"))
                        st.rerun()
                    except ApiError as exc:
                        st.error(exc.message)
            with c2:
                if st.button("恢复该用户全部采集", key=f"admin_restore_all_{user['id']}", use_container_width=True):
                    try:
                        result = api.admin_restore_jobs(user["id"], list(labels.keys()))
                        st.success(result.get("message", "已恢复"))
                        st.rerun()
                    except ApiError as exc:
                        st.error(exc.message)

    st.markdown("### 职位恢复申请")
    try:
        requests_data = api.admin_restore_requests("pending")
    except ApiError as exc:
        st.error(exc.message)
        return
    requests = requests_data.get("items") or []
    if not requests:
        st.success("没有待处理的恢复申请。")
        return
    for item in requests:
        with st.container(border=True):
            st.markdown(f"**{item.get('username')}** 申请恢复 {len(item.get('job_ids') or [])} 条")
            st.caption(f"说明：{item.get('note') or '无'}｜提交：{ui.format_datetime(item.get('created_at'))}")
            for job in item.get("jobs") or []:
                st.markdown(f"- {job.get('title')}｜{job.get('company')}")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("批准恢复", type="primary", key=f"restore_ok_{item['id']}", use_container_width=True):
                    try:
                        result = api.approve_restore_request(item["id"])
                        st.success(result.get("message", "已恢复"))
                        st.rerun()
                    except ApiError as exc:
                        st.error(exc.message)
            with c2:
                if st.button("拒绝", key=f"restore_no_{item['id']}", use_container_width=True):
                    try:
                        api.reject_restore_request(item["id"], "管理员拒绝")
                        st.warning("已拒绝")
                        st.rerun()
                    except ApiError as exc:
                        st.error(exc.message)


def _resets(api: ApiClient) -> None:
    """忘记密码申请：批准后显示一次性短码。"""
    try:
        items = api.admin_reset_requests("pending")
    except ApiError as exc:
        st.error(exc.message)
        return
    if not items:
        st.success("没有待处理的密码重置申请。")
        return
    code_data = st.session_state.get("admin_reset_code")
    if code_data:
        st.success("短码已生成（只显示这一次）")
        st.code(code_data.get("code", ""), language="text")
        st.caption(f"申请 #{code_data.get('id')}；30 分钟内有效。让用户到登录页“忘记密码”第二步输入。")
    for item in items:
        with st.container(border=True):
            st.markdown(f"**{item.get('username')}｜{item.get('display_name')}**")
            st.caption(f"说明：{item.get('note') or '无'}｜提交：{ui.format_datetime(item.get('created_at'))}")
            c1, c2, c3 = st.columns([1, 1, 3])
            with c1:
                if st.button("批准并生成短码", type="primary", key=f"reset_ok_{item['id']}", use_container_width=True):
                    try:
                        result = api.approve_reset(item["id"])
                        st.session_state["admin_reset_code"] = {"id": item["id"], "code": result.get("code")}
                        st.rerun()
                    except ApiError as exc:
                        st.error(exc.message)
            with c2:
                if st.button("拒绝", key=f"reset_no_{item['id']}", use_container_width=True):
                    try:
                        api.reject_reset(item["id"], "管理员拒绝")
                        st.warning("已拒绝")
                        st.rerun()
                    except ApiError as exc:
                        st.error(exc.message)


def _feedback(api: ApiClient) -> None:
    """反馈处理：管理员评级 + 回复。"""
    try:
        data = api.admin_feedback()
    except ApiError as exc:
        st.error(exc.message)
        return
    items = data.get("items") or []
    if not items:
        st.success("还没有用户反馈。")
        return
    for item in items:
        with st.expander(f"{item.get('username')}｜{item.get('category')}｜{item.get('status')}", expanded=False):
            st.write(item.get("content"))
            st.caption(f"评分 {item.get('rating')}/5｜提交 {ui.format_datetime(item.get('created_at'))}")
            if item.get("admin_reply"):
                st.info(f"已回复（{item.get('level')}）：{item.get('admin_reply')}")
            else:
                with st.form(f"reply_form_{item['id']}"):
                    level = st.selectbox("重要级", ["低", "一般", "重要", "紧急"], index=1)
                    reply = st.text_area("回复", height=90)
                    submitted = st.form_submit_button("提交回复", type="primary")
                if submitted:
                    if not reply.strip():
                        st.error("回复不能为空")
                    else:
                        try:
                            api.reply_feedback(item["id"], level, reply.strip())
                            st.success("已回复")
                            st.rerun()
                        except ApiError as exc:
                            st.error(exc.message)


def _security_export(api: ApiClient) -> None:
    """密保问题修改 + 数据导出。"""
    st.markdown("### 管理员密保")
    st.caption("管理员密码固定不可改；密保问题/答案可以在这里修改。修改前必须再次验证当前密码。")
    try:
        current = api.get_security_question()
    except ApiError as exc:
        st.error(exc.message)
        current = {}
    st.info("当前问题：" + (current.get("question") or "未设置"))
    with st.form("security_question_form"):
        password = st.text_input("当前管理员密码", type="password", autocomplete="current-password")
        question = st.text_input("新的密保问题", max_chars=128)
        answer = st.text_input("新的密保答案", type="password", max_chars=128)
        submitted = st.form_submit_button("更新密保", type="primary")
    if submitted:
        if len(question.strip()) < 5 or len(answer.strip()) < 2:
            st.error("问题至少 5 字，答案至少 2 字")
        else:
            try:
                result = api.update_security_question(password, question.strip(), answer.strip())
                st.success(result.get("message", "已更新"))
            except ApiError as exc:
                st.error(exc.message)

    st.divider()
    st.markdown("### 数据导出")
    st.caption("导出 CSV + manifest 的 ZIP；含个人资料，请妥善保管。简历原文件不在 ZIP 内。")
    if st.button("生成导出包", type="primary"):
        with st.spinner("打包数据中..."):
            try:
                content, filename = api.admin_export_bytes()
                st.session_state["admin_export"] = {"content": content, "filename": filename}
            except ApiError as exc:
                st.error(exc.message)
    export = st.session_state.get("admin_export")
    if export:
        st.download_button(
            "下载导出包",
            data=export["content"],
            file_name=export["filename"],
            mime="application/zip",
            use_container_width=False,
        )


def render_admin(api: ApiClient) -> None:
    """管理员主页面。"""
    ui.page_header("管理员控制台", "审批、用量、反馈、数据导出；系统文件与磁盘在“系统后端”。")
    tabs = st.tabs(["数据总览", "注册审核", "账号管理", "密码重置", "反馈", "安全与导出"])
    with tabs[0]:
        _overview(api)
    with tabs[1]:
        _registrations(api)
    with tabs[2]:
        _account_management(api)
    with tabs[3]:
        _resets(api)
    with tabs[4]:
        _feedback(api)
    with tabs[5]:
        _security_export(api)
