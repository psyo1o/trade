# -*- coding: utf-8 -*-
"""
기존 trade_history.json 에 sector 필드를 채운다.

사용 (프로젝트 루트에서):
    python scripts/backfill_trade_history_sectors.py

동작:
    1. trade_history.json 백업 (trade_history.json.bak-YYYYMMDD-HHMMSS)
    2. 각 건에 sector 없으면 ``strategy.sector_lock`` (get_kr_sector / get_us_sector) 과 동일 경로로 조회
    3. trade_history.json 갱신
    4. trade_history_sectors_backfill.json — 건별 키·섹터 요약(오버레이·검수용)

봇/GUI는 기록의 sector 필드를 우선 쓰고, 없으면 sectors_backfill 의 by_trade_key 를 참고할 수 있다.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

HISTORY_PATH = _ROOT / "trade_history.json"
OVERLAY_PATH = _ROOT / "trade_history_sectors_backfill.json"


def main() -> int:
    if not HISTORY_PATH.is_file():
        print(f"❌ 없음: {HISTORY_PATH}")
        print("   매매가 한 번이라도 기록된 뒤 다시 실행하세요.")
        return 1

    try:
        with open(_ROOT / "config.json", encoding="utf-8") as f:
            config = json.load(f)
        from api import kis_api

        kis_api.configure(config)
        kis_api.refresh_brokers_if_needed()
    except Exception as e:
        print(f"⚠️ KIS 초기화 실패 — 국장 섹터는 네이버 폴백만 사용될 수 있음: {e}")

    from utils.trade_sector import resolve_trade_sector, trade_record_key

    raw = HISTORY_PATH.read_text(encoding="utf-8")
    try:
        history = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"❌ JSON 파싱 실패: {e}")
        return 1
    if not isinstance(history, list):
        print("❌ trade_history 루트가 배열이 아닙니다.")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = HISTORY_PATH.with_name(f"trade_history.json.bak-{stamp}")
    shutil.copy2(HISTORY_PATH, backup)
    print(f"📋 백업: {backup.name}")

    by_key: dict[str, str] = {}
    filled = 0
    skipped = 0
    unique_pairs: set[tuple[str, str]] = set()

    for i, item in enumerate(history):
        if not isinstance(item, dict):
            continue
        if str(item.get("sector") or "").strip():
            key = trade_record_key(item)
            by_key[key] = str(item["sector"]).strip()
            skipped += 1
            continue

        mk = str(item.get("market") or "")
        tk = str(item.get("ticker") or "")
        unique_pairs.add((mk, tk))

    print(f"  조회 대상 종목 {len(unique_pairs)}개 (섹터 비어 있는 건만)…")

    sector_cache: dict[tuple[str, str], str] = {}
    for mk, tk in sorted(unique_pairs, key=lambda x: (x[0], x[1])):
        try:
            sector_cache[(mk, tk)] = resolve_trade_sector(mk, tk)
        except Exception:
            sector_cache[(mk, tk)] = "Unknown"
        time.sleep(0.05)

    for item in history:
        if not isinstance(item, dict):
            continue
        if str(item.get("sector") or "").strip():
            continue
        mk = str(item.get("market") or "")
        tk = str(item.get("ticker") or "")
        sec = sector_cache.get((mk, tk), "Unknown")
        item["sector"] = sec
        by_key[trade_record_key(item)] = sec
        filled += 1

    HISTORY_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    for item in history:
        if isinstance(item, dict) and str(item.get("sector") or "").strip():
            by_key[trade_record_key(item)] = str(item["sector"]).strip()

    overlay = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(HISTORY_PATH.name),
        "backup": backup.name,
        "filled_count": filled,
        "already_had_sector": skipped,
        "by_trade_key": by_key,
    }
    OVERLAY_PATH.write_text(
        json.dumps(overlay, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"✅ sector 채움: {filled}건 | 기존 보유: {skipped}건 | 총 {len(history)}건")
    print(f"✅ 요약 파일: {OVERLAY_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
