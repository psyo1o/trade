# -*- coding: utf-8 -*-
"""섀도우·라이브 규칙 이벤트 — append-only JSONL.

주문·포지션은 건드리지 않는다. 사후 평가는 ``analysis/run_shadow_review.py``.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_LOCK = threading.Lock()
_PATH_OVERRIDE: Path | None = None
_KST = ZoneInfo("Asia/Seoul")


def set_shadow_events_path(path: str | Path | None) -> None:
    """운영 경로 덮어쓰기. 빈 값이면 기본 ``logs/shadow_events.jsonl``."""
    global _PATH_OVERRIDE
    text = str(path or "").strip()
    _PATH_OVERRIDE = Path(text) if text else None


def resolve_shadow_events_path(explicit: str | Path | None = None) -> Path:
    if explicit is not None and str(explicit).strip():
        return Path(explicit)
    if _PATH_OVERRIDE is not None:
        return _PATH_OVERRIDE
    return Path(__file__).resolve().parents[1] / "logs" / "shadow_events.jsonl"


def now_kst_iso(now: datetime | None = None) -> str:
    if now is None:
        current = datetime.now(_KST)
    elif now.tzinfo is None:
        current = now.replace(tzinfo=_KST)
    else:
        current = now.astimezone(_KST)
    return current.isoformat(timespec="seconds")


def append_shadow_event(event: dict[str, Any], *, path: str | Path | None = None) -> Path:
    """한 줄 JSON 추가. 실패해도 예외를 올리지 않고 경로만 반환(매매 루프 보호)."""
    target = resolve_shadow_events_path(path)
    payload = dict(event)
    payload.setdefault("ts_kst", now_kst_iso())
    line = json.dumps(payload, ensure_ascii=False, default=str)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
    except OSError as exc:
        print(f"  ⚠️ [SHADOW] 이벤트 기록 실패: {exc}")
    return target
