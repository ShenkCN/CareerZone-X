# ============================================================
# 清空语义：A 的采集批次对 B 可见；A 清空后 B 没有自己的副本也应消失。
# ============================================================
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def _make_user(client: TestClient, admin_headers: dict, prefix: str) -> dict:
    username = prefix + uuid.uuid4().hex[:7]
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "UserPass#2026", "display_name": username},
    )
    assert response.status_code == 201, response.text
    users = client.get("/api/admin/users", params={"status": "pending"}, headers=admin_headers).json()
    user = next(item for item in users if item["username"] == username)
    assert client.post(
        f"/api/admin/users/{user['id']}/approve",
        json={"note": "batch visibility test"},
        headers=admin_headers,
    ).status_code == 200
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
    import_token = client.post("/api/import/token", headers=headers).json()["token"]
    return {"id": user["id"], "headers": headers, "import_token": import_token}


def test_owner_clear_removes_batch_for_others_without_copy(
    client: TestClient, admin_headers: dict
):
    a = _make_user(client, admin_headers, "oa")
    b = _make_user(client, admin_headers, "ob")
    c = _make_user(client, admin_headers, "oc")
    unique = uuid.uuid4().hex[:10]
    url = "https://www.zhipin.com/job_detail/batch-" + unique + ".html"
    title = "批次可见性测试" + unique
    job = {
        "title": title,
        "company": "测试公司",
        "city": "上海",
        "url": url,
        "salary_text": "10-20K",
        "description": "验证 A 清空后，没有自己副本的 B/C 也看不到；恢复后重新可见。",
        "requirements": "",
    }
    imported = client.post(
        "/api/jobs/import",
        json={"source": "boss", "page_url": url, "mode": "detail", "jobs": [job]},
        headers={"X-Import-Key": a["import_token"]},
    )
    assert imported.status_code == 200, imported.text
    job_id = imported.json()["job_ids"][0]

    def search(item):
        response = client.post(
            "/api/jobs/search",
            json={"keyword": unique, "screen": False, "reset_exclusions": True},
            headers=item["headers"],
        )
        assert response.status_code == 200, response.text
        return response.json()["total"]

    assert search(a) == 1
    assert search(b) == 1
    assert search(c) == 1

    cleared = client.delete("/api/jobs/clear", headers={"X-Import-Key": a["import_token"]})
    assert cleared.status_code == 200, cleared.text
    assert search(a) == 0
    assert search(b) == 0
    assert search(c) == 0

    restored = client.post(
        f"/api/admin/users/{a['id']}/restore-jobs",
        json={"job_ids": [job_id]},
        headers=admin_headers,
    )
    assert restored.status_code == 200, restored.text
    assert search(a) == 1
    assert search(b) == 1
    assert search(c) == 1
