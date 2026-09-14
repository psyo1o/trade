# -*- coding: utf-8 -*-
"""로그 연·월 폴더 경로·namer."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from utils.logger import _daily_log_namer, _log_month_dir, _month_dir_for_ymd


def test_month_dir_no_zero_pad():
    dt = datetime(2026, 9, 14, tzinfo=ZoneInfo("Asia/Seoul"))
    d = _log_month_dir(dt)
    assert d.as_posix().endswith("2026년/9월")


def test_daily_namer_puts_file_in_month_folder(tmp_path, monkeypatch):
    import utils.logger as lg

    monkeypatch.setattr(lg, "_LOG_ROOT", tmp_path)
    out = _daily_log_namer(str(tmp_path / "2026년" / "9월" / "bot.log.2026-09-13"))
    p = Path(out)
    assert p.name == "bot.2026-09-13.log"
    assert p.parent.name == "9월"
    assert p.parent.parent.name == "2026년"
    assert p.parent.is_dir()


def test_month_dir_for_ymd():
    d = _month_dir_for_ymd(2026, 12)
    assert "2026년" in d.parts
    assert "12월" in d.parts
