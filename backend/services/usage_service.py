# ============================================================
# LLM 用量账本：每次调用（含失败）都落库，管理员页才有真实数据。
# 用独立 Session 写入，避免主业务事务回滚时把“已经花掉的 token”也回滚掉。
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

from backend.config import get_settings
from backend.database import SessionLocal
from backend.models import ApiUsage

logger = logging.getLogger(__name__)


def record_usage(
    *,
    user_id: Optional[int],
    model: str,
    endpoint: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    latency_ms: int = 0,
    success: bool = True,
    error_code: str = "",
) -> None:
    """写一条用量记录；失败只记日志，绝不因为统计失败影响主业务。"""
    settings = get_settings()
    cost = (
        prompt_tokens / 1_000_000 * settings.price_input_per_m
        + completion_tokens / 1_000_000 * settings.price_output_per_m
    )
    db = SessionLocal()
    try:
        db.add(
            ApiUsage(
                user_id=user_id,
                provider="deepseek",
                model=(model or settings.deepseek_model)[:64],
                endpoint=endpoint[:64],
                prompt_tokens=max(0, int(prompt_tokens or 0)),
                completion_tokens=max(0, int(completion_tokens or 0)),
                total_tokens=max(0, int(total_tokens or 0)),
                cost_estimate=round(cost, 6),
                latency_ms=max(0, int(latency_ms or 0)),
                success=bool(success),
                error_code=(error_code or "")[:64],
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("写入 API 用量失败")
    finally:
        db.close()
