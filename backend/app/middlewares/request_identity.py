"""当前请求的身份(2026-11-17)。

用途:`llm_call_logger` 在很深的服务层里记用量,以前写死 `user_id="king"`
(注释解释了当时 ImportError 的来龙去脉),而配额中间件按 JWT 里的 sub 查同一行 ——
于是除 "king" 以外的用户 `cost_cents` 永不增长,**配额永远不会触发**。

做法:鉴权中间件验签后把用户名放进 ContextVar;日志侧优先读它,读不到(后台任务/
脚本)才回退到默认值,并在回执里标出来源。
"""

from __future__ import annotations

from contextvars import ContextVar

_DEFAULT_USER = "king"

_current_user: ContextVar[str | None] = ContextVar("current_request_user", default=None)


def set_current_user(value: str | None) -> None:
    _current_user.set(value or None)


def current_user() -> str | None:
    return _current_user.get()


def resolve_user_id(explicit: str | None = None) -> str:
    """请求身份 > ContextVar > 默认值(后台任务)。"""
    if explicit:
        return str(explicit)
    return current_user() or _DEFAULT_USER