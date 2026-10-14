# -*- coding: utf-8 -*-
"""providers 层：模型适配层（对外只暴露 base 契约 + 装配函数）。"""
from .base import (ModelProvider, envelope, err, estimate_tokens,
                   MODEL_TIMEOUT, MODEL_RATE_LIMIT, MODEL_UNAVAILABLE,
                   MODEL_AUTH, MODEL_BAD_REQUEST, MODEL_INTERNAL)
from .registry import build_provider, build_all, BUILDERS

__all__ = [
    "ModelProvider", "envelope", "err", "estimate_tokens",
    "MODEL_TIMEOUT", "MODEL_RATE_LIMIT", "MODEL_UNAVAILABLE",
    "MODEL_AUTH", "MODEL_BAD_REQUEST", "MODEL_INTERNAL",
    "build_provider", "build_all", "BUILDERS",
]
