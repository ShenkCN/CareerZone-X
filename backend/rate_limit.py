# ============================================================
# 轻量限流中间件：本机单进程足够，不引入 Redis。
# 目的不是防国家级 DDoS，而是防止：
# - 登录暴力破解；
# - 扩展/脚本误循环把 DeepSeek 额度刷爆；
# - 上传接口被大文件反复打。
# ============================================================
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from backend.deps import get_client_ip


class RateLimitMiddleware(BaseHTTPMiddleware):
    """固定窗口 + 双端队列记录时间戳，按 IP+规则分桶。"""

    def __init__(self, app, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    @staticmethod
    def _rule(path: str) -> tuple[int, int]:
        """返回 (窗口秒数, 最大次数)。路径越敏感，额度越小。"""
        if path.startswith("/api/auth/login") or path.startswith("/api/auth/admin/verify"):
            return 60, 8
        # 注册/找回是低频操作，但本机测试时也需要重试几次；
        # 20 次/10 分钟足够个人使用，又不会因输错一次就锁一小时。
        if path.startswith("/api/auth/nudge-approval"):
            return 600, 5
        if path.startswith("/api/auth/register"):
            return 600, 20
        if path.startswith("/api/auth/forgot-password"):
            return 600, 10
        if path.startswith("/api/jobs/import"):
            return 60, 12
        if path.startswith("/api/jobs/") and path.endswith("/analyze"):
            return 60, 10
        if path.startswith("/api/resumes"):
            return 3600, 30
        return 60, 180

    async def dispatch(self, request: Request, call_next):
        if not self.enabled or request.url.path == "/health":
            return await call_next(request)
        # 只限流“会改变状态/花钱”的请求。读取职位、简历列表、画像等 GET
        # 不参与限流：首页每次 rerun 都会读多个 GET，限流会让正常刷新也被锁 18 分钟。
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return await call_next(request)
        window, limit = self._rule(request.url.path)
        now = time.monotonic()
        key = f"{get_client_ip(request)}:{request.url.path}:{limit}"
        bucket = self._hits[key]
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= limit:
            retry = max(1, int(window - (now - bucket[0])))
            if retry >= 60:
                wait_text = f"约 {max(1, (retry + 59) // 60)} 分钟"
            else:
                wait_text = f"{retry} 秒"
            return JSONResponse(
                status_code=429,
                content={"detail": f"请求过于频繁，请{wait_text}后再试"},
                headers={"Retry-After": str(retry)},
            )
        bucket.append(now)
        # 简单清理：防止长时间运行后 key 无限增长。
        if len(self._hits) > 5000:
            for stale in [k for k, v in self._hits.items() if not v or now - v[-1] > 3600]:
                self._hits.pop(stale, None)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """给所有 API 响应加基础安全头；Swagger 页面单独放行 CSP。"""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if not request.url.path.startswith("/api/docs") and request.url.path != "/api/openapi.json":
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            )
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """根据 Content-Length 提前拒绝超大请求；分块上传没有长度时交给各接口上限。"""

    def __init__(self, app, max_bytes: int = 12 * 1024 * 1024):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > self.max_bytes:
            return JSONResponse(status_code=413, content={"detail": "请求体过大"})
        return await call_next(request)
