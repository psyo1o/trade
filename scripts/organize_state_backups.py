# -*- coding: utf-8 -*-
"""기존 bot_state 스냅샷을 backups/YYYY/MM 으로 옮기고, 일당 1개 정리·잡파일 삭제.

- 루트 ``bot_state.before_circuit_reset_*``
- 레거시 평면 폴더 ``bot_state_backups/`` → ``backups/YYYY/MM/``
- ``thin_archives_one_per_day``: 종류×일자당 최신 1개만 유지
"""
from __future__ import annotations

import re
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BACKUPS = ROOT / "backups"
LEGACY_DIR = ROOT / "bot_state_backups"

JUNK = [
    ROOT / "_doc.txt",
    ROOT / "_runbot_vkospi.txt",
    ROOT / "_tmp_fix_changelog.py",
    ROOT / "_tmp_patch_phase5.py",
    ROOT / "_vkospi_excerpt.txt",
    ROOT / "analysis" / "_write_test.txt",
    ROOT / "scripts" / "_patch_phase5_session.py",
]

RESET_RE = re.compile(
    r"^bot_state\.before_circuit_reset_(\d{8})_(\d{6})\.json$"
)
# bot_state_YYYYMMDD_HHMMSS.json / trade_history_... / before_* 등 이름에서 날짜 추출
YMD_RE = re.compile(r"(20\d{6})")


def _move_to_month(src: Path, year: int, month: int, dest_name: str | None = None) -> Path:
    dest_dir = BACKUPS / f"{year:04d}" / f"{month:02d}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (dest_name or src.name)
    n = 0
    while dest.exists():
        n += 1
        dest = dest_dir / f"{Path(dest_name or src.name).stem}_dup{n}{dest.suffix}"
    shutil.move(str(src), str(dest))
    return dest


def _ymd_from_name(name: str) -> tuple[int, int] | None:
    m = YMD_RE.search(name)
    if not m:
        return None
    ymd = m.group(1)
    return int(ymd[:4]), int(ymd[4:6])


def organize_legacy_flat_dir(*, verbose: bool = False) -> Counter:
    """``bot_state_backups/`` 평면 파일을 ``backups/YYYY/MM/`` 로 이동."""
    counts: Counter = Counter()
    if not LEGACY_DIR.is_dir():
        print("  skip: bot_state_backups/ 없음")
        return counts
    files = [p for p in LEGACY_DIR.iterdir() if p.is_file()]
    print(f"  legacy files: {len(files)}")
    for p in sorted(files):
        ym = _ymd_from_name(p.name)
        if ym is None:
            # 날짜 없으면 mtime 기준
            try:
                mt = datetime.fromtimestamp(p.stat().st_mtime)
                year, month = mt.year, mt.month
            except Exception:
                year, month = 2026, 6
        else:
            year, month = ym
        dest = _move_to_month(p, year, month)
        key = f"{year:04d}/{month:02d}"
        counts[key] += 1
        if verbose:
            print(f"  move {p.name} -> {dest.relative_to(ROOT)}")
    # 빈 폴더 제거
    try:
        leftover = list(LEGACY_DIR.iterdir())
        if not leftover:
            LEGACY_DIR.rmdir()
            print("  removed empty bot_state_backups/")
        else:
            print(f"  leftover in bot_state_backups/: {len(leftover)}")
    except OSError as e:
        print(f"  rmdir bot_state_backups skip: {e}")
    for k in sorted(counts):
        print(f"  {k}: {counts[k]}")
    return counts


def main() -> None:
    print("=== organize backups ===")
    for p in sorted(ROOT.glob("bot_state.before_circuit_reset_*.json")):
        m = RESET_RE.match(p.name)
        if not m:
            continue
        ymd, hms = m.group(1), m.group(2)
        year, month = int(ymd[:4]), int(ymd[4:6])
        dest = _move_to_month(p, year, month, f"bot_state_{ymd}_{hms}.json")
        print(f"  move {p.name} -> {dest.relative_to(ROOT)}")

    print("=== organize bot_state_backups/ ===")
    organize_legacy_flat_dir(verbose=False)

    from execution.state_backup import thin_archives_one_per_day

    print("=== thin one per day (kind × day) ===")
    stats = thin_archives_one_per_day(BACKUPS)
    print(f"  kept={stats['kept']} deleted={stats['deleted']}")

    print("=== delete junk ===")
    for p in JUNK:
        if p.is_file():
            p.unlink()
            print(f"  del {p.relative_to(ROOT)}")
        else:
            print(f"  skip missing {p.name}")

    print("=== done ===")
    if BACKUPS.is_dir():
        by_month: Counter = Counter()
        for p in BACKUPS.rglob("*.json"):
            try:
                rel = p.relative_to(BACKUPS)
                parts = rel.parts
                if len(parts) >= 2:
                    by_month[f"{parts[0]}/{parts[1]}"] += 1
            except Exception:
                pass
        for k in sorted(by_month):
            print(f"  backups/{k}: {by_month[k]} files")


if __name__ == "__main__":
    main()
