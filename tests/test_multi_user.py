# ============================================================
# 多用户清空/恢复测试：A 清空不能影响 B；管理员可恢复 A。
# ============================================================
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def _create_user(client: TestClient, admin_headers: dict, prefix: str) -> tuple[str, dict]:
    username = prefix + uuid.uuid4().hex[:7]
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "UserPass#2026", "display_name": username},
    )
    assert response.status_code == 201, response.text
    users = client.get("/api/admin/users", params={"status": "pending"}, headers=admin_headers).json()
    user = next(item for item in users if item["username"] == username)
    approve = client.post(
        f"/api/admin/users/{user['id']}/approve",
        json={"note": "multi-user test"},
        headers=admin_headers,
    )
    assert approve.status_code == 200, approve.text
    login = client.post("/api/auth/login", json={"username": username, "password": "UserPass#2026"})
    assert login.status_code == 200, login.text
    headers = {"Authorization": "Bearer " + login.json()["access_token"]}
    profile = {
        "real_name": "张三", "phone": "", "email": "", "city": "上海",
        "target_cities": ["上海"], "target_roles": ["测试工程师"], "job_type": "全职",
        "salary_min": None, "salary_max": None, "experience": "", "education": "",
        "skills": [], "industries": [], "preferred_company_sizes": [], "bio": "",
        "github": "", "portfolio": "",
    }
    assert client.put("/api/profile", json=profile, headers=headers).status_code == 200
    token = client.post("/api/import/token", headers=headers).json()["token"]
    return username, {"headers": headers, "import_token": token, "id": user["id"]}


def test_clear_is_per_user_and_admin_can_restore(client: TestClient, admin_headers, user_headers):
    a_name, a = _create_user(client, admin_headers, "a")
    b_name, b = _create_user(client, admin_headers, "b")
    job_url = "https://www.zhipin.com/job_detail/multi-user-" + uuid.uuid4().hex[:10] + ".html"
    job = {
        "title": "多用户恢复测试工程师",
        "company": "测试公司",
        "city": "上海",
        "url": job_url,
        "salary_text": "10-20K",
        "description": "用于验证 A 清空不会影响 B，管理员可以恢复 A 的可见性。",
        "requirements": "",
    }
    for item in (a, b):
        imported = client.post(
            "/api/jobs/import",
            json={"source": "boss", "page_url": job_url, "mode": "detail", "jobs": [job]},
            headers={"X-Import-Key": item["import_token"]},
        )
        assert imported.status_code == 200, imported.text
        assert imported.json()["inserted"] + imported.json()["updated"] == 1

    def search(item):
        response = client.post(
            "/api/jobs/search",
            json={"keyword": "多用户恢复测试", "screen": False, "reset_exclusions": True},
            headers=item["headers"],
        )
        assert response.status_code == 200, response.text
        return response.json()["total"]

    assert search(a) == 1
    assert search(b) == 1

    cleared = client.delete("/api/jobs/clear", headers={"X-Import-Key": a["import_token"]})
    assert cleared.status_code == 200, cleared.text
    assert search(a) == 0
    assert search(b) == 1

    hidden = client.get("/api/jobs/cleared", headers=a["headers"]).json()["items"]
    assert len(hidden) == 1
    job_id = hidden[0]["job"]["id"]

    request = client.post(
        "/api/jobs/restore-request",
        json={"job_ids": [job_id], "note": "请恢复"},
        headers=a["headers"],
    )
    assert request.status_code == 200, request.text
    requests_data = client.get("/api/admin/restore-requests", headers=admin_headers).json()["items"]
    request_id = next(item["id"] for item in requests_data if item["job_ids"] == [job_id])
    approved = client.post(f"/api/admin/restore-requests/{request_id}/approve", headers=admin_headers)
    assert approved.status_code == 200, approved.text
    assert search(a) == 1

    users = client.get("/api/admin/users", headers=admin_headers).json()
    a_row = next(item for item in users if item["username"] == a_name)
    assert a_row["job_captures"] >= 1
