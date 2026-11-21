# ============================================================
# Prompt 加载：文件和代码分离，改提示词不用改业务逻辑。
# 版本号写进分析记录，出问题能追溯到是哪一版 Prompt。
# ============================================================
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT_VERSION = "2026-11-17.v1"


@lru_cache(maxsize=16)
def _load_raw(name: str) -> str:
    """读一次缓存一次；Prompt 是静态文件，不需要每个请求都读磁盘。"""
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt 不存在：{path}")
    return path.read_text(encoding="utf-8")


def _strip_leading_comments(text: str) -> str:
    """去掉文件顶部的说明注释，避免把“给维护者看的话”也发给模型。"""
    lines = text.splitlines()
    idx = 0
    while idx < len(lines) and (lines[idx].startswith("#") or not lines[idx].strip()):
        idx += 1
    return "\n".join(lines[idx:]).strip()


def render_prompt(name: str, **values: str) -> str:
    """用 {{KEY}} 占位符替换，避免 Python format 与 JSON 花括号冲突。"""
    text = _strip_leading_comments(_load_raw(name))
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text
