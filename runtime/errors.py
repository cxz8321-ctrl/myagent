# -*- coding: utf-8 -*-
"""统一错误模型（M5）：所有错误归三类，且都有补救路径。

三类（规格书 M5）：
    retryable    可重试：超时 / 限流 / 服务暂不可用 —— 稍后重试或退避重试
    fatal       不可重试：鉴权失败 / 请求非法 / 工具参数错 / 未知系统错 —— 修了再跑
    needs_human 需人工：权限被拒 / 需要审批 / 密钥缺失 —— 交人处理

统一异常类型 `AgentError`；所有出错都经 `classify()` / `from_exception()` 归一成
可 JSON 序列化的字典（code / message / category / retryable / remediation / detail），
走同一条上报路径（事件总线 error 事件 + 结构化日志 + trace 落盘）。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

# ---- 类别 ----
RETRASSISTANTBLE = "retryable"
FATAL = "fatal"
NEEDS_HUMAN = "needs_human"
CATEGORIES = (RETRASSISTANTBLE, FATAL, NEEDS_HUMAN)

# ---- 错误码 -> 类别（集中一处，新增错误码在这里登记）----
_CODE_CATEGORY: Dict[str, str] = {
    # provider
    "MODEL_TIMEOUT": RETRASSISTANTBLE,
    "MODEL_RATE_LIMIT": RETRASSISTANTBLE,
    "MODEL_UNAVAILABLE": RETRASSISTANTBLE,
    "MODEL_INTERNAL": RETRASSISTANTBLE,
    "MODEL_AUTH": NEEDS_HUMAN,          # 密钥不对/欠费，得人去换
    "MODEL_BAD_REQUEST": FATAL,
    # tool
    "TOOL_TIMEOUT": RETRASSISTANTBLE,
    "TOOL_FAILED": FATAL,
    "TOOL_BAD_ARGS": FATAL,
    "TOOL_NOT_FOUND": FATAL,
    "TOOL_DENIED": NEEDS_HUMAN,         # 权限/审批：交人决定
    # system
    "SYS_LOOP_GUARD": FATAL,
    "SYS_CANCELLED": FATAL,             # 中断不是"错"，也算终结性
    "SYS_INTERNAL": FATAL,
    "SYS_BAD_CONFIG": NEEDS_HUMAN,
    "SYS_NEEDS_APPROVAL": NEEDS_HUMAN,
}

# 兜底关键词：错误码没登记时按文案/字段猜
_NEED_HUMAN_HINTS = ("approval", "permission", "denied", "auth", "unauthorized",
                     "forbidden", "api_key", "apikey", "secret", "审批", "权限", "密钥")
_RETRASSISTANTBLE_HINTS = ("timeout", "timed out", "rate limit", "too many requests",
                    "unavailable", "connection", "temporarily", "超时", "限流")


def category_for(code: str, message: str = "", retryable: Optional[bool] = None) -> str:
    """错误码 -> 类别；码没登记时用关键词兜底。"""
    code = str(code or "")
    if code in _CODE_CATEGORY:
        return _CODE_CATEGORY[code]
    text = ("%s %s" % (code, message or "")).lower()
    if any(h in text for h in _NEED_HUMAN_HINTS):
        return NEEDS_HUMAN
    if any(h in text for h in _RETRASSISTANTBLE_HINTS):
        return RETRASSISTANTBLE
    if retryable is True:
        return RETRASSISTANTBLE
    return FATAL


_REMEDIATION = {
    RETRASSISTANTBLE: "可重试：退避后重试；连续失败就换 provider 或检查网络/服务状态。",
    FATAL: "不可重试：修正请求参数、工具实现或代码后重跑；必要时回看这次 trace。",
    NEEDS_HUMAN: "需人工：请人审批/授权，或补齐密钥、检查权限与审批口径（/approvals）。",
}


def remediation_for(category: str) -> str:
    return _REMEDIATION.get(category, _REMEDIATION[FATAL])


def classify(code: str, message: str = "", detail: Any = None,
             retryable: Optional[bool] = None) -> Dict[str, Any]:
    """把任意错误归一成统一字典（可 JSON 序列化、不抛）。"""
    cat = category_for(code, message, retryable)
    if retryable is None:
        retryable = cat == RETRASSISTANTBLE
    return {
        "code": str(code or "SYS_INTERNAL"),
        "message": str(message or ""),
        "category": cat,
        "retryable": bool(retryable),
        "remediation": remediation_for(cat),
        "detail": detail,
    }


class AgentError(Exception):
    """统一异常：内核里所有"出错"都用它包裹，出口只报数据不抛异常。"""

    def __init__(self, code: str, message: str = "", detail: Any = None,
                 category: Optional[str] = None, retryable: Optional[bool] = None,
                 cause: Optional[BaseException] = None):
        self.code = str(code)
        self.message = str(message)
        self.detail = detail
        self.cause = cause
        if category and category in CATEGORIES:
            self.category = category
            self.retryable = bool(retryable) if retryable is not None else category == RETRASSISTANTBLE
        else:
            got = classify(code, message, detail, retryable)
            self.category = got["category"]
            self.retryable = got["retryable"]
        super().__init__("%s: %s" % (self.code, self.message) if self.message else self.code)

    def to_dict(self) -> Dict[str, Any]:
        d = classify(self.code, self.message, self.detail,
                     retryable=(self.retryable if self.category == RETRASSISTANTBLE else None))
        d["category"] = self.category
        d["retryable"] = self.retryable
        d["remediation"] = remediation_for(self.category)
        if self.cause is not None:
            d["cause"] = "%s: %s" % (self.cause.__class__.__name__, self.cause)
        return d


def from_exception(exc: BaseException) -> Dict[str, Any]:
    """把任意异常归一成统一错误字典（含补救路径）。"""
    if isinstance(exc, AgentError):
        return exc.to_dict()
    code = "SYS_INTERNAL"
    text = "%s: %s" % (exc.__class__.__name__, exc)
    name = exc.__class__.__name__.lower()
    if "timeout" in name:
        code = "MODEL_TIMEOUT" if "model" in name else "TOOL_TIMEOUT"
    elif "auth" in name or "permission" in name:
        code = "MODEL_AUTH"
    d = classify(code, text, detail={"exception": exc.__class__.__name__})
    d["cause"] = text
    return d


def is_retryable(error: Any) -> bool:
    if isinstance(error, AgentError):
        return error.category == RETRASSISTANTBLE
    if isinstance(error, dict):
        return error.get("category") == RETRASSISTANTBLE or error.get("retryable") is True
    return False


def retry_call(fn: Callable[[], Any], attempts: int = 3, base_delay: float = 0.0,
               sleep: Optional[Callable[[float], None]] = None,
               on_error: Optional[Callable[[Dict[str, Any], int], None]] = None) -> Any:
    """按错误类别重试：可重试才退避重试，不可重试立即透出。

    `fn` 成功即返回值；失败时抛出的异常经 from_exception 归类后再决定是否重试。
    返回最后一次结果/异常（包装成 AgentError 抛出，由调用方决定）。
    """
    import time as _time
    slp = sleep or _time.sleep
    last: Optional[Dict[str, Any]] = None
    for i in range(1, max(1, int(attempts)) + 1):
        try:
            return fn()
        except BaseException as e:  # noqa: BLE001
            last = from_exception(e)
            if on_error:
                try:
                    on_error(last, i)
                except Exception:  # noqa: BLE001
                    pass
            if not is_retryable(last) or i >= attempts:
                raise
            if base_delay > 0:
                slp(base_delay * (2 ** (i - 1)))
    raise AgentError(last["code"], last["message"]) if last else AgentError("SYS_INTERNAL", "retry_call")
