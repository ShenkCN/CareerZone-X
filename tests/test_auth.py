# ============================================================
# 认证测试：账号/密码规则、注册审核、管理员二次验证、改密。
# ============================================================
from __future__ import annotations

from fastapi.testclient import TestClient


def test_username_rejects_symbols(client: TestClient):
    response = client.post(
        "/api/auth/register",
        json={"username": "bad_name", "password": "GoodPass#2026"},
    )
    assert response.status_code == 422
    assert "账号" in response.text


def test_pure_numeric_password_is_allowed(client: TestClient):
    """用户明确要求纯数字密码；6 位以上、含数字即可注册。"""
    response = client.post(
        "/api/auth/register",
        json={"username": "numericpass", "password": "12345678"},
    )
    assert response.status_code == 201, response.text


def test_pure_letter_password_is_allowed(client: TestClient):
    """纯字母密码也允许；账号仍必须 5—15 位。"""
    response = client.post(
        "/api/auth/register",
        json={"username": "letterpass", "password": "abcdefgh"},
    )
    assert response.status_code == 201, response.text


def test_short_password_rejected(client: TestClient):
    """密码下限是 6 位；长度不足仍要给出具体错误。"""
    response = client.post(
        "/api/auth/register",
        json={"username": "validname", "password": "abc"},
    )
    assert response.status_code == 422
    assert "密码" in response.text and "6" in response.text


def test_registration_needs_admin_approval_then_login(client: TestClient, admin_headers):
    username = "pendinguser"
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "GoodPass#2026", "display_name": "待审核"},
    )
    assert response.status_code == 201, response.text
    # 未审核不能登录
    blocked = client.post("/api/auth/login", json={"username": username, "password": "GoodPass#2026"})
    assert blocked.status_code == 403
    # 管理员批准
    users = client.get("/api/admin/users", params={"status": "pending"}, headers=admin_headers).json()
    user_id = next(item["id"] for item in users if item["username"] == username)
    approved = client.post(f"/api/admin/users/{user_id}/approve", json={"note": "ok"}, headers=admin_headers)
    assert approved.status_code == 200
    login = client.post("/api/auth/login", json={"username": username, "password": "GoodPass#2026"})
    assert login.status_code == 200
    assert login.json()["access_token"]


def test_admin_requires_2fa_and_rejects_wrong_answer(client: TestClient):
    first = client.post("/api/auth/login", json={"username": "Shenk", "password": "TestAdmin#2026"})
    assert first.status_code == 200
    data = first.json()
    assert data["requires_2fa"] is True
    wrong = client.post(
        "/api/auth/admin/verify",
        json={"challenge_token": data["challenge_token"], "answer": "WRONG"},
    )
    assert wrong.status_code == 401


def test_change_password_and_admin_cannot_change(client: TestClient, admin_headers):
    # 管理员没有改密接口语义：调用会 403
    response = client.post(
        "/api/auth/change-password",
        json={"old_password": "TestAdmin#2026", "new_password": "AnotherPass#2026"},
        headers=admin_headers,
    )
    assert response.status_code == 403
