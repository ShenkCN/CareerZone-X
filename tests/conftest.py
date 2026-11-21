# ============================================================
# 测试夹具：独立 SQLite + 关闭限流/向量 + 固定管理员凭据。
# 必须在 import backend 之前设置环境变量，否则 Settings 会先读 .env。
# ============================================================
from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = ROOT / "storage" / "test_careerzone.db"
TEST_DB.parent.mkdir(parents=True, exist_ok=True)
if TEST_DB.exists():
    TEST_DB.unlink()

os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": f"sqlite:///{TEST_DB.as_posix()}",
        "APP_SECRET_KEY": "test-secret-key-not-for-production-0123456789",
        "ADMIN_USERNAME": "Shenk",
        "ADMIN_INITIAL_PASSWORD": "TestAdmin#2026",
        "ADMIN_SECURITY_QUESTION": "测试用管理员暗号？",
        "ADMIN_INITIAL_ANSWER": "TEST2026",
        "LLM_ENABLED": "false",
        "VECTOR_ENABLED": "false",
        "RATE_LIMIT_ENABLED": "false",
        "MAX_RESUME_MB": "2",
        "MAX_AVATAR_MB": "1",
    }
)

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    """整个测试会话共享一个 TestClient；with 会触发 lifespan 建表/管理员。"""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def admin_token(client: TestClient) -> str:
    """管理员两步登录：密码 → 密保答案。"""
    first = client.post("/api/auth/login", json={"username": "Shenk", "password": "TestAdmin#2026"})
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["requires_2fa"] is True
    second = client.post(
        "/api/auth/admin/verify",
        json={"challenge_token": data["challenge_token"], "answer": "TEST2026"},
    )
    assert second.status_code == 200, second.text
    return second.json()["access_token"]


@pytest.fixture(scope="session")
def admin_headers(admin_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}


def _register(client: TestClient, username: str, password: str = "UserPass#2026") -> dict:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": password, "display_name": "测试用户"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def approve(client: TestClient, admin_headers: dict[str, str], username: str) -> None:
    users = client.get("/api/admin/users", params={"status": "pending"}, headers=admin_headers)
    assert users.status_code == 200, users.text
    user = next(item for item in users.json() if item["username"] == username)
    response = client.post(
        f"/api/admin/users/{user['id']}/approve",
        json={"note": "测试通过"},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text


@pytest.fixture()
def user_token(client: TestClient, admin_headers: dict[str, str]) -> str:
    """注册 → 管理员批准 → 登录，返回普通用户 token。"""
    import uuid

    username = "u" + uuid.uuid4().hex[:8]
    _register(client, username)
    approve(client, admin_headers, username)
    login = client.post("/api/auth/login", json={"username": username, "password": "UserPass#2026"})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


@pytest.fixture()
def user_headers(user_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user_token}"}


@pytest.fixture()
def profile(user_headers: dict[str, str], client: TestClient) -> dict:
    """给需要搜索的测试准备一份最小画像。"""
    payload = {
        "real_name": "张三",
        "phone": "13123661035",
        "email": "test@example.com",
        "city": "苏州",
        "target_cities": ["苏州", "上海"],
        "target_roles": ["AI 应用交付"],
        "job_type": "全职",
        "salary_min": 8000,
        "salary_max": 12000,
        "experience": "1-3年",
        "education": "大专",
        "skills": ["Python", "FastAPI", "RAG"],
        "industries": ["人工智能"],
        "preferred_company_sizes": ["20-99人"],
        "bio": "1.5 年企业 IT 交付经验，做过 RAG 知识库。",
        "github": "https://github.com/ShenkCN",
        "portfolio": "",
    }
    response = client.put("/api/profile", json=payload, headers=user_headers)
    assert response.status_code == 200, response.text
    return response.json()
