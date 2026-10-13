# -*- coding: utf-8 -*-
"""섀도우 규칙 모드 — off | shadow | live. 알 수 없는 값은 shadow(주문 없음)."""
from __future__ import annotations

RULE_MODES = ("off", "shadow", "live")


def normalize_rule_mode(value, default: str = "shadow") -> str:
    """설정값을 off/shadow/live 로 정규화.

    * 키가 없거나 빈 문자열이면 ``default`` (기본 shadow).
    * 오타·알 수 없는 값은 **shadow** — live 로 오인하지 않는다.
    """
    fallback = default if str(default or "").strip().lower() in RULE_MODES else "shadow"
    if value is None:
        return fallback
    text = str(value).strip().lower()
    if text in RULE_MODES:
        return text
    if text == "":
        return fallback
    return "shadow"
