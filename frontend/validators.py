# ============================================================
# 前端预校验：只为了少一次往返、把规则说清楚；最终以后端为准。
# 注意：这里不引入第三方库，保持离线可跑。
# ============================================================
from __future__ import annotations


def password_error(password: str) -> str:
    """返回第一条不满足的密码规则；空字符串表示通过。"""
    if len(password) < 6 or len(password) > 20:
        return "密码长度必须是 6—20 位。"
    if any(ch.isspace() for ch in password):
        return "密码不能包含空格。"
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in password):
        return "密码只能使用英文字母、数字和常见英文符号。"
    return ""


def username_error(username: str) -> str:
    """账号规则与后端 validate_username 保持一致。"""
    value = (username or "").strip()
    if not 5 <= len(value) <= 15:
        return "账号必须是 5—15 位。"
    if not value.isalnum() or not value.isascii():
        return "账号只能使用字母和数字，不能含符号。"
    return ""
