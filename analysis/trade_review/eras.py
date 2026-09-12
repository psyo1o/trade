# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any


def _parse_day(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def load_eras(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("strategy_eras.json must be a list")
    return raw


def era_for_timestamp(ts: str | None, eras: list[dict[str, Any]]) -> dict[str, Any]:
    if not ts:
        return {"id": "unknown", "label": "미상"}
    d = _parse_day(ts)
    for era in eras:
        start = _parse_day(str(era["start"]))
        end = _parse_day(str(era["end"]))
        if start <= d <= end:
            return era
    return {"id": "unknown", "label": "미상"}


def era_label(ts: str | None, eras: list[dict[str, Any]]) -> str:
    return str(era_for_timestamp(ts, eras).get("label", "미상"))
