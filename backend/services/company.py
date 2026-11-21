# ============================================================
# 公司名清洗：招聘列表卡片经常把“职位+薪资+经验+学历+公司”拼成一段，
# 直接展示会变成一长串乱码。这里只做保守提取，拿不准就显示“未知公司”。
# ============================================================
from __future__ import annotations

import re

_COMPANY_SUFFIX_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9（）()]{2,30}"
    r"(?:股份有限公司|有限责任公司|有限公司|集团|科技|网络|信息技术|银行|研究院|中心))"
)
_EDU_PREFIX_RE = re.compile(
    r"^(?:在校/应届|应届|大专|本科|硕士|博士|学历不限|不限|经验不限|以内|\d+年以内|\d+-\d+年|\d+年)+"
)
# 有些卡片把“K1-3年本科”直接粘在公司前，反复剥掉这些残渣。
_JUNK_PREFIX_RE = re.compile(r"^[Kk\d.．\-年月薪·\s]+")


def clean_company(value: str) -> str:
    """从脏公司字段里提取公司名；宁可“未知公司”，不展示乱码。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or text in {"未知公司", "None"}:
        return "未知公司"
    # 先剥掉“K/数字/学历”前缀，再判断是否已是短公司名。
    for _ in range(3):
        text = _JUNK_PREFIX_RE.sub("", text)
        text = _EDU_PREFIX_RE.sub("", text)
    text = text.strip()
    if not text:
        return "未知公司"
    # 短的正常公司名直接返回（如“上海云帆网络有限公司”）。
    if len(text) <= 24 and "K" not in text and "年" not in text:
        return text[:80]

    matches = _COMPANY_SUFFIX_RE.findall(text)
    if not matches:
        return "未知公司"
    candidate = matches[-1]
    # 反复剥离“K/数字/年月/学历”前缀，直到稳定。
    for _ in range(3):
        candidate = _JUNK_PREFIX_RE.sub("", candidate)
        candidate = _EDU_PREFIX_RE.sub("", candidate)
    candidate = candidate.strip()
    if len(candidate) < 2:
        return "未知公司"
    return candidate[:80]
