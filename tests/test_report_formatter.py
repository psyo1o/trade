"""report_formatter 단위 테스트."""
from __future__ import annotations

from services.report_formatter import format_survival_telegram_message


def test_survival_message_contains_markets():
    snap = {
        "weather": {"KR": "☀️", "US": "☀️", "COIN": "🌧️"},
        "labels": {
            "kr": {"cash": 1_000_000, "total": 5_000_000, "roi": 1.5},
            "us": {"cash": 100.0, "total": 500.0, "roi": -2.0},
            "coin": {"cash": 50_000, "total": 100_000, "roi": 0.0},
        },
        "holdings": {"kr": [], "us": [], "coin": []},
        "snapshot_saved_at": "2026-05-28 10:00:00",
    }
    msg = format_survival_telegram_message(snap, weekend_kis_suppress=True)
    assert "생존신고" in msg
    assert "1,000,000" in msg
    assert "직전 조회" in msg
