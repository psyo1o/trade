# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from execution import state_backup as sb


def test_archive_file_writes_year_month(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "BACKUPS_ROOT", tmp_path / "backups")
    src = tmp_path / "bot_state.json"
    src.write_text('{"positions": {}}', encoding="utf-8")
    out = sb.archive_file(src, "bot_state", force=True, event="kr_open")
    assert out is not None
    assert out.is_file()
    assert out.parent.parent.parent == tmp_path / "backups"
    assert out.name.startswith("bot_state_")
    assert out.name.endswith("_kr_open.json")
    # 동일 이벤트 당일 재호출 → 스킵
    out2 = sb.archive_file(src, "bot_state", event="kr_open")
    assert out2 is None


def test_detect_session_events_edges():
    assert sb.detect_session_events(False, False, True, False) == ["kr_open"]
    assert sb.detect_session_events(True, False, False, False) == ["kr_close"]
    assert sb.detect_session_events(False, False, False, True) == ["us_open"]
    assert sb.detect_session_events(False, True, False, False) == ["us_close"]
    assert sb.detect_session_events(None, None, True, True) == ["kr_open", "us_open"]
    assert sb.detect_session_events(None, None, False, False) == []


def test_maybe_archive_on_session_edges(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "BACKUPS_ROOT", tmp_path / "backups")
    st_path = tmp_path / "bot_state.json"
    th_path = tmp_path / "trade_history.json"
    st_path.write_text('{"positions": {}}', encoding="utf-8")
    th_path.write_text("[]", encoding="utf-8")
    state: dict = {}
    fired = sb.maybe_archive_on_session_edges(
        state,
        state_path=st_path,
        history_path=th_path,
        kr_open=True,
        us_open=False,
    )
    assert fired == ["kr_open"]
    assert state[sb._STATE_KR_OPEN] is True
    assert state[sb._STATE_US_OPEN] is False
    # 같은 장중 유지 → 추가 없음
    fired2 = sb.maybe_archive_on_session_edges(
        state,
        state_path=st_path,
        history_path=th_path,
        kr_open=True,
        us_open=False,
    )
    assert fired2 == []
    # 마감
    fired3 = sb.maybe_archive_on_session_edges(
        state,
        state_path=st_path,
        history_path=th_path,
        kr_open=False,
        us_open=False,
    )
    assert fired3 == ["kr_close"]
    files = list((tmp_path / "backups").rglob("*.json"))
    names = {p.name for p in files}
    assert any("_kr_open.json" in n for n in names)
    assert any("_kr_close.json" in n for n in names)
    assert any(n.startswith("trade_history_") and "_kr_open.json" in n for n in names)


def test_thin_archives_one_per_day(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "BACKUPS_ROOT", tmp_path / "backups")
    d = tmp_path / "backups" / "2026" / "06"
    d.mkdir(parents=True)
    (d / "bot_state_20260616_100000.json").write_text("{}", encoding="utf-8")
    (d / "bot_state_20260616_150000.json").write_text("{}", encoding="utf-8")
    (d / "trade_history_20260616_100000.json").write_text("[]", encoding="utf-8")
    (d / "trade_history_20260616_120000.json").write_text("[]", encoding="utf-8")
    (d / "bot_state_20260617_090000.json").write_text("{}", encoding="utf-8")
    stats = sb.thin_archives_one_per_day(tmp_path / "backups")
    assert stats["deleted"] == 2
    assert stats["kept"] == 3
    assert (d / "bot_state_20260616_150000.json").is_file()
    assert not (d / "bot_state_20260616_100000.json").is_file()
    assert (d / "trade_history_20260616_120000.json").is_file()
    assert not (d / "trade_history_20260616_100000.json").is_file()
