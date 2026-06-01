# -*- coding: utf-8 -*-
"""
미장 유니버스 스크리너 — 고베타(High-Beta) · 섹터 분산(Sector Lock 방어).

모델
    1. S&P 500 · Nasdaq-100 구성 종목 + GICS(또는 yfinance) 섹터 조회
    2. **배제 섹터** (유틸·필수소비·부동산·소재) 원천 제외
    3. **Nasdaq-100** 통과 종목 시총 순 최대 ~90개 우선 편입
    4. 잔여 슬롯(~60–70)은 **S&P 500** 에서 섹터별 라운드로빈(시총 순)으로 채워 150개·섹터 다양성 확보
    5. ``us_universe_cache.json`` 에 tickers + sectors 저장

실행
    * ``run_bot.start_scanner_scheduler`` — 매일 15:20 US/Eastern ``run_us_screener()``
    * ``python us_screener.py`` — 단독 강제 재빌드
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import time
import traceback
from collections import defaultdict
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests
import yfinance as yf

try:
    import pytz  # type: ignore
except Exception:  # pragma: no cover
    pytz = None  # noqa: N816

# ---------------------------------------------------------------------------
# 고베타 유니버스 정책 상수
# ---------------------------------------------------------------------------
US_UNIVERSE_CACHE_FILE = "us_universe_cache.json"
US_UNIVERSE_CACHE_TTL_SEC = 24 * 3600
DEFAULT_UNIVERSE_LIMIT = 150
NDX_TARGET_MAX = 90
NDX_TARGET_SOFT_MIN = 80

# GICS / yfinance 섹터명 — 소문자 정규화 후 비교
EXCLUDED_SECTOR_KEYS = frozenset(
    {
        "utilities",
        "consumer staples",
        "consumer defensive",
        "real estate",
        "basic materials",
        "materials",  # Wikipedia GICS 열 명칭 (yfinance는 Basic Materials)
    }
)

SYMBOL_COL_CANDIDATES = ("Symbol", "Ticker", "NASDAQ Symbol")
SECTOR_COL_CANDIDATES = (
    "GICS Sector",
    "GIC Sector",
    "Sector",
    "GICS sector",
)

SP_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NDX_WIKI_URLS = (
    "https://en.wikipedia.org/wiki/Nasdaq-100",
    "https://en.wikipedia.org/wiki/NASDAQ-100",
    "https://en.wikipedia.org/wiki/List_of_Nasdaq-100_companies",
)

_WIKI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 cbot-universe/1.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _us_ticker_hyphen(sym: str) -> str:
    return str(sym or "").strip().upper().replace(".", "-")


def _normalize_sector_key(sector: str) -> str:
    return str(sector or "").strip().lower()


def is_excluded_gics_sector(sector: str) -> bool:
    """Defensive / Low-Vol 배제 섹터 여부."""
    key = _normalize_sector_key(sector)
    if not key or key == "unknown":
        return False
    return key in EXCLUDED_SECTOR_KEYS


def _fetch_market_cap_yf(sym: str) -> tuple[str, float]:
    """시총 — fast_info 우선(구버전보다 가벼움). 401 시 즉시 0 반환."""
    from utils.yfinance_guard import _yf_auth_failure, yf_suppress_stderr, yf_ticker_backoff

    s = _us_ticker_hyphen(sym)
    with yf_suppress_stderr():
        try:
            t = yf.Ticker(s)
            fi = getattr(t, "fast_info", None)
            if fi is not None:
                cap = float(fi.get("marketCap") or 0.0)
                if cap > 0:
                    return s, cap
            cap = float((t.info or {}).get("marketCap") or 0.0)
            if cap > 0:
                return s, cap
        except Exception as e:
            if _yf_auth_failure(e):
                yf_ticker_backoff(s)
    return s, 0.0


def _fetch_us_sector_gics(sym: str) -> tuple[str, str]:
    from utils.yfinance_guard import _yf_auth_failure, yf_suppress_stderr, yf_ticker_backoff

    s = _us_ticker_hyphen(sym)
    with yf_suppress_stderr():
        try:
            info = yf.Ticker(s).info or {}
            sec = str(info.get("sector") or info.get("industry") or "").strip() or "Unknown"
            return s, sec
        except Exception as e:
            if _yf_auth_failure(e):
                yf_ticker_backoff(s)
    return s, "Unknown"


def _yf_market_cap_probe() -> bool:
    """전역 yfinance 시총 가능 여부(1회). 실패 시 500+종 조회 생략 → 속도·401 스팸 방지."""
    from utils.yfinance_guard import _yf_auth_failure, yf_suppress_stderr

    with yf_suppress_stderr():
        try:
            fi = getattr(yf.Ticker("AAPL"), "fast_info", None)
            if fi is not None and float(fi.get("marketCap") or 0) > 0:
                return True
        except Exception as e:
            if _yf_auth_failure(e):
                return False
    return False


def _wiki_sector_map(rows: list[tuple[str, str]]) -> dict[str, str]:
    """위키 GICS 만 사용 — 대량 yfinance 섹터 보강 제거(구버전 대비 수백 호출 절약)."""
    return {sym: sec for sym, sec in rows}


def _order_proxy_caps(rows: list[tuple[str, str, str]]) -> dict[str, float]:
    """yfinance 불가 시 위키·필터 순서로 시총 대용 점수."""
    out: dict[str, float] = {}
    for i, (sym, _sec, _sk) in enumerate(rows):
        if sym not in out:
            out[sym] = float(10_000_000_000 - i)
    return out


def _wiki_read_tables(url: str) -> list[pd.DataFrame]:
    resp = requests.get(url, headers=_WIKI_HEADERS, timeout=45)
    status = resp.status_code
    html = resp.text or ""
    if status != 200 or len(html) < 10_000:
        print(f"  ⚠️ [위키] 응답 이상 — status={status}, len={len(html)} ({url})")

    last_exc: Exception | None = None
    for flavor in ("lxml", "bs4", "html5lib"):
        try:
            tables = pd.read_html(StringIO(html), flavor=flavor)
            if tables:
                return tables
        except ImportError as e:
            last_exc = e
        except Exception as e:
            last_exc = e
    raise RuntimeError(
        f"pd.read_html 실패(lxml/bs4/html5lib): {last_exc!r} — status={status}, len={len(html)} ({url})"
    )


def _find_column(table: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    flat = [str(c).split(".")[-1] for c in table.columns]
    for cand in candidates:
        if cand in flat:
            return table.columns[flat.index(cand)]
    return None


def _wiki_index_constituents(
    url: str,
    *,
    min_rows: int = 50,
) -> list[tuple[str, str]]:
    """
    위키 지수 구성 표에서 (티커, 섹터) 추출.

    섹터 열이 없으면 ``Unknown`` — 이후 yfinance 로 보강.
    """
    tables = _wiki_read_tables(url)
    for table in tables:
        sym_col = _find_column(table, SYMBOL_COL_CANDIDATES)
        if sym_col is None:
            continue
        sec_col = _find_column(table, SECTOR_COL_CANDIDATES)
        rows: list[tuple[str, str]] = []
        seen: set[str] = set()
        for i in range(len(table)):
            raw_sym = table[sym_col].iloc[i]
            sym = _us_ticker_hyphen(str(raw_sym))
            if not sym or sym in ("NAN", "NAT", "") or sym in seen:
                continue
            seen.add(sym)
            if sec_col is not None:
                sec = str(table[sec_col].iloc[i] or "").strip() or "Unknown"
            else:
                sec = "Unknown"
            rows.append((sym, sec))
        if len(rows) >= min_rows:
            return rows
    raise RuntimeError(f"위키 표에서 심볼 열을 찾지 못함: {url}")


def _wiki_ndx_constituents() -> list[tuple[str, str]]:
    last_err: Exception | None = None
    for url in NDX_WIKI_URLS:
        try:
            rows = _wiki_index_constituents(url, min_rows=90)
            if rows:
                return rows
        except Exception as e:
            last_err = e
            print(f"  ⚠️ [미장 유니버스] Nasdaq100 위키 실패({url}): {e}")
    if last_err:
        print(f"  ⚠️ [미장 유니버스] Nasdaq100 위키 전 경로 실패: {last_err!r}")
    return []


def _parallel_caps(symbols: list[str], *, label: str) -> dict[str, float]:
    """필터 통과 종목만 시총 조회(구버전: 전체 S&P+NDX와 유사 규모, 불필요 섹터 보강 없음)."""
    uniq = list(dict.fromkeys(_us_ticker_hyphen(s) for s in symbols if s))
    caps: dict[str, float] = {}
    if not uniq:
        return caps
    if not _yf_market_cap_probe():
        print(
            "  ⚠️ [미장 유니버스] yfinance 시총 조회 불가(401/rate limit) — "
            "위키·필터 순서로 순위 대체 (스케줄 15:20 ET 재시도 권장)"
        )
        return caps
    print(f"     ... {label} 시총 병렬 조회 ({len(uniq)}종) ...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        for sym, cap in ex.map(_fetch_market_cap_yf, uniq):
            if cap > 0:
                caps[sym] = cap
    if len(caps) < max(30, len(uniq) // 10):
        print(
            f"  ⚠️ [미장 유니버스] 시총 유효 {len(caps)}/{len(uniq)}종 — "
            "순위는 위키 순서 폴백"
        )
    return caps


def _filter_high_beta_rows(
    rows: list[tuple[str, str]],
    sector_map: dict[str, str],
) -> list[tuple[str, str, str]]:
    """배제 섹터 제거 → (symbol, sector, sector_key) 리스트."""
    kept: list[tuple[str, str, str]] = []
    dropped = 0
    for sym, wiki_sec in rows:
        sec = sector_map.get(sym, wiki_sec) or wiki_sec or "Unknown"
        if is_excluded_gics_sector(sec):
            dropped += 1
            continue
        kept.append((sym, sec, _normalize_sector_key(sec)))
    if dropped:
        print(f"     ... 배제 섹터 필터: {dropped}종 제외 (유니버스 후보 {len(kept)}종)")
    return kept


def _pick_ndx_universe(
    ndx_rows: list[tuple[str, str, str]],
    caps: dict[str, float],
    *,
    max_n: int = NDX_TARGET_MAX,
) -> list[str]:
    """필터 통과 NDX — 시총 순, 최대 ``max_n`` (시총 없으면 위키 순서)."""
    scored: list[tuple[float, str]] = []
    for i, (sym, _sec, _sk) in enumerate(ndx_rows):
        cap = caps.get(sym, 0.0)
        if cap <= 0:
            cap = float(10_000_000_000 - i)
        scored.append((cap, sym))
    scored.sort(key=lambda x: x[0], reverse=True)
    picked = [sym for _, sym in scored[:max_n]]
    if len(picked) < NDX_TARGET_SOFT_MIN:
        print(
            f"  ⚠️ [미장 유니버스] NDX 편입 {len(picked)}개 "
            f"(목표 {NDX_TARGET_SOFT_MIN}~{max_n})"
        )
    return picked


def _round_robin_sector_fill(
    sp_rows: list[tuple[str, str, str]],
    caps: dict[str, float],
    universe: set[str],
    *,
    slots: int,
) -> list[str]:
    """
    S&P 잔여 슬롯 — 섹터별 시총 순 큐에서 라운드로빈으로 ``slots`` 만큼 채움.
    """
    if slots <= 0:
        return []

    buckets: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for i, (sym, sec, sk) in enumerate(sp_rows):
        if sym in universe:
            continue
        cap = caps.get(sym, 0.0)
        if cap <= 0:
            cap = float(10_000_000_000 - i)
        buckets[sk or _normalize_sector_key(sec)].append((cap, sym))

    for sk in buckets:
        buckets[sk].sort(key=lambda x: x[0], reverse=True)

    sector_order = sorted(
        buckets.keys(),
        key=lambda sk: (-len(buckets[sk]), -max((c for c, _ in buckets[sk]), default=0.0)),
    )
    indices = {sk: 0 for sk in sector_order}
    filled: list[str] = []

    while len(filled) < slots:
        progressed = False
        for sk in sector_order:
            if len(filled) >= slots:
                break
            lst = buckets[sk]
            idx = indices[sk]
            while idx < len(lst):
                _cap, sym = lst[idx]
                indices[sk] = idx + 1
                idx += 1
                if sym in universe:
                    continue
                filled.append(sym)
                universe.add(sym)
                progressed = True
                break
        if not progressed:
            break

    return filled


def build_high_beta_us_universe(*, limit: int = DEFAULT_UNIVERSE_LIMIT) -> tuple[list[str], dict[str, str], dict]:
    """
    고베타·섹터 분산 미장 유니버스 빌드.

    Returns:
        (tickers, sectors, meta)
    """
    print(
        "  -> ⏳ [미장 유니버스] 고베타 모델: NDX 우선(~90) + S&P 섹터 라운드로빈 "
        f"(총 {limit}개, 배제=유틸·필수소비·부동산·소재)"
    )

    sp_raw = _wiki_index_constituents(SP_WIKI_URL, min_rows=400)
    ndx_raw = _wiki_ndx_constituents()

    sector_map = _wiki_sector_map(sp_raw + ndx_raw)

    sp_beta = _filter_high_beta_rows(sp_raw, sector_map)
    ndx_beta = _filter_high_beta_rows(ndx_raw, sector_map) if ndx_raw else []

    cand_rows = ndx_beta + [r for r in sp_beta if r[0] not in {x[0] for x in ndx_beta}]
    caps = _parallel_caps([s for s, _, _ in cand_rows], label="필터통과")
    if len(caps) < max(30, len(cand_rows) // 10):
        caps = {**_order_proxy_caps(ndx_beta), **_order_proxy_caps(sp_beta)}

    ndx_pick = _pick_ndx_universe(ndx_beta, caps, max_n=NDX_TARGET_MAX)
    universe: set[str] = set(ndx_pick)
    print(f"     ... NDX 편입: {len(ndx_pick)}개")

    slots_left = max(0, limit - len(ndx_pick))
    sp_fill: list[str] = []
    if slots_left > 0:
        sp_fill = _round_robin_sector_fill(
            sp_beta,
            caps,
            universe,
            slots=slots_left,
        )
        print(f"     ... S&P 섹터 분산 편입: {len(sp_fill)}개 (목표 잔여 {slots_left})")

    final = (ndx_pick + sp_fill)[:limit]
    if len(final) < limit:
        print(f"  ⚠️ [미장 유니버스] 목표 {limit}개 미만 ({len(final)}개)")

    if not final:
        raise RuntimeError("고베타 유니버스 빌드 실패 — 후보 0종")

    wiki_sec_by_sym = {sym: sec for sym, sec in sp_raw + ndx_raw}
    sectors = {sym: wiki_sec_by_sym.get(sym, "Unknown") for sym in final}
    if _yf_market_cap_probe():
        print(f"     ... GICS 섹터 최종 조회 ({len(final)}종) ...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
            sec_pairs = list(ex.map(_fetch_us_sector_gics, final))
        for sym, sec in sec_pairs:
            if sec and _normalize_sector_key(sec) not in ("", "unknown"):
                sectors[sym] = sec
    else:
        print(f"     ... GICS 섹터: 위키 값 사용 (yfinance 불가, {len(final)}종)")

    final, sectors, purged_n, sp_fill_extra = _purge_excluded_after_final_sectors(
        final,
        sectors,
        sp_beta=sp_beta,
        caps=caps,
        limit=limit,
    )
    if purged_n:
        print(
            f"     ... 최종 yfinance 섹터 재검증: 배제 {purged_n}종 교체 "
            f"(위키/초기 섹터와 불일치 보정)"
        )
        sp_fill = sp_fill + sp_fill_extra

    sector_counts: dict[str, int] = defaultdict(int)
    for sec in sectors.values():
        sector_counts[sec] += 1

    yf_ok = _yf_market_cap_probe()
    meta = {
        "model": "high_beta_sector_diversified",
        "target_limit": limit,
        "actual_n": len(final),
        "ndx_n": len(ndx_pick),
        "sp_fill_n": len(sp_fill),
        "purged_after_sector_fetch": purged_n,
        "caps_source": "yfinance" if yf_ok and len(caps) >= 30 else "wiki_order_proxy",
        "excluded_sectors": sorted(EXCLUDED_SECTOR_KEYS),
        "sector_histogram": dict(sorted(sector_counts.items(), key=lambda x: -x[1])),
    }
    return final, sectors, meta


def _purge_excluded_after_final_sectors(
    final: list[str],
    sectors: dict[str, str],
    *,
    sp_beta: list[tuple[str, str, str]],
    caps: dict[str, float],
    limit: int,
) -> tuple[list[str], dict[str, str], int, list[str]]:
    """
    yfinance 최종 섹터 기준 배제 종목 제거 후 S&P 라운드로빈으로 슬롯 보충.

    Returns:
        (final, sectors, purged_count, extra_sp_fill_tickers)
    """
    kept: list[str] = []
    purged = 0
    for sym in final:
        if is_excluded_gics_sector(sectors.get(sym, "")):
            sectors.pop(sym, None)
            purged += 1
        else:
            kept.append(sym)

    universe = set(kept)
    need = max(0, limit - len(kept))
    extra_fill: list[str] = []
    wiki_sec = {sym: sec for sym, sec, _ in sp_beta}
    if need > 0:
        extra_fill = _round_robin_sector_fill(sp_beta, caps, universe, slots=need * 2)
        for sym in extra_fill:
            if len(kept) >= limit:
                break
            if sym in universe:
                continue
            sec = sectors.get(sym) or wiki_sec.get(sym, "Unknown")
            if is_excluded_gics_sector(sec):
                continue
            sectors[sym] = sec
            kept.append(sym)
            universe.add(sym)

    return kept[:limit], sectors, purged, extra_fill


def _write_universe_cache(
    cache_path: Path,
    tickers: list[str],
    sectors: dict[str, str],
    meta: dict,
) -> None:
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "tickers": tickers,
        "sectors": sectors,
        "meta": meta,
    }
    cache_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_or_build_us_universe(
    *,
    limit: int = DEFAULT_UNIVERSE_LIMIT,
    force_refresh: bool = False,
    cache_path: Path | None = None,
    ttl_sec: int = US_UNIVERSE_CACHE_TTL_SEC,
) -> list[str]:
    """
    캐시 TTL 내이면 ``us_universe_cache.json`` 사용, 아니면 고베타 모델로 재빌드.

    ``strategy.sector_lock.seed_us_sector_cache`` 에 섹터를 주입한다.
    """
    from strategy.sector_lock import seed_us_sector_cache

    base = Path(__file__).resolve().parent
    path = cache_path or (base / US_UNIVERSE_CACHE_FILE)

    if not force_refresh:
        try:
            if path.exists():
                age = time.time() - path.stat().st_mtime
                if age < ttl_sec:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    tickers = payload.get("tickers") or []
                    sectors = payload.get("sectors") or {}
                    if 100 <= len(tickers) <= 150 and all(isinstance(x, str) for x in tickers):
                        seed_us_sector_cache(sectors)
                        print(
                            f"  -> ✅ 미장 유니버스 캐시 사용: {len(tickers)}개 "
                            f"(갱신 후 {int(age // 3600)}h, {path.name})"
                        )
                        return tickers
        except Exception as e:
            print(f"  ⚠️ 미장 유니버스 캐시 읽기 실패 — 재빌드: {e}")

    try:
        tickers, sectors, meta = build_high_beta_us_universe(limit=limit)
        _write_universe_cache(path, tickers, sectors, meta)
        seed_us_sector_cache(sectors)
        print(f"✅ 미장 유니버스 {len(tickers)}개 빌드·캐시 저장 완료 ({path.name})")
        return tickers
    except Exception as e:
        print(f"⚠️ [경고] 미장 유니버스 빌드 실패: {e}")
        if str(os.environ.get("US_SCREENER_DEBUG", "")).strip().lower() in ("1", "true", "yes"):
            traceback.print_exc()
        try:
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                tickers = payload.get("tickers") or []
                sectors = payload.get("sectors") or {}
                if 100 <= len(tickers) <= 150:
                    seed_us_sector_cache(sectors)
                    age_h = int((time.time() - path.stat().st_mtime) // 3600)
                    print(
                        f"    -> 🗂️ 기존 캐시 재사용: {len(tickers)}개 "
                        f"(갱신 후 {age_h}h) — 신규 빌드 실패 폴백"
                    )
                    return tickers
        except Exception as e2:
            print(f"    ⚠️ 기존 캐시 재사용도 실패: {e2}")
        print("    -> 비상용 백업 목록으로 대신합니다.")
        fb = ["QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"]
        seed_us_sector_cache({t: "Unknown" for t in fb})
        # 빌드 실패 시 8종 백업으로 캐시를 덮어쓰지 않음(기존 100~150 캐시 보존)
        if path.exists():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
                old_tickers = old.get("tickers") or []
                if len(old_tickers) >= 50:
                    print(f"    -> 기존 캐시 {len(old_tickers)}개 유지 (백업 목록으로 덮어쓰지 않음)")
                    seed_us_sector_cache(old.get("sectors") or {})
                    return old_tickers
            except Exception:
                pass
        return fb[: max(1, min(limit, len(fb)))]


def run_us_screener(limit: int = DEFAULT_UNIVERSE_LIMIT) -> list[str]:
    """스케줄러·CLI — 강제 재빌드."""
    now_tag = datetime.now(pytz.timezone("US/Eastern")) if pytz else datetime.now()
    print(f"🌙 [미장 발굴기] US 고베타 유니버스 재빌드 ({now_tag:%Y-%m-%d %H:%M %Z})")
    tickers = load_or_build_us_universe(limit=limit, force_refresh=True)
    print(f"🎉 [미장 발굴기] 완료 — 총 {len(tickers)}개 ({US_UNIVERSE_CACHE_FILE} 갱신)")
    return tickers


if __name__ == "__main__":
    run_us_screener()
