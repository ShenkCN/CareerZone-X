# ============================================================
# 简历服务：安全落盘、PDF/Word 文本解析、AI 结构化、规则兜底。
# 设计原则：
# 1) 原文件必须保存，数据库只存相对路径，方便回档/下载；
# 2) 上传先校验大小/扩展名/文件头，不能只信浏览器 MIME；
# 3) AI 解析失败不丢文件，返回规则抽取结果并标注 warning。
# ============================================================
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Optional

from docx import Document
from fastapi import UploadFile
from pypdf import PdfReader

from backend.config import Settings
from backend.security import new_resume_filename
from backend.services.llm import LLMError, LLMService
from backend.services.prompts import render_prompt
from backend.services.text import clean_text

logger = logging.getLogger(__name__)

ALLOWED_RESUME_EXTENSIONS = {".pdf", ".docx"}
MAX_TEXT_CHARS = 30000


class ResumeError(ValueError):
    """简历校验/解析的可展示错误。"""


def _safe_original_name(name: str) -> str:
    """只保留文件名本身，去掉路径分隔符和控制字符，防止路径穿越与日志注入。"""
    base = Path(name or "resume").name
    base = re.sub(r"[\x00-\x1f]", "", base).strip()
    return base[:255] or "resume"


async def save_upload(
    upload: UploadFile,
    user_id: int,
    settings: Settings,
) -> tuple[Path, str, int, str]:
    """分块保存上传文件，返回 (绝对路径, 相对路径, 大小, sha256)。

    分块而不是 upload.read() 的原因：大文件一次读进内存会拖垮单进程服务；
    同时边写边算哈希，避免写完再读一遍。
    """
    original = _safe_original_name(upload.filename or "")
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_RESUME_EXTENSIONS:
        raise ResumeError("仅支持 .pdf 或 .docx 文件；.doc 请先另存为 .docx")

    user_dir = settings.resume_dir / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    stored = user_dir / new_resume_filename(suffix)
    hasher = hashlib.sha256()
    size = 0
    first_chunk = b""

    try:
        with stored.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                if not first_chunk:
                    first_chunk = chunk[:8]
                size += len(chunk)
                if size > settings.max_resume_bytes:
                    raise ResumeError(f"文件超过 {settings.max_resume_mb}MB 限制")
                hasher.update(chunk)
                handle.write(chunk)
    except Exception:
        # 任何失败都删掉半截文件，避免垃圾文件占空间。
        stored.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()

    if size == 0:
        stored.unlink(missing_ok=True)
        raise ResumeError("文件为空")
    if suffix == ".pdf" and not first_chunk.startswith(b"%PDF"):
        stored.unlink(missing_ok=True)
        raise ResumeError("文件头不是 PDF，已拒绝")
    if suffix == ".docx" and not first_chunk.startswith(b"PK"):
        stored.unlink(missing_ok=True)
        raise ResumeError("文件头不是 DOCX，已拒绝")

    relative = stored.relative_to(settings.storage_dir).as_posix()
    return stored, relative, size, hasher.hexdigest()


def resolve_stored_path(settings: Settings, relative: str) -> Path:
    """把数据库里的相对路径还原为绝对路径，并确保仍在 storage 内。"""
    candidate = (settings.storage_dir / relative).resolve()
    storage = settings.storage_dir.resolve()
    if storage not in candidate.parents and candidate != storage:
        raise ResumeError("文件路径非法")
    return candidate


def extract_text(path: Path) -> str:
    """按扩展名解析 PDF/DOCX；解析失败抛 ResumeError，由路由决定是否仍保存。"""
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            reader = PdfReader(str(path))
            pages = []
            for page in reader.pages[:60]:  # 60 页足够覆盖简历，防止恶意超大 PDF
                pages.append(page.extract_text() or "")
            text = "\n".join(pages)
        elif suffix == ".docx":
            document = Document(str(path))
            parts = [p.text for p in document.paragraphs]
            # 表格里常放教育/技能信息，不能漏；逐行拼接。
            for table in document.tables:
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if cells:
                        parts.append(" | ".join(cells))
            text = "\n".join(parts)
        else:
            raise ResumeError("不支持的简历格式")
    except ResumeError:
        raise
    except Exception as exc:
        logger.warning("简历解析失败：%s", exc.__class__.__name__)
        raise ResumeError("简历无法解析，可能文件损坏或加密") from exc

    text = clean_text(text, MAX_TEXT_CHARS)
    if len(text) < 20:
        raise ResumeError("简历中没有提取到足够文字；扫描版 PDF 需要先 OCR")
    return text


def fallback_extract(text: str) -> dict[str, Any]:
    """不调用模型时的规则抽取：只填最有把握的邮箱/电话/学历/技能。"""
    email_match = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text)
    phone_match = re.search(r"(?<![\d])(?:1[3-9]\d{9})(?![\d])", text)
    education = ""
    for value in ("博士", "硕士", "本科", "大专", "高中"):
        if value in text:
            education = value
            break
    cities = [city for city in ("苏州", "上海", "杭州", "南京", "无锡", "重庆", "广州", "深圳", "北京") if city in text]
    skill_pool = [
        "Python", "FastAPI", "Docker", "Linux", "MySQL", "RAG", "Chroma", "DeepSeek",
        "Streamlit", "Git", "Nginx", "Redis", "Java", "SpringBoot", "Vue", "SQL",
    ]
    skills = [skill for skill in skill_pool if skill.lower() in text.lower()]
    return {
        "real_name": "",
        "phone": phone_match.group(0) if phone_match else "",
        "email": email_match.group(0) if email_match else "",
        "city": cities[0] if cities else "",
        "target_cities": cities[:3],
        "target_roles": [],
        "job_type": "全职",
        "salary_min": None,
        "salary_max": None,
        "experience": "",
        "education": education,
        "skills": skills,
        "industries": [],
        "preferred_company_sizes": [],
        "bio": "",
        "github": "",
        "portfolio": "",
        "highest_education": education,
        "graduation_year": None,
        "years_of_experience": None,
    }


def _normalize_draft(data: dict[str, Any]) -> dict[str, Any]:
    """模型可能多返回字段/类型不对；这里只保留画像允许的键并做类型收敛。"""
    allowed = {
        "real_name", "phone", "email", "city", "target_cities", "target_roles", "job_type",
        "salary_min", "salary_max", "experience", "education", "skills", "industries",
        "preferred_company_sizes", "bio", "github", "portfolio", "highest_education",
        "graduation_year", "years_of_experience",
    }
    result: dict[str, Any] = {}
    for key in allowed:
        if key not in data:
            continue
        value = data[key]
        if key in {"target_cities", "target_roles", "skills", "industries", "preferred_company_sizes"}:
            if isinstance(value, list):
                result[key] = [str(item).strip()[:64] for item in value if str(item).strip()][:50]
            elif isinstance(value, str) and value.strip():
                result[key] = [value.strip()[:64]]
            else:
                result[key] = []
        elif key in {"salary_min", "salary_max", "graduation_year", "years_of_experience"}:
            try:
                result[key] = int(value) if value not in (None, "", "null") else None
            except (TypeError, ValueError):
                result[key] = None
        else:
            result[key] = clean_text(value, 2000) if isinstance(value, str) else ""
    return result


def parse_resume(
    text: str,
    *,
    llm: LLMService,
    user_id: Optional[int],
) -> tuple[dict[str, Any], str, str]:
    """返回 (画像草稿, 解析模式, 警告)。

    AI 失败时降级为规则抽取，但明确告诉用户“仅自动识别了部分字段”，
    不让用户误以为模型已经读过全文。
    """
    fallback = fallback_extract(text)
    if not llm.available:
        return fallback, "rules", "未配置 DeepSeek，当前仅用规则识别了邮箱/电话/学历/技能，请手动补齐。"
    try:
        prompt = render_prompt("resume_extract", RESUME=text[:24000])
        result = llm.chat_json(
            system="你是严谨的简历信息抽取器，只输出 JSON。",
            user=prompt,
            endpoint="resume_extract",
            user_id=user_id,
            temperature=0.1,
            max_tokens=2500,
        )
        if not isinstance(result.data, dict):
            raise LLMError("简历解析返回格式错误", "invalid_json")
        draft = _normalize_draft(result.data)
        # AI 没抽到的联系方式/技能，用规则结果补空，不覆盖 AI 结果。
        for key in ("phone", "email", "education", "skills", "city"):
            if not draft.get(key) and fallback.get(key):
                draft[key] = fallback[key]
        return draft, "deepseek", ""
    except LLMError as exc:
        logger.warning("AI 简历解析失败：%s", exc.code)
        return fallback, "rules", f"AI 解析暂不可用（{exc}），已用规则识别部分字段，请手动补齐。"
