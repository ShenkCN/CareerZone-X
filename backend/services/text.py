# ============================================================
# 文本与来源工具：职位字段清洗、薪资归一化、来源白名单。
# 这些函数不碰数据库，方便单独写测试。
# ============================================================
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from backend.schemas import SOURCE_LABELS

# 只允许这五个招聘平台的域名（含子域名）导入；避免扩展被利用去抓任意网站。
SOURCE_HOSTS = {
    "boss": ("zhipin.com",),
    "liepin": ("liepin.com",),
    "zhaopin": ("zhaopin.com",),
    "51job": ("51job.com",),
    "yupao": ("yupao.com",),
}

# 企业官网/手动导入不限定域名，但仍然只允许 http/https。
FLEXIBLE_SOURCES = {"official", "manual", "demo"}

_WS_RE = re.compile(r"[ \t\u3000]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
# BOSS 等站点用私有区字符画图标；不清掉会把“公司名”变成一串乱码。
_PRIVATE_USE_RE = re.compile(r"[\ue000-\uf8ff\ufffd]")
_SALARY_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[-~—至]\s*(\d+(?:\.\d+)?)\s*([kK千万元]?)"
)
_SALARY_SINGLE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([kK千万元])")
# BOSS 详情页会带 <style> 的 class 样式；旧版扩展用 textContent 把它抓进了 JD。
_CSS_BLOCK_RE = re.compile(r"\.[A-Za-z0-9_-]+\{[^{}]*\}")
_CSS_DECL_RE = re.compile(
    r"\b(?:display|width|height|overflow|visibility|font-size|font-style|font-weight|line-height|position|z-index)\s*:[^;{}]+;?",
    re.I,
)
_UI_NOISE_RE = re.compile(r"(?:收藏|立即沟通|举报|微信扫码分享)\s*")


def clean_text(value: Any, max_len: int = 20000) -> str:
    """统一空白/换行，并截断超长字段，防止把 10MB 文本写进数据库。"""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _PRIVATE_USE_RE.sub("", text)
    text = _WS_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()[:max_len]


def clean_jd_text(value: Any, max_len: int = 20000) -> str:
    """清掉旧采集混入的 CSS/页面按钮文本，保留 JD 正文。"""
    text = str(value or "")
    text = _CSS_BLOCK_RE.sub(" ", text)
    if "{" in text or "display:" in text or "font-style:" in text:
        text = _CSS_DECL_RE.sub(" ", text)
    text = _UI_NOISE_RE.sub(" ", text)
    # 详情页底部会带招聘方名片、地址、推荐职位；JD 正文到这里结束。
    for marker in ("工作地址", "查看更多信息", "求职工具", "热门职位", "公司基本信息", "招聘者", "去App", "前往App", "与BOSS随时沟通"):
        index = text.find(marker)
        if index > 100:
            text = text[:index]
    text = text.replace("BOSS直聘", "")
    cleaned = clean_text(text, max_len)
    # 详情页如果只抓到下载 App/页面按钮，不要冒充 JD 覆盖旧数据。
    if len(cleaned) < 80 and any(word in cleaned for word in ("下载App", "不错过", "立即沟通", "收藏")):
        return ""
    return cleaned


def source_label(source: str) -> str:
    """来源代码转中文；未知来源不直接回显，避免 XSS/脏数据。"""
    return SOURCE_LABELS.get(source, "其他来源")


def allowed_host_for_source(source: str, url: str) -> bool:
    """校验职位 URL 是否属于该来源。

    安全意义：即使采集令牌泄露，攻击者也不能通过本接口把恶意链接
    伪装成 BOSS 职位；前端跳转前还会再次确认域名。
    """
    if not url:
        return source in FLEXIBLE_SOURCES
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower().strip(".")
    if not host:
        return False
    if source in FLEXIBLE_SOURCES:
        return True
    suffixes = SOURCE_HOSTS.get(source, ())
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def detect_source_from_url(url: str) -> str:
    """从 URL 反推来源；识别不了就归到 manual，绝不猜成某个平台。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return "manual"
    for source, suffixes in SOURCE_HOSTS.items():
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return source
    return "manual"


def parse_salary(text: str) -> tuple[Optional[int], Optional[int]]:
    """把中文薪资文本粗略归一成“月薪元”。

    只做保守解析：无法确定周期时宁可为空，避免把“20元/小时”误判成 20K。
    日薪按 21.75 个工作日折算，年薪按 12 个月折算，仅用于筛选排序。
    """
    raw = (text or "").strip().lower()
    if not raw or any(word in raw for word in ("面议", "保密", "negotiable")):
        return None, None

    # 先处理“元/天”“元/小时”，避免被下面的 K 逻辑误伤。
    if "元/天" in raw or "/天" in raw:
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|~|—|至)?\s*(\d+(?:\.\d+)?)?", raw)
        if match:
            low = float(match.group(1)) * 21.75
            high = float(match.group(2)) * 21.75 if match.group(2) else low
            return int(low), int(high)
    if "元/时" in raw or "/时" in raw or "小时" in raw:
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|~|—|至)?\s*(\d+(?:\.\d+)?)?", raw)
        if match:
            low = float(match.group(1)) * 21.75 * 8
            high = float(match.group(2)) * 21.75 * 8 if match.group(2) else low
            return int(low), int(high)

    match = _SALARY_RANGE_RE.search(raw)
    if match:
        low = float(match.group(1))
        high = float(match.group(2))
        unit = match.group(3)
        if unit in {"k", "千"}:
            low, high = low * 1000, high * 1000
        elif unit == "万":
            # “1-1.5万/月”常见；如果是年薪会另写“/年”，这里先按月处理。
            low, high = low * 10000, high * 10000
        if "年" in raw and "月" not in raw:
            low, high = low / 12, high / 12
        return int(low), int(high)

    match = _SALARY_SINGLE_RE.search(raw)
    if match:
        value = float(match.group(1))
        unit = match.group(2)
        if unit in {"k", "千"}:
            value *= 1000
        elif unit == "万":
            value *= 10000
        if "年" in raw and "月" not in raw:
            value /= 12
        return int(value), int(value)
    return None, None


def make_fingerprint(source: str, title: str, company: str, city: str, url: str = "") -> str:
    """职位去重指纹。

    包含 source：同一岗位在不同平台分别展示，用户需要知道来源；
    包含 url 作为兜底：同名岗位在不同门店/不同链接时不会被误合并。
    """
    normalized = "|".join(
        [
            source.strip().lower(),
            re.sub(r"\s+", "", title or "").lower(),
            re.sub(r"\s+", "", company or "").lower(),
            re.sub(r"\s+", "", city or "").lower(),
            (url or "").split("?")[0].rstrip("/").lower(),
        ]
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def profile_hash(data: dict[str, Any]) -> str:
    """画像哈希：分析缓存键；只取影响匹配的字段，联系方式不参与。"""
    keys = [
        "target_roles",
        "target_cities",
        "job_type",
        "salary_min",
        "salary_max",
        "experience",
        "education",
        "skills",
        "industries",
        "preferred_company_sizes",
        "city",
        "bio",
    ]
    payload = {key: data.get(key) for key in keys}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def json_default(value: Any) -> Any:
    """JSON 序列化兜底：datetime 转 ISO，其他对象转字符串。"""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def utc_iso(value: Optional[datetime]) -> Optional[str]:
    """输出给前端的时间统一带 Z，避免浏览器按本地时区误解。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def clamp_score(value: Any, default: int = 0) -> int:
    """AI 返回的分数可能越界/是字符串；统一夹到 0—100。"""
    try:
        score = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(0, min(100, score))


def normalize_recommendation(value: Any) -> str:
    """只接受四档建议；模型胡写时降级为 stretch，不污染前端样式。"""
    text = str(value or "").strip().lower()
    mapping = {
        "strong": "strong",
        "强烈推荐": "strong",
        "worth": "worth",
        "值得投": "worth",
        "值得": "worth",
        "stretch": "stretch",
        "冲刺": "stretch",
        "skip": "skip",
        "不建议": "skip",
    }
    return mapping.get(text, "stretch")
