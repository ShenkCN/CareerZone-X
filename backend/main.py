# ============================================================
# FastAPI 入口：只做“组装”（中间件/路由/异常/健康检查），
# 业务逻辑全部在 routers/services，避免 main.py 变成屎山。
# ============================================================
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.bootstrap import bootstrap
from backend.config import get_settings
from backend.database import engine
from backend.rate_limit import BodySizeLimitMiddleware, RateLimitMiddleware, SecurityHeadersMiddleware
from backend.routers import admin, auth, extension, feedback, jobs, profile, resumes
from backend.services.llm import LLMService
from backend.services.vector_store import get_vector_store

settings = get_settings()


def _configure_logging() -> None:
    """控制台 + 轮转文件；日志里绝不打印 API Key/密码。"""
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        file_handler = RotatingFileHandler(
            settings.log_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


_configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建表/管理员；关闭时释放连接池。"""
    if settings.is_production and settings.app_secret_key.startswith("dev-"):
        raise RuntimeError("生产环境必须设置随机 APP_SECRET_KEY，不能使用默认值")
    if not settings.deepseek_api_key.strip():
        logger.warning("未配置 DEEPSEEK_API_KEY：AI 初筛/分析会降级为规则或不可用")
    try:
        bootstrap()
        logger.info("CareerZone X API 启动完成")
    except Exception:
        logger.exception("启动初始化失败")
        raise
    yield
    engine.dispose()


app = FastAPI(
    title="CareerZone X API",
    description="本地优先的个人 AI 求职工作台。职位数据来自用户授权采集，不提供自动爬取接口。",
    version="0.4.0",
    lifespan=lifespan,
    docs_url="/api/docs" if settings.docs_enabled else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if settings.docs_enabled else None,
)

# CORS：本机 Streamlit + 浏览器扩展。扩展的 Origin 是 chrome-extension:// 随机 ID，
# 所以用正则；同时不允许携带 Cookie（我们使用 Authorization 头），降低 CSRF 面。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list,
    allow_origin_regex=r"^(chrome-extension|moz-extension)://.*$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Import-Key"],
    expose_headers=["Content-Disposition"],
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(BodySizeLimitMiddleware, max_bytes=max(settings.max_resume_bytes + 2 * 1024 * 1024, 12 * 1024 * 1024))
app.add_middleware(RateLimitMiddleware, enabled=settings.rate_limit_enabled)

app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(resumes.router)
app.include_router(jobs.router)
app.include_router(feedback.router)
app.include_router(admin.router)
app.include_router(extension.router)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """参数错误只回必要信息，不把整段请求体（可能含密码）回显。"""
    errors = []
    for error in exc.errors()[:8]:
        errors.append({"loc": error.get("loc", []), "msg": error.get("msg", "参数错误")})
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "参数校验失败", "errors": errors},
    )


@app.exception_handler(IntegrityError)
async def integrity_exception_handler(request: Request, exc: IntegrityError):
    """唯一键冲突等数据库错误统一返回 409，不把 SQL 暴露给前端。"""
    logger.warning("数据库完整性冲突：%s", exc.__class__.__name__)
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": "数据冲突，请刷新后重试"})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """兜底 500：日志保留堆栈，客户端只看到通用错误。"""
    logger.exception("未处理异常：%s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误，请查看本地日志"})


@app.get("/health", tags=["系统"])
def health():
    """健康检查：数据库可查询、AI/向量是否配置，不返回任何密钥。

    数据库刚启动/连接池换节点时，第一次探活可能失败；
    重试一次并记录原因，避免页面把瞬时抖动误判为“整个服务挂了”。
    """
    db_ok = False
    last_error = ""
    for attempt in range(2):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            db_ok = True
            break
        except Exception as exc:  # noqa: BLE001 - 健康检查必须返回 JSON 而不是 500
            last_error = exc.__class__.__name__
            if attempt == 0:
                time.sleep(0.3)
    if not db_ok:
        logger.warning("健康检查数据库探活失败：%s", last_error)
    return {
        "status": "ok" if db_ok else "degraded",
        "app": settings.app_name,
        "version": "0.4.0",
        "database": db_ok,
        "db_backend": engine.url.get_backend_name(),
        "llm_configured": LLMService(settings).available,
        "vector_enabled": get_vector_store().configured,
        "environment": settings.app_env,
    }


@app.get("/", tags=["系统"])
def root():
    """根路径给排查用；真正的界面在 Streamlit 8501。"""
    return {
        "name": settings.app_name,
        "api_docs": "/api/docs" if settings.docs_enabled else "disabled",
        "health": "/health",
        "ui": "http://127.0.0.1:8501",
    }
