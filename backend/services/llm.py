# ============================================================
# DeepSeek 客户端：直接走 OpenAI 兼容的 HTTP 接口。
# 为什么不用 openai SDK：
# 本机 Windows 的应用控制策略拦截了 SDK 依赖的 jiter DLL，
# 用 requests 直接 POST 更少依赖、更好排障，也完全兼容 DeepSeek。
# 关键点：JSON 输出、超时、重试、用量记录、绝不回显 Key。
# ============================================================
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

from backend.config import Settings, get_settings
from backend.services.usage_service import record_usage

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """LLM 不可用/返回不可解析时的统一异常，路由层可安全展示其中的中文信息。"""

    def __init__(self, message: str, code: str = "llm_error"):
        super().__init__(message)
        self.code = code


@dataclass
class LLMResult:
    """一次调用的结果与账本字段。"""

    data: Any
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def extract_json(text: str) -> Any:
    """从模型输出中提取 JSON。

    模型经常在 JSON 外包 Markdown 代码块，或前后加解释；
    先剥代码块，再从第一个 {/[ 到最后一个 }/] 截取。
    """
    raw = (text or "").strip()
    raw = _FENCE_RE.sub("", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    starts = [idx for idx in (raw.find("{"), raw.find("[")) if idx >= 0]
    if not starts:
        raise LLMError("模型没有返回 JSON", "invalid_json")
    start = min(starts)
    end = max(raw.rfind("}"), raw.rfind("]"))
    if end <= start:
        raise LLMError("模型返回的 JSON 不完整", "invalid_json")
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"模型返回的 JSON 无法解析：{exc.msg}", "invalid_json") from exc


class LLMService:
    """对 DeepSeek /chat/completions 的薄封装，业务层只关心 chat_json。"""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.model = self.settings.deepseek_model
        self._session: Optional[requests.Session] = None

    @property
    def available(self) -> bool:
        """有 Key 且未显式关闭时才可用。"""
        return bool(self.settings.llm_enabled and self.settings.deepseek_api_key.strip())

    def _get_session(self) -> requests.Session:
        """复用 TCP 连接；不把 Key 放在 URL 里。"""
        if not self.available:
            raise LLMError("未配置 DeepSeek API Key，AI 功能暂不可用", "not_configured")
        if self._session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "Authorization": f"Bearer {self.settings.deepseek_api_key.strip()}",
                    "Content-Type": "application/json",
                }
            )
            self._session = session
        return self._session

    def chat_json(
        self,
        *,
        system: str,
        user: str,
        endpoint: str,
        user_id: Optional[int] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> LLMResult:
        """调用 DeepSeek 并返回解析后的 JSON。

        重试只针对网络/超时/限流/5xx；401/400 立即失败，
        避免把“Key 错了”重试三次，白白拖慢界面。
        """
        session = self._get_session()
        url = f"{self.settings.deepseek_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        last_error: Optional[Exception] = None

        for attempt in range(self.settings.llm_max_retries + 1):
            started = time.perf_counter()
            try:
                response = session.post(
                    url,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=(10, self.settings.llm_timeout_seconds),
                )
                latency_ms = int((time.perf_counter() - started) * 1000)

                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.settings.llm_max_retries:
                        logger.warning("DeepSeek HTTP %s，准备重试", response.status_code)
                        time.sleep(min(8.0, 1.5 * (2 ** attempt)))
                        continue
                if response.status_code >= 400:
                    code = f"http_{response.status_code}"
                    record_usage(
                        user_id=user_id,
                        model=self.model,
                        endpoint=endpoint,
                        latency_ms=latency_ms,
                        success=False,
                        error_code=code,
                    )
                    # 只从响应里提取简短错误，绝不回显 Authorization 头。
                    try:
                        detail = response.json().get("error", {})
                        message = detail.get("message") if isinstance(detail, dict) else ""
                    except ValueError:
                        message = ""
                    logger.error("DeepSeek HTTP %s：%s", response.status_code, (message or "")[:200])
                    raise LLMError(
                        f"DeepSeek 接口错误（HTTP {response.status_code}），请检查 Key/额度",
                        code,
                    )

                body = response.json()
                choices = body.get("choices") or []
                if not choices:
                    raise LLMError("DeepSeek 返回空 choices", "empty_response")
                content = (choices[0].get("message") or {}).get("content") or ""
                usage = body.get("usage") or {}
                prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                completion_tokens = int(usage.get("completion_tokens", 0) or 0)
                total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)
                data = extract_json(content)

                record_usage(
                    user_id=user_id,
                    model=body.get("model") or self.model,
                    endpoint=endpoint,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    latency_ms=latency_ms,
                    success=True,
                )
                return LLMResult(
                    data=data,
                    content=content,
                    model=body.get("model") or self.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    latency_ms=latency_ms,
                )
            except requests.Timeout as exc:
                last_error = exc
                logger.warning("DeepSeek 超时（第 %s 次）", attempt + 1)
                if attempt < self.settings.llm_max_retries:
                    time.sleep(min(8.0, 1.5 * (2 ** attempt)))
                    continue
                record_usage(
                    user_id=user_id,
                    model=self.model,
                    endpoint=endpoint,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=False,
                    error_code="timeout",
                )
                raise LLMError("DeepSeek 超时，请稍后重试", "timeout") from exc
            except requests.ConnectionError as exc:
                last_error = exc
                logger.warning("DeepSeek 连接失败（第 %s 次）", attempt + 1)
                if attempt < self.settings.llm_max_retries:
                    time.sleep(min(8.0, 1.5 * (2 ** attempt)))
                    continue
                record_usage(
                    user_id=user_id,
                    model=self.model,
                    endpoint=endpoint,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=False,
                    error_code="connection",
                )
                raise LLMError("DeepSeek 连接失败，请检查网络", "connection") from exc
            except LLMError as exc:
                # JSON 解析失败：记失败用量后原样抛出，不再重试（多数是输出被截断）。
                record_usage(
                    user_id=user_id,
                    model=self.model,
                    endpoint=endpoint,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=False,
                    error_code=exc.code,
                )
                raise
            except ValueError as exc:
                record_usage(
                    user_id=user_id,
                    model=self.model,
                    endpoint=endpoint,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=False,
                    error_code="bad_response",
                )
                raise LLMError("DeepSeek 返回了无法解析的响应", "bad_response") from exc
            except Exception as exc:  # noqa: BLE001 - 兜底，避免未知异常打穿 500 页面
                last_error = exc
                logger.exception("LLM 未知异常")
                record_usage(
                    user_id=user_id,
                    model=self.model,
                    endpoint=endpoint,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=False,
                    error_code="unknown",
                )
                raise LLMError("DeepSeek 调用失败，请查看 API 日志", "unknown") from exc
        raise LLMError("DeepSeek 调用失败", "unknown") from last_error
