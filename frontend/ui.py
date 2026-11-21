# ============================================================
# 通用 UI：主题、徽章、职位卡片、时间/金额格式化、鱼缸组件。
# 所有来自招聘网站/用户输入的文本都先 html.escape，再进 unsafe_allow_html，
# 防止采集到恶意 JD 后在本地页面执行脚本。
# ============================================================
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parent.parent
SHANGHAI = ZoneInfo("Asia/Shanghai")

# 推荐档位 → 中文/颜色；集中在一处，列表和详情不会出现两套口径。
RECOMMENDATION_META = {
    "strong": {"label": "强烈推荐", "emoji": "🟢", "color": "#0f766e", "bg": "#ccfbf1"},
    "worth": {"label": "值得投", "emoji": "🔵", "color": "#1d4ed8", "bg": "#dbeafe"},
    "stretch": {"label": "可冲刺", "emoji": "🟠", "color": "#b45309", "bg": "#fef3c7"},
    "skip": {"label": "不建议", "emoji": "⚪", "color": "#64748b", "bg": "#f1f5f9"},
}


def inject_css() -> None:
    """全局样式。只改视觉，不改变 Streamlit 的可访问性/交互语义。"""
    st.markdown(
        """
        <style>
        /* 主背景：暖白 + 极淡青，长时间看不刺眼 */
        .stApp { background: radial-gradient(1200px 600px at 10% -10%, #e7eeea 0%, #f4f1ea 48%, #f7f4ee 100%); }
        /* 侧边栏：深青，和主区形成层次 */
        [data-testid="stSidebar"] { background: linear-gradient(180deg, #203a38 0%, #284945 60%, #1d3432 100%); }
        [data-testid="stSidebar"] * { color: #edf5f1; }
        [data-testid="stSidebar"] .stButton button { background: rgba(255,255,255,.10); color: #fff; border: 1px solid rgba(255,255,255,.22); }
        [data-testid="stSidebar"] .stButton button:hover { background: rgba(255,255,255,.20); }
        /* 标题字体用系统衬线，避免加载外部字体拖慢离线环境 */
        html, body, .stApp { font-size: 14px; }
        h1, h2, h3 { font-family: "Iowan Old Style", "Palatino Linotype", "Noto Serif SC", serif; letter-spacing: .2px; }
        h1 { color: #213b39; font-size: 1.9rem !important; line-height: 1.15 !important; margin-bottom: .35rem !important; }
        h2 { font-size: 1.45rem !important; line-height: 1.2 !important; }
        h3 { font-size: 1.15rem !important; line-height: 1.25 !important; }
        [data-testid="stVerticalBlock"] { gap: .55rem; }
        [data-testid="stHorizontalBlock"] { gap: .65rem; }
        [data-testid="stMarkdownContainer"] p { line-height: 1.55; }
        .stButton > button { padding: .35rem .7rem; font-size: 13px; }
        .czx-job-title { font-size: 17px; }
        .czx-salary { font-size: 16px; }
        .czx-job-company, .czx-reason { font-size: 12px; }
        .czx-muted { font-size: 12px; }
        div[data-testid="stVerticalBlockBorderWrapper"] > div { padding: .55rem .7rem !important; }
        /* 主按钮：珊瑚色，和深青形成对比 */
        .stButton > button[kind="primary"] { background: #b5654a; border: 1px solid #b5654a; color: #fff; border-radius: 10px; transition: transform .15s ease, background .15s ease; }
        .stButton > button[kind="primary"]:hover { background: #9f543d; border-color: #9f543d; }
        .stButton > button { border-radius: 10px; transition: transform .15s ease, background .15s ease; }
        .stButton > button:active { transform: scale(.985); }
        /* 卡片边框：Streamlit 1.4x 的 bordered container */
        div[data-testid="stVerticalBlockBorderWrapper"] { border-color: rgba(33,59,57,.12) !important; border-radius: 16px; background: rgba(255,253,248,.86); transition: box-shadow .2s ease, transform .2s ease; }
        div[data-testid="stVerticalBlockBorderWrapper"]:hover { box-shadow: 0 10px 28px rgba(33,59,57,.08); transform: translateY(-1px); }
        /* 顶部品牌条 */
        .czx-brand { display:flex; align-items:center; gap:10px; margin-bottom: 2px; }
        .czx-brand .mark { width:38px; height:38px; border-radius:12px; background:#284945; color:#fff; display:flex; align-items:center; justify-content:center; font-size:22px; box-shadow:0 8px 20px rgba(33,59,57,.14); }
        .czx-brand .name { font-size:24px; font-weight:800; color:#213b39; letter-spacing:.5px; }
        .czx-brand .sub { font-size:12px; color:#64748b; margin-top:-2px; }
        /* 顶部小标签 */
        .czx-badge { display:inline-block; padding:2px 9px; border-radius:999px; font-size:12px; font-weight:700; margin-right:4px; }
        .czx-muted { color:#64748b; font-size:13px; }
        .czx-job-title { font-size:19px; font-weight:800; color:#0f172a; margin:0; }
        .czx-job-company { font-size:13px; color:#475569; margin-top:2px; }
        .czx-salary { font-size:18px; font-weight:800; color:#9c5a3c; }
        .czx-reason { font-size:13px; color:#334155; background:rgba(15,118,110,.06); border-left:3px solid #0f766e; padding:7px 10px; border-radius:6px; }
        .czx-source { font-size:11px; color:#475569; background:#f1f5f9; padding:2px 7px; border-radius:999px; }
        .czx-hero { padding: 18px 20px; border-radius: 20px; background: linear-gradient(135deg, #203a38 0%, #2f5752 65%, #3f6f6a 100%); color:#fff; box-shadow:0 12px 32px rgba(33,59,57,.14); animation: czxFadeUp .45s ease-out; }
        .czx-hero h2 { color:#fff; margin:0 0 4px 0; }
        .czx-hero p { color:#ccfbf1; margin:0; }
        .czx-score { font-size:42px; font-weight:900; line-height:1; }
        .czx-result-line { font-size:14px; color:#475569; margin:4px 0 0 0; }
        .czx-result-line b { color:#213b39; font-size:17px; }
        .czx-result-line span { margin:0 8px; color:#94a3b8; }
        .czx-kicker { text-transform:uppercase; letter-spacing:.12em; font-size:11px; color:#64748b; font-weight:700; }
        /* 表格：不依赖 pyarrow/pandas，避免本机 DLL 策略导致 st.dataframe 报错 */
        .czx-table-wrap { max-height: 460px; overflow:auto; border:1px solid rgba(11,59,58,.12); border-radius:12px; background:#fff; }
        table.czx-table { width:100%; border-collapse:collapse; font-size:13px; }
        table.czx-table th { position:sticky; top:0; background:#f1f5f9; color:#334155; text-align:left; padding:8px 10px; font-weight:700; white-space:nowrap; }
        table.czx-table td { padding:7px 10px; border-top:1px solid #e2e8f0; color:#0f172a; vertical-align:top; }
        table.czx-table tr:hover td { background:#f8fafc; }
        /* 头像占位：没有上传时也别空着 */
        .czx-avatar-placeholder { width:72px; height:72px; border-radius:50%; background:#dce8e2; color:#284945; display:flex; align-items:center; justify-content:center; font-size:30px; border:1px solid rgba(33,59,57,.12); }
        .czx-sidebar-avatar { width:64px; height:64px; border-radius:50%; background:#dce8e2; color:#284945; display:flex; align-items:center; justify-content:center; font-size:28px; border:2px solid rgba(255,255,255,.35); margin:4px 0 4px 0; }
        .czx-jd-preview, .czx-jd-box { white-space: normal; overflow-wrap: anywhere; line-height: 1.7; font-size: 13px; color: #334155; background: rgba(255,253,248,.85); border: 1px solid rgba(33,59,57,.10); border-radius: 10px; padding: 10px 12px; }
        .czx-jd-preview { max-height: 180px; overflow: hidden; position: relative; }
        .czx-jd-box { max-height: 420px; overflow-y: auto; }
        /* 轻微入场动画，不阻塞交互 */
        @keyframes czxFadeUp { from { opacity:.0; transform: translateY(6px); } to { opacity:1; transform: translateY(0); } }
        @media (prefers-reduced-motion: reduce) { * { animation-duration: .01ms !important; transition-duration: .01ms !important; } }
        /* 手机/窄屏：所有 columns 纵向堆叠，表格横向滚动 */
        @media (max-width: 820px) {
            [data-testid="stHorizontalBlock"] { flex-direction: column !important; gap: .55rem !important; }
            [data-testid="column"] { width: 100% !important; flex: 1 1 100% !important; min-width: 100% !important; }
            .czx-hero { padding: 14px 16px; }
            .czx-hero h2 { font-size: 25px; line-height: 1.25; }
            .czx-brand .name { font-size: 21px; }
            .czx-job-title { font-size: 17px; }
            .czx-salary { font-size: 16px; }
            .czx-table-wrap { max-height: 320px; }
            [data-testid="stSidebar"] { min-width: 84vw !important; max-width: 88vw !important; }
            h1 { font-size: 26px !important; }
            h2 { font-size: 21px !important; }
            [data-testid="stVerticalBlockBorderWrapper"] > div { padding: .5rem .6rem !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def brand_header(subtitle: str = "把“看见的职位”变成可解释的投递决策") -> None:
    """品牌条：所有页面共用，避免每页重写标题。"""
    st.markdown(
        f"""
        <div class="czx-brand">
          <div class="mark">🐟</div>
          <div><div class="name">CareerZone X</div><div class="sub">{html.escape(subtitle)}</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def page_header(title: str, subtitle: str = "") -> None:
    """页面标题 + 一句说明；统一间距。"""
    st.markdown(f"## {html.escape(title)}")
    if subtitle:
        st.markdown(f'<div class="czx-muted">{html.escape(subtitle)}</div>', unsafe_allow_html=True)


def badge(text: str, color: str = "#0f766e", bg: str = "#ccfbf1") -> str:
    """生成安全的 HTML 徽章；调用方负责已转义 text 或传入常量。"""
    return f'<span class="czx-badge" style="color:{color};background:{bg}">{html.escape(str(text))}</span>'


def recommendation_badge(rec: Optional[str]) -> str:
    """推荐档位徽章；未知档位显示“待分析”，不误导。"""
    meta = RECOMMENDATION_META.get(rec or "", None)
    if not meta:
        return badge("待分析", "#64748b", "#f1f5f9")
    return badge(f"{meta['emoji']} {meta['label']}", meta["color"], meta["bg"])


def fallback_badge(is_fallback: bool) -> str:
    """明确标注规则兜底，避免用户把规则分当成 DeepSeek 的结论。"""
    if is_fallback:
        return badge("规则初筛 · 非 AI", "#92400e", "#fef3c7")
    return badge("DeepSeek 分析", "#0f766e", "#ccfbf1")


def safe_url(url: str) -> str:
    """只允许 http/https；防止 javascript:/data: 链接被 Streamlit 渲染。"""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return ""
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return url
    return ""


def format_datetime(value: Any) -> str:
    """UTC 时间转上海时间；解析失败就原样返回，不让页面崩。"""
    if not value:
        return "—"
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(value)[:16]


def format_salary(job: dict[str, Any]) -> str:
    """优先展示原站薪资文本；没有就用归一化区间。"""
    text = (job.get("salary_text") or "").strip()
    if text:
        return text
    low, high = job.get("salary_min"), job.get("salary_max")
    if low and high:
        return f"{low // 1000}K—{high // 1000}K"
    if low:
        return f"{low // 1000}K 起"
    return "薪资未注明"


def job_tags(job: dict[str, Any]) -> list[str]:
    """把城市/经验/学历/类型/规模拼成标签，过滤空值并去重。"""
    values = [
        job.get("city"),
        job.get("experience"),
        job.get("education"),
        job.get("job_type"),
        job.get("company_size"),
    ]
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result[:6]


def render_job_card(
    job: dict[str, Any],
    *,
    key_prefix: str,
    show_actions: bool = True,
) -> Optional[str]:
    """渲染一张职位卡，返回用户动作："detail" / "bookmark" / None。

    为什么不用纯 HTML 卡片：收藏/详情需要真实按钮和回调；
    卡片内只把不可交互的文字用 HTML 排版，按钮仍交给 Streamlit。
    """
    screening = job.get("screening") or {}
    with st.container(border=True):
        left, right = st.columns([3.4, 1])
        with left:
            st.markdown(
                f'<div class="czx-job-title">{html.escape(job.get("title") or "未知职位")}</div>'
                f'<div class="czx-job-company">{html.escape(job.get("company") or "未知公司")} · '
                f'{html.escape(job.get("city") or "城市未注明")}</div>',
                unsafe_allow_html=True,
            )
            tags_html = " ".join(badge(tag) for tag in job_tags(job))
            source = job.get("source_label") or job.get("source") or "未知来源"
            st.markdown(
                f'<div style="margin:6px 0 4px 0">{tags_html} <span class="czx-source">{html.escape(source)}</span></div>',
                unsafe_allow_html=True,
            )
        with right:
            st.markdown(
                f'<div style="text-align:right"><div class="czx-salary">{html.escape(format_salary(job))}</div>'
                f'<div class="czx-muted">浏览 {int(job.get("view_count") or 0)} 次</div></div>',
                unsafe_allow_html=True,
            )
        if screening:
            score = int(screening.get("fit_score") or 0)
            rec = screening.get("recommendation")
            st.markdown(
                f'{recommendation_badge(rec)} {badge(f"匹配 {score}/100")} '
                f'{fallback_badge(bool(screening.get("is_fallback")))}'
                f'<div class="czx-reason" style="margin-top:7px">{html.escape(screening.get("reason") or "暂无判断理由")}</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(badge("尚未分析", "#64748b", "#f1f5f9"), unsafe_allow_html=True)

        if show_actions:
            actions = st.columns([1.2, 1.2, 1.2, 3.4])
            action = None
            with actions[0]:
                if st.button("查看详情", key=f"{key_prefix}_detail_{job['id']}", use_container_width=True):
                    action = "detail"
            with actions[1]:
                label = "已收藏" if job.get("bookmarked") else "收藏"
                if st.button(label, key=f"{key_prefix}_bm_{job['id']}", use_container_width=True):
                    action = "bookmark"
            with actions[2]:
                link = safe_url(job.get("url") or "")
                if link:
                    st.link_button("原站投递", link, use_container_width=True)
                else:
                    st.button("无原链接", disabled=True, key=f"{key_prefix}_nolink_{job['id']}", use_container_width=True)
            return action
    return None


def render_fish_games(height: int = 360) -> None:
    """加载静态鱼缸小游戏；不传用户数据，纯前端本地运行。"""
    path = ROOT / "frontend" / "components" / "fish_games.html"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        st.info("鱼缸组件文件缺失")
        return
    components.html(content, height=height, scrolling=False)


def set_auth_cookie(token: str) -> None:
    """把会话令牌写入浏览器 Cookie，刷新页面时由 st.context.cookies 读回。

    安全说明：这是本机自用版的取舍——Cookie 必须可被 JS 写入，所以不是 HttpOnly。
    公网部署应换成服务端设置的 HttpOnly + Secure + SameSite Cookie。
    """
    safe_token = token.strip()
    if not safe_token or any(ch in safe_token for ch in ";\r\n"):
        return
    components.html(
        f"""
        <script>
          // 12 小时，与后端普通用户会话时长一致；管理员 4 小时会先过期，重新登录即可。
          document.cookie = "czx_session={safe_token}; path=/; max-age=43200; SameSite=Lax";
        </script>
        """,
        height=1,
        scrolling=False,
    )


def clear_auth_cookie() -> None:
    """退出登录时清除 Cookie；避免刷新后又被自动登录。"""
    components.html(
        """
        <script>
          document.cookie = "czx_session=; path=/; max-age=0; SameSite=Lax";
        </script>
        """,
        height=1,
        scrolling=False,
    )


def scroll_to_top() -> None:
    """切换页面时把主区域滚回顶部。

    Streamlit 的 rerun 会保留滚动位置，从职位列表底部点进详情时
    会直接落在页面中部，看不到标题。这里用 0 高度的组件执行一次 JS。
    """
    components.html(
        """
        <script>
          // 兼容 Streamlit 不同版本的主滚动容器；找不到就退回 window。
          const p = window.parent;
          const main = p.document.querySelector('[data-testid="stMain"]') || p.document.querySelector('section.main');
          if (main) { main.scrollTop = 0; }
          p.scrollTo(0, 0);
        </script>
        """,
        height=0,
        scrolling=False,
    )


def section_gap() -> None:
    """统一段落间距，避免每个页面写不同的空行数。"""
    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)


def html_table(rows: list[dict[str, Any]], max_height: int = 460) -> None:
    """用纯 HTML 渲染表格。

    为什么不用 st.dataframe：本机应用控制策略拦截了 pyarrow 的 DLL，
    st.dataframe 一调用就 ImportError；HTML 表格没有这个依赖，也更好控样式。
    """
    if not rows:
        st.info("暂无数据")
        return
    columns = list(rows[0].keys())
    head = "".join(f"<th>{html.escape(str(col))}</th>" for col in columns)
    body_parts = []
    for row in rows:
        cells = "".join(
            f"<td>{html.escape(str(row.get(col, '') if row.get(col, '') is not None else '—'))}</td>"
            for col in columns
        )
        body_parts.append(f"<tr>{cells}</tr>")
    table = (
        f'<div class="czx-table-wrap" style="max-height:{int(max_height)}px">'
        f'<table class="czx-table"><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(body_parts)}</tbody></table></div>'
    )
    st.markdown(table, unsafe_allow_html=True)


def human_size(value: Any) -> str:
    """字节转可读；用于管理员系统页。"""
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def strip_html(text: str) -> str:
    """极简去标签，用于手动导入预览；不做完整 HTML 解析。"""
    return re.sub(r"<[^>]+>", "", text or "")
