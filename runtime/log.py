# -*- coding: utf-8 -*-
"""结构化日志（一行一条 JSON）+ trace_id 贯穿全链路。"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from typing import Any, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        d = {
            "ts": int(time.time() * 1000),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            d.update(extra)
        if getattr(record, "exc_info", None):
            d["exc"] = self.formatException(record.exc_info)
        try:
            from runtime.text import safe_dumps
            return safe_dumps(d)
        except Exception:  # noqa: BLE001 —— 日志格式化自己绝不能炸
            return '{"level": "%s", "msg": "<unprintable>"}' % record.levelname


def get_logger(name: str = "myagent", trace_id: Optional[str] = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(JsonFormatter())
        logger.addHandler(h)
        logger.setLevel(os.environ.get("MASSISTANTGENT_LOG_LEVEL", "INFO").upper())
        logger.propagate = False
    if trace_id:
        return logging.LoggerAdapter(logger, {"trace_id": trace_id})  # type: ignore[return-value]
    return logger


def log_event(logger: Any, msg: str, **fields: Any) -> None:
    """带结构化字段的日志；自动塞 trace_id。"""
    extra = dict(fields)
    tid = getattr(logger, "extra", {}).get("trace_id") if isinstance(logger, logging.LoggerAdapter) else None
    if tid:
        extra.setdefault("trace_id", tid)
    try:
        logger.info(msg, extra={"extra_fields": extra})
    except Exception:
        pass
