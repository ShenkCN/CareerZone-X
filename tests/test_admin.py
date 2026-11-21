# ============================================================
# 管理员测试：仪表盘、审批、密码重置、导出、系统信息、密保。
# ============================================================
from __future__ import annotations

from fastapi.testclient import TestClient


def test_dashboard_and_system(client: TestClient, admin_headers):
    dashboard = client.get("/api/admin/dashboard", headers=admin_headers)
    assert dashboard.status_code == 200, dashboard.text
    assert "counts" in dashboard.json()
    system = client.get("/api/admin/system", headers=admin_headers)
    assert system.status_code == 200, system.text
    assert "project_size_bytes" in system.json()


def test_export_returns_zip(client: TestClient, admin_headers):
    response = client.get("/api/admin/export", headers=admin_headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    assert response.content[:2] == b"PK"


def test_forgot_password_flow(client: TestClient, admin_headers):
    username = "forgotuser"
    client.post(
        "/api/auth/register",
        json={"username": username, "password": "OldPass#2026", "display_name": "忘记密码"},
    )
    users = client.get("/api/admin/users", params={"status": "pending"}, headers=admin_headers).json()
    user_id = next(item["id"] for item in users if item["username"] == username)
    client.post(f"/api/admin/users/{user_id}/approve", json={"note": "ok"}, headers=admin_headers)
    request = client.post("/api/auth/forgot-password", json={"username": username, "note": "test"})
    assert request.status_code == 200

    requests = client.get("/api/admin/password-resets", params={"status": "pending"}, headers=admin_headers)
    assert requests.status_code == 200
    item = next(row for row in requests.json() if row["username"] == username)
    approved = client.post(f"/api/admin/password-resets/{item['id']}/approve", headers=admin_headers)
    assert approved.status_code == 200, approved.text
    code = approved.json()["code"]
    assert code
    reset = client.post(
        "/api/auth/reset-password",
        json={"username": username, "code": code, "new_password": "NewPass#2026"},
    )
    assert reset.status_code == 200, reset.text
    login = client.post("/api/auth/login", json={"username": username, "password": "NewPass#2026"})
    assert login.status_code == 200
