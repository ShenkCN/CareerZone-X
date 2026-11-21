# ============================================================
# 职位测试：导入去重、筛选、分页、收藏、历史、权限。
# 不调用真实 DeepSeek：LLM_ENABLED=false，初筛走规则。
# ============================================================
from __future__ import annotations

from fastapi.testclient import TestClient


def _import_token(client: TestClient, user_headers: dict[str, str]) -> str:
    response = client.post("/api/import/token", headers=user_headers)
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _job(i: int) -> dict:
    return {
        "source": "boss",
        "page_url": "https://www.zhipin.com/web/geek/jobs?query=AI",
        "mode": "list",
        "jobs": [
            {
                "source_job_id": f"job-{i}",
                "url": f"https://www.zhipin.com/job_detail/{i}.html",
                "title": f"AI 应用交付工程师 {i}",
                "company": f"测试科技 {i} 有限公司",
                "city": "苏州",
                "salary_text": "9-14K",
                "job_type": "全职",
                "experience": "1-3年",
                "education": "大专",
                "description": "负责 RAG 知识库、FastAPI 接口、客户交付。",
                "requirements": "Python、Linux、Docker。",
                "tags": ["RAG", "FastAPI"],
            }
        ],
    }


def test_import_search_bookmark_history(client: TestClient, user_headers, profile):
    token = _import_token(client, user_headers)
    headers = {"X-Import-Key": token}
    first = client.post("/api/jobs/import", json=_job(1), headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["inserted"] == 1
    # 同 URL 再导入应更新而不是重复
    second = client.post("/api/jobs/import", json=_job(1), headers=headers)
    assert second.status_code == 200
    assert second.json()["inserted"] == 0
    assert second.json()["updated"] == 1

    search = client.post(
        "/api/jobs/search",
        json={"keyword": "AI", "screen": False, "reset_exclusions": True},
        headers=user_headers,
    )
    assert search.status_code == 200, search.text
    data = search.json()
    assert data["total"] >= 1
    assert data["items"]
    job_id = data["items"][0]["id"]
    assert data["items"][0]["source"] == "boss"

    detail = client.get(f"/api/jobs/{job_id}", headers=user_headers)
    assert detail.status_code == 200
    assert "RAG" in detail.json()["description"]
    assert detail.json()["job"]["view_count"] >= 1

    bookmark = client.post(f"/api/jobs/{job_id}/bookmark", headers=user_headers)
    assert bookmark.status_code == 200
    assert bookmark.json()["bookmarked"] is True
    bookmarks = client.get("/api/bookmarks", headers=user_headers)
    assert bookmarks.status_code == 200
    assert any(item["id"] == job_id for item in bookmarks.json()["items"])

    history = client.get("/api/history", headers=user_headers)
    assert history.status_code == 200
    assert any(item["job"]["id"] == job_id for item in history.json()["items"])


def test_filters_and_1000_limit(client: TestClient, user_headers, profile):
    token = _import_token(client, user_headers)
    headers = {"X-Import-Key": token}
    for i in range(2, 26):
        response = client.post("/api/jobs/import", json=_job(i), headers=headers)
        assert response.status_code == 200, response.text
    search = client.post(
        "/api/jobs/search",
        json={"city": "苏州", "salary_min": 9000, "screen": False, "reset_exclusions": True},
        headers=user_headers,
    )
    assert search.status_code == 200
    data = search.json()
    assert data["limit"] == 1000
    assert len(data["items"]) <= 1000
    assert all(item["city"] == "苏州" for item in data["items"])

    # 前端下拉框可能把“不限”原样传给后端；它必须被当作不筛选，
    # 否则会变成 job_type == '不限' 之类的空结果。
    unlimited = client.post(
        "/api/jobs/search",
        json={
            "city": "苏州/上海",
            "job_type": "不限",
            "experience": "不限",
            "education": "不限",
            "company_size": "不限",
            "screen": False,
            "reset_exclusions": True,
        },
        headers=user_headers,
    )
    assert unlimited.status_code == 200
    assert unlimited.json()["total"] >= 20


def test_user_cannot_access_admin(client: TestClient, user_headers):
    response = client.get("/api/admin/dashboard", headers=user_headers)
    assert response.status_code == 403


def test_import_rejects_wrong_host(client: TestClient, user_headers):
    token = _import_token(client, user_headers)
    bad = _job(99)
    bad["jobs"][0]["url"] = "https://evil.example.com/job/1"
    response = client.post("/api/jobs/import", json=bad, headers={"X-Import-Key": token})
    assert response.status_code == 200
    assert response.json()["skipped"] == 1
