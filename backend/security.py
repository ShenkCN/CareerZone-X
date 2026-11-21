# ============================================================
# 安全工具：密码、会话、管理员二次验证、一次性短码。
# 关键原则：任何“能登录”的凭据都只存哈希；日志/接口永不回显明文。
# ============================================================
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from backend.config import Settings

# Argon2id 是当前密码哈希的推荐算法；64MB 内存成本在本机可接受，
# 比 bcrypt 更能抵抗 GPU 暴力破解。
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)

USERNAME_RE = re.compile(r"^[A-Za-z0-9]{5,15}$")
# 管理员密保答案做归一化后再哈希：忽略大小写、首尾空格和连续空格，
# 避免用户明明答对却因为多打一个空格被锁在门外。
_ANSWER_WS_RE = re.compile(r"\s+")


def utcnow() -> datetime:
    """安全模块独立取 UTC，避免 import models 造成循环依赖。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_password(password: str) -> str:
    """生成 Argon2id 哈希；每次盐随机，所以同一密码两次哈希也不同。"""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码；哈希损坏/不匹配都返回 False，不向外泄露具体原因。"""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def validate_username(username: str) -> str:
    """校验登录账号：5—15 位，只允许字母/数字；纯数字或纯字母都可以。

    不允许符号是为了避免前后端/扩展对 @、#、空格的处理不一致；
    不再强制包含字母，满足用户“纯数字账号”的要求。
    """
    value = (username or "").strip()
    if not USERNAME_RE.fullmatch(value):
        raise ValueError("账号必须是 5—15 位字母或数字，不能含符号")
    return value


def validate_password(password: str) -> str:
    """密码策略：6—20 位；纯数字/纯字母/混合都可以。

    用户明确要求放宽，不再强制大小写或数字组合。
    仍然拒绝空格/中文/emoji，避免复制和编码歧义；安全强度会降低，
    本机自用可接受，公网部署建议恢复更严格策略 + 登录 2FA。
    """
    value = password or ""
    if not 6 <= len(value) <= 20:
        raise ValueError("密码长度必须为 6—20 位")
    if any(ch.isspace() for ch in value):
        raise ValueError("密码不能包含空格（避免复制时首尾空格丢失）")
    # 符号/字母都不强制；但只接受常见 ASCII，避免中文/emoji 编码问题。
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
        raise ValueError("密码只能使用英文字母、数字和常见英文符号")
    return value


def sha256_text(text: str) -> str:
    """通用 SHA-256：用于会话令牌、短码、指纹等“不需要可逆”的场景。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_session_token() -> tuple[str, str]:
    """返回 (给客户端的明文令牌, 数据库存的哈希)。

    明文只在这个函数返回时存在一次；之后客户端每次请求带 Bearer，
    服务端只拿哈希查库，数据库泄露也无法直接登录。
    """
    raw = secrets.token_urlsafe(32)
    return raw, sha256_text(raw)


def new_import_key() -> tuple[str, str, str]:
    """生成浏览器扩展采集令牌，返回 (明文, 哈希, 提示片段)。"""
    raw = "czx_" + secrets.token_urlsafe(24)
    return raw, sha256_text(raw), raw[:12] + "..."


def normalize_security_answer(answer: str) -> str:
    """密保答案归一化：去首尾空格、压缩连续空白、转小写。"""
    return _ANSWER_WS_RE.sub(" ", (answer or "").strip()).lower()


def make_admin_challenge(settings: Settings, user_id: int) -> str:
    """签发 5 分钟有效的管理员二次验证令牌。

    为什么不用数据库：挑战只活 5 分钟，签名令牌无状态、重启不丢，
    也少一张容易忘记清理的表。
    """
    serializer = URLSafeTimedSerializer(settings.app_secret_key, salt="careerzone-admin-2fa")
    return serializer.dumps({"uid": user_id, "purpose": "admin_2fa"})


def read_admin_challenge(settings: Settings, token: str, max_age: int = 300) -> Optional[int]:
    """校验二次验证令牌，成功返回 user_id；过期/篡改返回 None。"""
    serializer = URLSafeTimedSerializer(settings.app_secret_key, salt="careerzone-admin-2fa")
    try:
        data = serializer.loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("purpose") != "admin_2fa":
        return None
    uid = data.get("uid")
    return int(uid) if isinstance(uid, int) or str(uid).isdigit() else None


def new_reset_code() -> tuple[str, str, datetime]:
    """生成忘记密码一次性短码。

    用去掉易混淆字符的字母表，方便用户手输；短码 30 分钟过期，
    管理员批准后才生成，用户端无法自己触发重置。
    """
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(10))
    return raw, sha256_text(raw), utcnow() + timedelta(minutes=30)


def new_avatar_filename(extension: str) -> str:
    """头像文件名随机化，避免用原文件名造成路径穿越/覆盖。"""
    return f"{secrets.token_hex(16)}.{extension.lstrip('.').lower()}"


def new_resume_filename(extension: str) -> str:
    """简历文件名随机化；原始文件名另存数据库用于展示。"""
    return f"{secrets.token_hex(16)}.{extension.lstrip('.').lower()}"


def password_strength_label(password: str) -> str:
    """给前端一个轻量强度提示；真正的规则仍以 validate_password 为准。"""
    score = 0
    if len(password) >= 8:
        score += 1
    if len(password) >= 12:
        score += 1
    if re.search(r"[a-z]", password) and re.search(r"[A-Z]", password):
        score += 1
    if re.search(r"\d", password):
        score += 1
    if re.search(r"[^A-Za-z0-9]", password):
        score += 1
    return {0: "很弱", 1: "弱", 2: "一般", 3: "较强", 4: "强", 5: "很强"}.get(score, "很强")


def mask_token(raw: str) -> str:
    """只展示令牌前 8 位，日志/界面都不出现完整值。"""
    return raw[:8] + "..." if len(raw) > 8 else "***"
