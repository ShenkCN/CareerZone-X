# ============================================================
# Streamlit 的 API 客户端：所有请求都走 FastAPI，前端不直接碰数据库。
# 这样做的原因：
# 1) 权限校验只在后端一处，Streamlit 页面绕不过去；
# 2) 以后换 React/手机端，后端不用改；
# 3) 出错时能统一转成中文提示。
# ============================================================
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

# 读取项目根目录 .env；override=False 让系统环境变量优先（方便测试）。
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


class ApiError(RuntimeError):
    """API 返回非 2xx 时抛出；message 可直接展示给用户。"""

    def __init__(self, status_code: int, message: str, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.payload = payload


def _detail_from_response(response: requests.Response) -> str:
    """从 FastAPI 的错误 JSON 中提取可读信息，兼容 detail 为字符串/列表。"""
    try:
        data = response.json()
    except ValueError:
        return f"请求失败（HTTP {response.status_code}）"
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        messages = []
        for item in detail[:4]:
            if isinstance(item, dict):
                messages.append(str(item.get("msg") or item.get("loc") or "参数错误"))
            else:
                messages.append(str(item))
        return "；".join(messages) or f"请求失败（HTTP {response.status_code}）"
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, list) and errors:
        return "；".join(str(item.get("msg", "参数错误")) for item in errors[:4] if isinstance(item, dict))
    return f"请求失败（HTTP {response.status_code}）"


class ApiClient:
    """薄封装：一个方法对应一个后端接口，页面不拼 URL。"""

    def __init__(self, token: Optional[str] = None):
        self.base_url = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        self.token = token
        self.session = requests.Session()

    def set_token(self, token: Optional[str]) -> None:
        self.token = token

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[dict[str, Any]] = None,
        files: Any = None,
        data: Any = None,
        timeout: int = 120,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = self.session.request(
                method,
                url,
                json=json,
                params=params,
                files=files,
                data=data,
                headers=self._headers(),
                timeout=timeout,
            )
        except requests.Timeout as exc:
            raise ApiError(0, "请求超时，AI 分析可能需要更久，请稍后重试") from exc
        except requests.ConnectionError as exc:
            raise ApiError(0, "无法连接 API：请确认 uvicorn 已在 127.0.0.1:8000 启动") from exc

        if response.status_code >= 400:
            raise ApiError(response.status_code, _detail_from_response(response))
        if response.status_code == 204:
            return None
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response.content

    # ---------- 认证 ----------
    def login(self, username: str, password: str) -> dict[str, Any]:
        return self._request("POST", "/api/auth/login", json={"username": username, "password": password})

    def admin_verify(self, challenge_token: str, answer: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/auth/admin/verify",
            json={"challenge_token": challenge_token, "answer": answer},
        )

    def register(self, username: str, password: str, display_name: str = "") -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/auth/register",
            json={"username": username, "password": password, "display_name": display_name},
        )

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/api/auth/me")

    def nudge_approval(self, username: str) -> dict[str, Any]:
        """待审核用户催管理员；不创建登录会话。"""
        return self._request(
            "POST",
            "/api/auth/nudge-approval",
            json={"username": username},
            timeout=30,
        )

    def logout(self) -> None:
        return self._request("POST", "/api/auth/logout")

    def change_password(self, old_password: str, new_password: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/auth/change-password",
            json={"old_password": old_password, "new_password": new_password},
        )

    def forgot_password(self, username: str, note: str = "") -> dict[str, Any]:
        return self._request("POST", "/api/auth/forgot-password", json={"username": username, "note": note})

    def reset_password(self, username: str, code: str, new_password: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/auth/reset-password",
            json={"username": username, "code": code, "new_password": new_password},
        )

    # ---------- 画像 / 简历 ----------
    def get_profile(self) -> dict[str, Any]:
        return self._request("GET", "/api/profile")

    def save_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", "/api/profile", json=payload)

    def update_display_name(self, name: str) -> dict[str, Any]:
        return self._request("PUT", "/api/profile/display-name", json={"display_name": name})

    def upload_avatar(self, filename: str, content: bytes, mime: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/profile/avatar",
            files={"file": (filename, content, mime)},
        )

    def avatar_bytes(self) -> Optional[bytes]:
        try:
            return self._request("GET", "/api/profile/avatar", timeout=30)
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise

    def upload_resume(self, filename: str, content: bytes, mime: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/resumes",
            files={"file": (filename, content, mime)},
            timeout=180,
        )

    def list_resumes(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/resumes")

    def download_resume(self, resume_id: int) -> tuple[bytes, str]:
        response = self.session.get(
            f"{self.base_url}/api/resumes/{resume_id}/download",
            headers=self._headers(),
            timeout=60,
        )
        if response.status_code >= 400:
            raise ApiError(response.status_code, _detail_from_response(response))
        filename = "resume"
        disposition = response.headers.get("content-disposition", "")
        if "filename=" in disposition:
            filename = disposition.split("filename=")[-1].strip('"')
        return response.content, filename

    def activate_resume(self, resume_id: int) -> dict[str, Any]:
        return self._request("POST", f"/api/resumes/{resume_id}/activate")

    # ---------- 职位 ----------
    def search_jobs(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/jobs/search", json=payload, timeout=180)

    def job_detail(self, job_id: int) -> dict[str, Any]:
        return self._request("GET", f"/api/jobs/{job_id}")

    def analyze_job(self, job_id: int, force: bool = False) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/jobs/{job_id}/analyze",
            params={"force": "true" if force else "false"},
            timeout=180,
        )

    def bookmark(self, job_id: int) -> dict[str, Any]:
        return self._request("POST", f"/api/jobs/{job_id}/bookmark")

    def bookmarks(self, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        return self._request("GET", "/api/bookmarks", params={"offset": offset, "limit": limit})

    def history(self, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        return self._request("GET", "/api/history", params={"offset": offset, "limit": limit})

    def top_viewed(self, limit: int = 6) -> dict[str, Any]:
        return self._request("GET", "/api/jobs/top-viewed", params={"limit": limit})

    def similar_jobs(self, job_id: int, limit: int = 5) -> list[dict[str, Any]]:
        return self._request("GET", f"/api/jobs/{job_id}/similar", params={"limit": limit}, timeout=120)

    def sources(self) -> list[dict[str, str]]:
        return self._request("GET", "/api/jobs/sources")["items"]

    def manual_import(self, text: str, url: str, source: str = "manual") -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/jobs/manual",
            json={"text": text, "url": url, "source": source},
            timeout=180,
        )

    def last_import(self) -> dict[str, Any]:
        """最近一次浏览器采集的导入统计。"""
        return self._request("GET", "/api/import/last")

    def get_import_token(self) -> dict[str, Any]:
        return self._request("GET", "/api/import/token")

    def create_import_token(self) -> dict[str, Any]:
        return self._request("POST", "/api/import/token")

    def download_extension(self) -> bytes:
        """下载扩展 ZIP；返回二进制，由前端 download_button 展示。"""
        return self._request("GET", "/api/extension/download", timeout=60)

    # ---------- 反馈 ----------
    def create_feedback(self, category: str, content: str, rating: int) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/feedback",
            json={"category": category, "content": content, "rating": rating},
        )

    def my_feedback(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/feedback/mine")

    # ---------- 管理员 ----------
    def admin_dashboard(self) -> dict[str, Any]:
        return self._request("GET", "/api/admin/dashboard")

    def cleared_jobs(self) -> dict[str, Any]:
        """当前用户已清空的职位（可申请恢复）。"""
        return self._request("GET", "/api/jobs/cleared")

    def create_restore_request(self, job_ids: list[int], note: str = "") -> dict[str, Any]:
        return self._request("POST", "/api/jobs/restore-request", json={"job_ids": job_ids, "note": note})

    def admin_users(self, status: str = "") -> list[dict[str, Any]]:
        params = {"status": status} if status else None
        return self._request("GET", "/api/admin/users", params=params)

    def approve_user(self, user_id: int, note: str = "") -> dict[str, Any]:
        return self._request("POST", f"/api/admin/users/{user_id}/approve", json={"note": note})

    def reject_user(self, user_id: int, note: str = "") -> dict[str, Any]:
        return self._request("POST", f"/api/admin/users/{user_id}/reject", json={"note": note})

    def admin_user_captures(self, user_id: int) -> dict[str, Any]:
        return self._request("GET", f"/api/admin/users/{user_id}/captures")

    def admin_restore_jobs(self, user_id: int, job_ids: list[int]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/admin/users/{user_id}/restore-jobs",
            json={"job_ids": job_ids},
        )

    def admin_restore_requests(self, status: str = "pending") -> dict[str, Any]:
        return self._request("GET", "/api/admin/restore-requests", params={"status": status})

    def approve_restore_request(self, request_id: int) -> dict[str, Any]:
        return self._request("POST", f"/api/admin/restore-requests/{request_id}/approve")

    def reject_restore_request(self, request_id: int, note: str = "") -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/admin/restore-requests/{request_id}/reject",
            json={"note": note},
        )

    def admin_reset_requests(self, status: str = "pending") -> list[dict[str, Any]]:
        return self._request("GET", "/api/admin/password-resets", params={"status": status})

    def approve_reset(self, request_id: int) -> dict[str, Any]:
        return self._request("POST", f"/api/admin/password-resets/{request_id}/approve")

    def reject_reset(self, request_id: int, note: str = "") -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/admin/password-resets/{request_id}/reject",
            json={"note": note},
        )

    def admin_feedback(self, status: str = "") -> dict[str, Any]:
        params = {"status": status} if status else None
        return self._request("GET", "/api/admin/feedback", params=params)

    def reply_feedback(self, feedback_id: int, level: str, reply: str) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/api/admin/feedback/{feedback_id}",
            json={"level": level, "reply": reply},
        )

    def admin_system(self) -> dict[str, Any]:
        return self._request("GET", "/api/admin/system")

    def admin_audit(self, limit: int = 50) -> dict[str, Any]:
        return self._request("GET", "/api/admin/audit", params={"limit": limit})

    def admin_jobs(self, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        return self._request("GET", "/api/admin/jobs", params={"offset": offset, "limit": limit})

    def admin_rollback_resume(self, resume_id: int) -> dict[str, Any]:
        return self._request("POST", f"/api/admin/resumes/{resume_id}/activate")

    def admin_reindex(self) -> dict[str, Any]:
        return self._request("POST", "/api/admin/reindex", timeout=30)

    def admin_export_bytes(self) -> tuple[bytes, str]:
        response = self.session.get(
            f"{self.base_url}/api/admin/export",
            headers=self._headers(),
            timeout=180,
        )
        if response.status_code >= 400:
            raise ApiError(response.status_code, _detail_from_response(response))
        disposition = response.headers.get("content-disposition", "")
        filename = "careerzone_export.zip"
        if "filename=" in disposition:
            filename = disposition.split("filename=")[-1].strip('"')
        return response.content, filename

    def get_security_question(self) -> dict[str, Any]:
        return self._request("GET", "/api/admin/security-question")

    def update_security_question(self, current_password: str, question: str, answer: str) -> dict[str, Any]:
        return self._request(
            "PUT",
            "/api/admin/security-question",
            json={"current_password": current_password, "question": question, "answer": answer},
        )
