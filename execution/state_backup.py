# -*- coding: utf-8 -*-
"""장부·매매내역 세션 경계 아카이브 백업.

구조::
    backups/YYYY/MM/bot_state_YYYYMMDD_HHMMSS_kr_open.json
    backups/YYYY/MM/trade_history_YYYYMMDD_HHMMSS_us_close.json

시점(정규장 개장↔마감 전이, 15분 사이클 근사)::
    kr_open / kr_close / us_open / us_close

sidecar ``bot_state.bak``(매 저장 덮어쓰기)과 별개.
"""
from __future__ import annotations

import re
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_BOT_ROOT = Path(__file__).resolve().parent.parent
BACKUPS_ROOT = _BOT_ROOT / "backups"
_SEOUL = ZoneInfo("Asia/Seoul")

SESSION_EVENTS = ("kr_open", "kr_close", "us_open", "us_close")

# state 에 직전 장중 여부 저장 (재시작 후 edge 감지용)
_STATE_KR_OPEN = "_session_bak_kr_open"
_STATE_US_OPEN = "_session_bak_us_open"

_YMD_RE = re.compile(r"(20\d{6})")
_STAMP_RE = re.compile(r"(20\d{6})_(\d{6})")


def _now_seoul() -> datetime:
    return datetime.now(_SEOUL)


def month_dir(when: datetime | None = None) -> Path:
    dt = when or _now_seoul()
    return BACKUPS_ROOT / f"{dt.year:04d}" / f"{dt.month:02d}"


def _stamp(when: datetime | None = None) -> str:
    dt = when or _now_seoul()
    return dt.strftime("%Y%m%d_%H%M%S")


def _day_ymd(when: datetime | None = None) -> str:
    dt = when or _now_seoul()
    return dt.strftime("%Y%m%d")


def _event_suffix(event: str | None) -> str:
    ev = str(event or "").strip().lower()
    if ev in SESSION_EVENTS:
        return f"_{ev}"
    return ""


def event_archived_today(kind: str, event: str, when: datetime | None = None) -> bool:
    """당일 해당 세션 이벤트 아카이브가 이미 있으면 True."""
    d = month_dir(when)
    if not d.is_dir():
        return False
    ymd = _day_ymd(when)
    kind = str(kind or "file").strip().lower()
    ev = str(event or "").strip().lower()
    if ev not in SESSION_EVENTS:
        return False
    # bot_state_YYYYMMDD_*_kr_open.json
    for p in d.glob(f"{kind}_{ymd}_*_{ev}.json"):
        if p.is_file():
            return True
    return False


def archive_file(
    src: Path,
    kind: str,
    *,
    force: bool = False,
    event: str | None = None,
    when: datetime | None = None,
) -> Path | None:
    """``src`` 를 ``backups/YYYY/MM/{kind}_YYYYMMDD_HHMMSS[_event].json`` 으로 복사.

    ``event`` 가 세션 이벤트이고 ``force=False`` 이면 당일 동일 이벤트는 스킵.
    """
    try:
        src = Path(src)
        if not src.is_file() or src.stat().st_size <= 0:
            return None
        kind = str(kind or "file").strip().lower() or "file"
        now = when or _now_seoul()
        ev = str(event or "").strip().lower() or None
        if ev and ev not in SESSION_EVENTS:
            ev = None
        if ev and not force and event_archived_today(kind, ev, now):
            return None
        dest_dir = month_dir(now)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{kind}_{_stamp(now)}{_event_suffix(ev)}.json"
        if dest.exists():
            dest = dest_dir / f"{kind}_{_stamp(now)}{_event_suffix(ev)}_{src.stat().st_size}.json"
        shutil.copy2(src, dest)
        return dest
    except Exception:
        return None


def archive_bot_state(
    state_path: Path,
    *,
    force: bool = False,
    event: str | None = None,
) -> Path | None:
    return archive_file(state_path, "bot_state", force=force, event=event)


def archive_trade_history(
    history_path: Path,
    *,
    force: bool = False,
    event: str | None = None,
) -> Path | None:
    return archive_file(history_path, "trade_history", force=force, event=event)


def detect_session_events(
    prev_kr: bool | None,
    prev_us: bool | None,
    curr_kr: bool,
    curr_us: bool,
) -> list[str]:
    """직전/현재 장중 여부로 세션 경계 이벤트 목록."""
    out: list[str] = []
    if prev_kr is None:
        # 첫 관측: 이미 장중이면 개장 스냅(당일 1회 가드가 중복 방지)
        if curr_kr:
            out.append("kr_open")
    elif (not prev_kr) and curr_kr:
        out.append("kr_open")
    elif prev_kr and (not curr_kr):
        out.append("kr_close")

    if prev_us is None:
        if curr_us:
            out.append("us_open")
    elif (not prev_us) and curr_us:
        out.append("us_open")
    elif prev_us and (not curr_us):
        out.append("us_close")
    return out


def maybe_archive_on_session_edges(
    state: dict,
    *,
    state_path: Path,
    history_path: Path | None,
    kr_open: bool,
    us_open: bool,
) -> list[str]:
    """장 개장/마감 전이 시 bot_state·trade_history 아카이브.

    ``state`` 에 직전 장중 플래그를 갱신한다(이후 save_state 로 유지).
    반환: 실제 아카이브를 시도한 이벤트명 목록.
    """
    if not isinstance(state, dict):
        return []
    prev_kr = state.get(_STATE_KR_OPEN)
    prev_us = state.get(_STATE_US_OPEN)
    if prev_kr is not None:
        prev_kr = bool(prev_kr)
    if prev_us is not None:
        prev_us = bool(prev_us)
    curr_kr = bool(kr_open)
    curr_us = bool(us_open)
    events = detect_session_events(prev_kr, prev_us, curr_kr, curr_us)
    fired: list[str] = []
    sp = Path(state_path)
    hp = Path(history_path) if history_path else None
    for ev in events:
        # 당일 동일 이벤트는 archive_file 내부에서도 스킵
        out_s = archive_bot_state(sp, event=ev)
        out_h = None
        if hp is not None and hp.is_file():
            out_h = archive_trade_history(hp, event=ev)
        if out_s is not None or out_h is not None:
            fired.append(ev)
    state[_STATE_KR_OPEN] = curr_kr
    state[_STATE_US_OPEN] = curr_us
    return fired


def _archive_family(name: str) -> str | None:
    """파일명 → bot_state | trade_history | None."""
    n = name.lower()
    if n.startswith("trade_history"):
        return "trade_history"
    if n.startswith("bot_state"):
        return "bot_state"
    return None


def _ymd_and_sort_key(name: str) -> tuple[str | None, str]:
    m = _STAMP_RE.search(name)
    if m:
        return m.group(1), f"{m.group(1)}_{m.group(2)}"
    m2 = _YMD_RE.search(name)
    if m2:
        return m2.group(1), m2.group(1) + "_000000"
    return None, name


def thin_archives_one_per_day(
    root: Path | None = None,
    *,
    dry_run: bool = False,
) -> dict:
    """기존 아카이브를 **종류(bot_state/trade_history)×일자당 1개**(최신 스탬프)만 남김.

    반환: ``{"kept": n, "deleted": n, "by_day": {...}}``
    """
    base = Path(root) if root else BACKUPS_ROOT
    kept = deleted = 0
    by_day: dict[str, int] = defaultdict(int)
    if not base.is_dir():
        return {"kept": 0, "deleted": 0, "by_day": {}}

    groups: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for p in base.rglob("*.json"):
        if not p.is_file():
            continue
        fam = _archive_family(p.name)
        if fam is None:
            continue
        ymd, _ = _ymd_and_sort_key(p.name)
        if not ymd:
            continue
        groups[(fam, ymd)].append(p)

    for (fam, ymd), files in groups.items():
        files_sorted = sorted(
            files,
            key=lambda x: _ymd_and_sort_key(x.name)[1],
            reverse=True,
        )
        keep = files_sorted[0]
        kept += 1
        by_day[f"{fam}:{ymd}"] = 1
        for p in files_sorted[1:]:
            deleted += 1
            if not dry_run:
                try:
                    p.unlink()
                except OSError:
                    deleted -= 1
    return {"kept": kept, "deleted": deleted, "by_day": dict(by_day)}
