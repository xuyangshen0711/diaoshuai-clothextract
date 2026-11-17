"""Safe, actionable AI errors; never show provider bodies or credentials."""
from __future__ import annotations


class TaggingError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.user_message = message
        if status_code is not None:
            self.status_code = status_code


def provider_error(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    if status == 402 or getattr(exc, "code", None) == "insufficient_quota":
        return "AI 账户余额不足或额度已用完，需要充值 AI 或补充额度后重试。已完成的结果会保留。"
    if status == 401:
        return "API 密钥无效，请更新服务配置后重试。"
    if status == 403:
        return "AI 服务拒绝访问，请检查密钥权限或账号状态。"
    if status == 429:
        return "AI 请求被限流，请稍后重试。"
    if "Timeout" in type(exc).__name__:
        return "AI 请求超时，请稍后重试。"
    if "Connection" in type(exc).__name__:
        return "无法连接 AI 服务，请检查网络后重试。"
    if isinstance(status, int) and status >= 500:
        return "AI 服务暂时不可用，请稍后重试。"
    return "AI 未能完成本次请求，请检查模型配置或稍后重试。"
