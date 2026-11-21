# ============================================================
# 官方岗位 API 适配器（预留）
#
# 当前没有平台授权，所以默认关闭；配置 official_api_* 后，
# 管理员可以用 /api/admin/official/collect 手动拉取；
# 未来可加定时任务，但绝不再写“自动登录爬 BOSS”。
# ============================================================
from __future__ import annotations

from typing import Any

import requests

from backend.config import Settings
from backend.services.text import clean_text


class OfficialSourceError(RuntimeError):
    """官方 API 配置缺失或返回异常。"""


def official_configured(settings: Settings) -> bool:
    return bool(
        settings.official_api_enabled
        and settings.official_api_base_url.strip()
        and settings.official_api_key.strip()
    )


def _normalize_item(item: dict[str, Any], provider: str) -> dict[str, Any]:
    """把不同官方厂商的字段收口到 ImportJobIn 的形状。

    约定字段（实现方按此返回）：
    source_job_id / url / title / company / city / salary_text /
    experience / education / description / requirements / tags
    字段缺失允许，后续 upsert 会补默认值。
    """
    return {
        "source": "official",
        "source_job_id": clean_text(item.get("source_job_id") or item.get("id"), 128),
        "url": clean_text(item.get("url") or item.get("job_url"), 1024),
        "title": clean_text(item.get("title") or item.get("job_name"), 255),
        "company": clean_text(item.get("company") or item.get("company_name"), 255),
        "city": clean_text(item.get("city"), 64),
        "district": clean_text(item.get("district"), 64),
        "salary_text": clean_text(item.get("salary_text") or item.get("salary"), 128),
        "job_type": clean_text(item.get("job_type"), 16),
        "experience": clean_text(item.get("experience"), 32),
        "education": clean_text(item.get("education"), 32),
        "industry": clean_text(item.get("industry"), 128),
        "company_size": clean_text(item.get("company_size"), 64),
        "address": clean_text(item.get("address"), 512),
        "description": clean_text(item.get("description") or item.get("jd"), 30000),
        "requirements": clean_text(item.get("requirements"), 20000),
        "tags": [clean_text(tag, 32) for tag in (item.get("tags") or []) if clean_text(tag, 32)][:30],
        "raw": {"provider": provider, "official_api": True, "source_item": item},
    }


def fetch_official_jobs(
    settings: Settings,
    *,
    query: str,
    city: str = "",
    page: int = 1,
    page_size: int = 20,
) -> list[dict[str, Any]]:
    """按统一契约请求官方 API；失败抛 OfficialSourceError，不静默爬取。"""
    if not official_configured(settings):
        raise OfficialSourceError(
            "官方 API 未配置。请在 .env 设置 OFFICIAL_API_ENABLED=true、"
            "OFFICIAL_API_BASE_URL、OFFICIAL_API_KEY。"
        )
    base = settings.official_api_base_url.rstrip("/")
    url = base + "/jobs"
    try:
        response = requests.get(
            url,
            params={
                "query": query,
                "city": city,
                "page": max(1, page),
                "page_size": max(1, min(page_size, 200)),
            },
            headers={"Authorization": f"Bearer {settings.official_api_key.strip()}"},
            timeout=settings.official_api_timeout,
        )
    except requests.RequestException as exc:
        raise OfficialSourceError(f"官方 API 连接失败：{exc}") from exc
    if response.status_code >= 400:
        raise OfficialSourceError(f"官方 API 返回 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OfficialSourceError("官方 API 返回不是 JSON") from exc
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise OfficialSourceError("官方 API 返回格式错误：需要 items 数组")
    provider = settings.official_api_base_url.split("//")[-1].split("/")[0]
    result: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            normalized = _normalize_item(item, provider)
            if normalized.get("title"):
                result.append(normalized)
    return result
