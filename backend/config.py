# ============================================================
# 配置层：所有环境差异都从这里进入，业务代码不直接读 os.environ。
# 这样做的原因：测试可以换数据库、生产可以换 MySQL，而不用改业务代码。
# ============================================================
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录 = backend/ 的上一级。用绝对路径，避免从不同工作目录启动时找不到 storage。
ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """应用配置。

    pydantic-settings 的优先级：环境变量 > .env > 字段默认值。
    因此 CI/测试可以临时覆盖 DATABASE_URL，而不会改坏本机 .env。
    """

    # ---------- 基础信息 ----------
    app_name: str = "CareerZone X"
    app_env: str = "development"  # development / test / production
    app_host: str = "127.0.0.1"  # 只绑本机，默认不对公网暴露
    app_port: int = 8000
    app_secret_key: str = "dev-only-change-me"
    api_base_url: str = "http://127.0.0.1:8000"

    # ---------- 数据库 ----------
    # 默认 MySQL 8；没有 MySQL 时可在 .env 改成 sqlite:///./storage/careerzone.db。
    database_url: str = "sqlite:///./storage/careerzone.db"
    db_echo: bool = False  # 打开会打印 SQL，仅排障时用，避免把参数写进日志

    # ---------- DeepSeek / LLM ----------
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    llm_timeout_seconds: int = 90
    llm_max_retries: int = 2
    llm_enabled: bool = True

    # ---------- 向量检索 ----------
    vector_enabled: bool = True
    # 默认不加载 bge：本机安全策略可能拦截 torch/regex DLL，导入会卡住/失败。
    # 需要语义模型时设 VECTOR_TRY_MODEL=true，并确认模型能正常 import。
    vector_try_model: bool = False
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    chroma_dir: str = "storage/chroma"

    # ---------- 管理员初始凭据 ----------
    # 这些值只在第一次建库时使用；之后数据库里只有 Argon2 哈希。
    admin_username: str = "Shenk"
    admin_initial_password: str = ""
    admin_security_question: str = "CareerZone X 安装时生成的 8 位管理员暗号是什么？"
    admin_initial_answer: str = ""

    # ---------- 来源/限流/上传 ----------
    allowed_origins: str = (
        "http://127.0.0.1:8501,http://localhost:8501,"
        "http://127.0.0.1:8000,http://localhost:8000"
    )
    max_resume_mb: int = 10
    max_avatar_mb: int = 2
    login_max_attempts: int = 5
    login_lock_minutes: int = 15
    session_hours_user: int = 12
    session_hours_admin: int = 4
    import_max_jobs: int = 1000
    # 本机自用可以关闭令牌校验；公网部署必须设 true。
    import_token_required: bool = False
    # 公网部署建议 true：扩展必须带登录会话；本机可 false 兼容旧扩展。
    import_require_session: bool = False
    # ---------- 官方 API 适配器（未来接入） ----------
    # 拿到平台官方授权后填这里；不配置时接口明确返回 503，不会偷偷爬。
    official_api_enabled: bool = False
    official_api_base_url: str = ""
    official_api_key: str = ""
    official_api_timeout: int = 30
    rate_limit_enabled: bool = True
    docs_enabled: bool = True

    # ---------- 费用估算（仅展示，不是账单） ----------
    price_input_per_m: float = 0.0
    price_output_per_m: float = 0.0

    # .env 固定在项目根目录；extra="ignore" 允许 .env 里存在未使用变量。
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("app_env")
    @classmethod
    def _check_env(cls, value: str) -> str:
        # 环境名只允许三个值，避免写成 "prod" 后安全开关判断失效。
        allowed = {"development", "test", "production"}
        value = value.strip().lower()
        if value not in allowed:
            raise ValueError(f"APP_ENV 必须是 {sorted(allowed)} 之一")
        return value

    @property
    def is_production(self) -> bool:
        """生产环境要关掉 /docs 并强制检查默认密钥。"""
        return self.app_env == "production"

    @property
    def storage_dir(self) -> Path:
        # 所有可变数据统一放 storage，备份/迁移时只搬这一个目录。
        return ROOT_DIR / "storage"

    @property
    def uploads_dir(self) -> Path:
        return self.storage_dir / "uploads"

    @property
    def resume_dir(self) -> Path:
        return self.uploads_dir / "resumes"

    @property
    def avatar_dir(self) -> Path:
        return self.uploads_dir / "avatars"

    @property
    def export_dir(self) -> Path:
        return self.storage_dir / "exports"

    @property
    def log_dir(self) -> Path:
        return self.storage_dir / "logs"

    @property
    def chroma_path(self) -> Path:
        # 相对路径按项目根解析，避免从 scripts/ 启动时写到奇怪的位置。
        path = Path(self.chroma_dir)
        return path if path.is_absolute() else ROOT_DIR / path

    @property
    def origins_list(self) -> list[str]:
        # 逗号分隔字符串转列表，并去掉空项，防止 CORS 配置里出现空 origin。
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]

    @property
    def max_resume_bytes(self) -> int:
        return self.max_resume_mb * 1024 * 1024

    @property
    def max_avatar_bytes(self) -> int:
        return self.max_avatar_mb * 1024 * 1024

    def ensure_dirs(self) -> None:
        """启动时一次建好目录，后续写文件就不用到处 try/except。"""
        for path in (
            self.storage_dir,
            self.resume_dir,
            self.avatar_dir,
            self.export_dir,
            self.log_dir,
            self.chroma_path,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """缓存配置对象：避免每个请求都重新解析 .env。"""
    settings = Settings()
    settings.ensure_dirs()
    return settings
