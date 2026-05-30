# -*- coding: utf-8 -*-
"""
장부(JSON) 입출력과 **시장별·합산** 리스크 플래그.

``bot_state.json`` 스키마(일부)
    * ``positions`` — 티커별 ``buy_p``, ``sl_p``, ``tier``, ``qty``(실계좌·매수 시 동기화, 주말 GUI 표시) 등.
    * ``cooldown`` — 매수 직후 짧은 재진입 방지(분 단위, ``in_cooldown``).
    * ``ticker_cooldowns`` — **매도 후** 티커별 절대 만료 시각(ISO). 매도 사유별(익절 1h·손절·타임스탑 24h); 분할 익절 잔량 시 미부여.
    * ``peak_equity_{KR|US|COIN}`` — 시장별 MDD 브레이크용 고점.
    * ``peak_total_equity`` / ``last_reset_week`` / ``account_circuit_peak_reset_pending`` /
      ``account_circuit_cooldown_until`` — Phase5 합산 서킷(월요일 주차 MDD).

V7.1: 모든 보유 종목을 액티브 매매·샹들리에 동일 적용. 포지션별 ``scale_out_done`` 은
``load_state`` 시 기본값 ``false`` 로 보강된다.
"""
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from utils.helpers import is_coin_ticker

# 동일 프로세스 내 GUI 스레드·매매 루프가 동시에 장부를 쓸 때 WinError 5/32 등 완화
_save_state_lock = threading.RLock()


def _empty_state_shell() -> dict:
    return {
        "positions": {},
        "cooldown": {},
        "ticker_cooldowns": {},
        "last_kis_display_snapshot": {},
        "last_coin_display_snapshot": {},
    }


def _finalize_loaded_dict(data: dict) -> dict:
    """로드 직후 공통 키 보강·레거시 이관."""
    data.setdefault("positions", {})
    data.setdefault("cooldown", {})
    data.setdefault("ticker_cooldowns", {})
    data.setdefault("last_kis_display_snapshot", {})
    data.setdefault("last_coin_display_snapshot", {})
    try:
        pt = float(data.get("peak_total_equity", 0.0) or 0.0)
    except (TypeError, ValueError):
        pt = 0.0
    try:
        legacy_pt = float(data.get("peak_equity_total_krw", 0.0) or 0.0)
    except (TypeError, ValueError):
        legacy_pt = 0.0
    if pt <= 0.0 and legacy_pt > 0.0:
        data["peak_total_equity"] = legacy_pt
    if "peak_equity_total_krw" in data:
        data.pop("peak_equity_total_krw", None)

    for _tk, _pos in list(data.get("positions", {}).items()):
        if isinstance(_pos, dict) and "scale_out_done" not in _pos:
            _pos["scale_out_done"] = False
    return data


def _recover_from_sidecar_backups(main_path: Path) -> dict | None:
    """동기화 충돌·0바이트 시 같은 폴더의 .bak / corrupt 백업에서 장부 딕셔너리 복구."""
    p = main_path.parent
    stem = main_path.stem
    candidates: list[Path] = []
    for name in (f"{stem}.bak", f"{stem}.json.bak", "bot_state.prev.json"):
        candidates.append(p / name)
    try:
        corrupt = sorted(
            p.glob(f"{stem}_corrupt_*.bak"),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        candidates.extend(corrupt)
    except Exception:
        pass
    for c in candidates:
        if not c.is_file():
            continue
        try:
            raw = c.read_text(encoding="utf-8")
            if not raw.strip():
                continue
            data = json.loads(raw)
            if isinstance(data, dict) and "positions" in data:
                return data
        except Exception:
            continue
    return None


def load_state(path: Path):
    """
    bot_state.json 로드. 파일 없음 / 빈 파일 / JSON 깨짐 시 기본 장부 반환(크래시 방지).

    * **0바이트·깨진 JSON** 은 동기화(OneDrive 등) 경합으로 자주 생기며, 예전 코드는 빈 ``positions`` 로
      메인 파일을 **덮어써 장부가 ‘초기화’된 것처럼** 보이게 했다. 이제는 sidecar ``.bak``·corrupt 백업을 먼저 시도한다.
    * 깨진 원본은 ``*_corrupt_타임스탬프.bak`` 으로 남긴다.
    """
    empty = _empty_state_shell()
    if not path.exists():
        return _finalize_loaded_dict(dict(empty))
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return _finalize_loaded_dict(dict(empty))
    if not raw.strip():
        recovered = _recover_from_sidecar_backups(path)
        if recovered is not None:
            print(
                "⚠️ [guard] bot_state.json 이 0바이트였습니다. 백업 파일에서 복구했습니다. "
                "(클라우드 동기화·동시 저장 충돌 가능 — 폴더를 ‘항상 이 장치에 유지’ 권장)"
            )
            fin = _finalize_loaded_dict(recovered)
            try:
                save_state(path, fin)
            except Exception:
                pass
            return fin
        print(
            "⚠️ [guard] bot_state.json 이 비어 있고 복구용 .bak 도 없습니다. 빈 장부로 진행합니다."
        )
        return _finalize_loaded_dict(dict(empty))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        bak: Path | None = None
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            bak = path.parent / f"{path.stem}_corrupt_{stamp}.bak"
            bak.write_bytes(raw.encode("utf-8", errors="replace"))
        except Exception:
            bak = None
        recovered = _recover_from_sidecar_backups(path)
        if recovered is not None:
            print(
                "⚠️ [guard] bot_state.json JSON 손상 — sidecar 백업으로 복구합니다."
                + (f" (손상본: {bak.name})" if bak else "")
            )
            fin = _finalize_loaded_dict(recovered)
            try:
                save_state(path, fin)
            except Exception as e:
                print(f"⚠️ [guard] 복구본 메인 기록 실패: {type(e).__name__}: {e}")
            return fin
        try:
            path.write_text(json.dumps(empty, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        print(
            "⚠️ [guard] bot_state.json 이 깨져 있고 복구 백업도 없어 기본 장부로 재생성했습니다."
            + (f" (손상 스냅샷: {bak.name})" if bak else "")
        )
        return _finalize_loaded_dict(dict(empty))
    if not isinstance(data, dict):
        return _finalize_loaded_dict(dict(empty))
    return _finalize_loaded_dict(data)


def save_state(path: Path, state):
    """UTF-8 JSON으로 **원자적** 덮어쓰기(임시 파일 후 rename). 부모 디렉터리가 없으면 생성.

    Windows에서 클라우드·NAS 동기화·백신이 ``bot_state.json`` 을 잠그면 ``os.replace`` 가
    거부될 수 있어 짧은 간격으로 재시도한다. GUI가 ``write_text`` 로 직접 덮어쓰면
    동시 쓰기·깨진 JSON으로 이어지므로 **항상 이 함수만** 쓸 것.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    with _save_state_lock:
        for attempt in range(18):
            fd, tmp = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp, path)
                return
            except OSError as e:
                win = getattr(e, "winerror", None)
                errno = win if win is not None else e.errno
                # 5 접근거부, 32 다른 프로세스가 파일 사용 중, 33 잠금 등
                retryable = errno in (5, 13, 32, 33)
                try:
                    if os.path.isfile(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                if attempt < 17 and retryable:
                    time.sleep(min(0.35, 0.04 * (attempt + 1)))
                    continue
                raise
            finally:
                try:
                    if os.path.isfile(tmp):
                        os.remove(tmp)
                except OSError:
                    pass

def in_cooldown(state, code, minutes=120):
    """``cooldown[code]`` ISO 시각 기준으로 ``minutes`` 분 안이면 True."""
    cd = state.get("cooldown", {}).get(code)
    if not cd:
        return False
    try:
        t = datetime.fromisoformat(cd)
        return datetime.now() < t + timedelta(minutes=minutes)
    except:
        return False

def set_cooldown(state, code):
    """해당 티커의 쿨다운 시작 시각을 ``now`` 로 기록."""
    state.setdefault("cooldown", {})[code] = datetime.now().isoformat(timespec="seconds")

def can_open_new(ticker, state, max_positions=5): # 메인에서 안 던져주면 기본값 5
    """[시장별 독립 슬롯] 메인에서 설정한 종목 수(max_positions)만큼 허락합니다."""
    positions = state.get("positions", {})
    kr_count = sum(1 for k in positions.keys() if str(k).isdigit())
    coin_count = sum(1 for k in positions.keys() if is_coin_ticker(str(k)))
    us_count = len(positions) - kr_count - coin_count

    if is_coin_ticker(str(ticker)):
        return coin_count < max_positions
    elif str(ticker).isdigit():
        return kr_count < max_positions
    else:
        return us_count < max_positions

def check_mdd_break(market_type, current_equity, state, path):
    """🛡️ 실시간 자산을 기준으로 고점 대비 5% 하락 시 매수 중단 로직"""
    peak_key = f"peak_equity_{market_type}"
    peak_equity = state.get(peak_key, current_equity)
    
    if current_equity > peak_equity:
        state[peak_key] = current_equity
        save_state(path, state)
        return True
    
    if current_equity < peak_equity * 0.95:
        print(f"  -> 🚨 [{market_type}] MDD 브레이크 발동! (고점 대비 -5% 하락). 신규 매수 차단.")
        return False
    return True


# --- Phase 5: 합산 자산 서킷(쿨다운) + 월요일 주차 고점(MDD) ---
ACCOUNT_CIRCUIT_COOLDOWN_KEY = "account_circuit_cooldown_until"
PEAK_EQUITY_TOTAL_KRW_KEY = "peak_equity_total_krw"  # 레거시 입력 이관용(쓰기 금지)
PEAK_TOTAL_EQUITY_KEY = "peak_total_equity"
LAST_RESET_WEEK_KEY = "last_reset_week"
ACCOUNT_CIRCUIT_PEAK_RESET_PENDING_KEY = "account_circuit_peak_reset_pending"
PHASE5_LAST_LOOP_TOTAL_KEY = "phase5_last_loop_total_krw"
# KIS 미장 개장 직후 합산 급등 오발동 방지 (peak 상향만 동결·MDD 판정은 유지)
PHASE5_US_OPEN_FREEZE_MINUTES = 5
PHASE5_PEAK_SPIKE_JUMP_PCT = 5.0


def _seoul_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Seoul"))


def week_label_seoul(dt: datetime | None = None) -> str:
    """ISO 연-주차 문자열 (예: ``2026-W16``). 기준: Asia/Seoul."""
    z = dt or _seoul_now()
    if z.tzinfo is None:
        z = z.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    else:
        z = z.astimezone(ZoneInfo("Asia/Seoul"))
    y, w, _ = z.isocalendar()
    return f"{y}-W{w:02d}"


def _drop_legacy_peak_key(state: dict) -> None:
    """레거시 키 ``peak_equity_total_krw`` 제거(단일 소스 유지)."""
    if PEAK_EQUITY_TOTAL_KRW_KEY in state:
        state.pop(PEAK_EQUITY_TOTAL_KRW_KEY, None)


def _migrate_legacy_peak_to_total(state: dict) -> bool:
    """레거시 ``peak_equity_total_krw`` 를 ``peak_total_equity`` 로 1회 이관 후 제거."""
    try:
        pt = float(state.get(PEAK_TOTAL_EQUITY_KEY, 0.0) or 0.0)
    except (TypeError, ValueError):
        pt = 0.0
    if pt > 0:
        _drop_legacy_peak_key(state)
        return False
    try:
        leg = float(state.get(PEAK_EQUITY_TOTAL_KRW_KEY, 0.0) or 0.0)
    except (TypeError, ValueError):
        leg = 0.0
    if leg > 0:
        state[PEAK_TOTAL_EQUITY_KEY] = leg
        _drop_legacy_peak_key(state)
        return True
    _drop_legacy_peak_key(state)
    return False


def _us_open_peak_freeze_window_kst(seoul: datetime) -> tuple[dt_time, dt_time]:
    """
    NYSE 정규장 09:30 ET 개장 직후 KST 구간 (서머타임 22:30~22:35, 표준 23:30~23:35).

    America/New_York DST 여부로 KST 창을 자동 선택한다.
    """
    ny = seoul.astimezone(ZoneInfo("America/New_York"))
    offset_h = ny.utcoffset()
    freeze_end_min = 30 + PHASE5_US_OPEN_FREEZE_MINUTES
    if offset_h is not None and int(offset_h.total_seconds()) == -4 * 3600:
        return dt_time(22, 30), dt_time(22, freeze_end_min)
    return dt_time(23, 30), dt_time(23, freeze_end_min)


def is_us_regular_open_peak_freeze_kst(dt: datetime | None = None) -> bool:
    """미장 개장 직후 API 불안정 구간 — ``peak_total_equity`` 상향 갱신만 동결."""
    seoul = dt or _seoul_now()
    if seoul.tzinfo is None:
        seoul = seoul.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    else:
        seoul = seoul.astimezone(ZoneInfo("Asia/Seoul"))
    if int(seoul.weekday()) >= 5:
        return False
    start, end = _us_open_peak_freeze_window_kst(seoul)
    t = seoul.time()
    return start <= t <= end


def phase5_peak_raise_block_reason(state: dict, current_total_krw: float, dt: datetime | None = None) -> str:
    """
    고점 **상향** 갱신을 막아야 할 때 사유 문자열, 허용 시 빈 문자열.

    * 미장 개장 직후 5분 동결
    * 직전 루프 대비 +5% 이상 급등(더티 틱)
    """
    cur = float(current_total_krw)
    if is_us_regular_open_peak_freeze_kst(dt):
        seoul = dt or _seoul_now()
        if seoul.tzinfo is None:
            seoul = seoul.replace(tzinfo=ZoneInfo("Asia/Seoul"))
        else:
            seoul = seoul.astimezone(ZoneInfo("Asia/Seoul"))
        start, end = _us_open_peak_freeze_window_kst(seoul)
        return f"us_open_freeze({start.strftime('%H:%M')}~{end.strftime('%H:%M')} KST)"

    try:
        prev = float(state.get(PHASE5_LAST_LOOP_TOTAL_KEY, 0.0) or 0.0)
    except (TypeError, ValueError):
        prev = 0.0
    if prev > 0.0 and cur > prev:
        jump_pct = (cur - prev) / prev * 100.0
        if jump_pct >= PHASE5_PEAK_SPIKE_JUMP_PCT:
            return f"equity_spike(+{jump_pct:.2f}% vs prev {prev:,.0f})"
    return ""


def get_phase5_peak_total_equity(state: dict) -> float:
    """Phase5 MDD 계산용 주차 트레일링 고점(원화) — 단일 키 ``peak_total_equity``."""
    try:
        pt = float(state.get(PEAK_TOTAL_EQUITY_KEY, 0.0) or 0.0)
    except (TypeError, ValueError):
        pt = 0.0
    if pt > 0:
        return pt
    return 0.0


def apply_phase5_trailing_week_and_cooldown(state: dict, current_total_krw: float, path: Path) -> None:
    """
    합산 총자산 기준으로 주차 고점·쿨다운 후 리셋·상향 추적을 한 번에 반영한다.

    순서
        1) 레거시 ``peak_equity_total_krw`` 가 있으면 ``peak_total_equity`` 로 1회 이주 후 삭제
        2) 서킷 쿨다운이 **끝난 뒤** ``account_circuit_peak_reset_pending`` 이면 고점을 현재 총자산으로 리셋(무한 발동 방지)
        3) **서울 기준 월요일**이고 ``last_reset_week`` 가 이번 주와 다르면 고점을 현재 총자산으로 덮어쓰고 주차 갱신
        4) 그 외 ``현재 > 고점`` 이면 고점 상향 (미장 개장 직후 5분·직전 루프 +5% 급등 시 스킵)
        5) ``phase5_last_loop_total_krw`` 에 이번 루프 합산 저장 (스파이크 필터용)
    """
    cur = float(current_total_krw)
    mutated = False
    if _migrate_legacy_peak_to_total(state):
        mutated = True

    in_cd = in_account_circuit_cooldown(state)

    if not in_cd and state.get(ACCOUNT_CIRCUIT_PEAK_RESET_PENDING_KEY) is True:
        state[PEAK_TOTAL_EQUITY_KEY] = cur
        state[ACCOUNT_CIRCUIT_PEAK_RESET_PENDING_KEY] = False
        _drop_legacy_peak_key(state)
        mutated = True
        print(f"  📌 [Phase5] 쿨다운 해제 후 고점 리셋(영점) → {cur:,.0f}원")

    seoul = _seoul_now()
    if seoul.weekday() == 0:
        wl = week_label_seoul(seoul)
        if str(state.get(LAST_RESET_WEEK_KEY, "")).strip() != wl:
            state[PEAK_TOTAL_EQUITY_KEY] = cur
            state[LAST_RESET_WEEK_KEY] = wl
            _drop_legacy_peak_key(state)
            mutated = True
            print(f"  📌 [Phase5] 월요일 주차 고점 앵커 ({wl}) → {cur:,.0f}원")

    peak = float(state.get(PEAK_TOTAL_EQUITY_KEY, 0.0) or 0.0)
    if cur > peak:
        block = phase5_peak_raise_block_reason(state, cur, seoul)
        if block:
            print(
                f"  📌 [Phase5] 고점 상향 갱신 스킵 ({block}) — "
                f"peak={peak:,.0f}원, current={cur:,.0f}원 (MDD 판정은 계속)"
            )
        else:
            state[PEAK_TOTAL_EQUITY_KEY] = cur
            _drop_legacy_peak_key(state)
            mutated = True
    elif peak <= 0.0 and cur > 0.0:
        block = phase5_peak_raise_block_reason(state, cur, seoul)
        if block:
            print(
                f"  📌 [Phase5] 고점 초기화 스킵 ({block}) — "
                f"current={cur:,.0f}원 (MDD 판정은 계속)"
            )
        else:
            state[PEAK_TOTAL_EQUITY_KEY] = cur
            _drop_legacy_peak_key(state)
            mutated = True

    state[PHASE5_LAST_LOOP_TOTAL_KEY] = cur
    mutated = True

    if mutated:
        save_state(path, state)


def in_account_circuit_cooldown(state) -> bool:
    raw = state.get(ACCOUNT_CIRCUIT_COOLDOWN_KEY)
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(str(raw))
        return datetime.now() < until
    except Exception:
        return False


def set_account_circuit_cooldown(state, path: Path, hours: float = 24.0) -> None:
    until = datetime.now() + timedelta(hours=float(hours))
    state[ACCOUNT_CIRCUIT_COOLDOWN_KEY] = until.isoformat(timespec="seconds")
    state[ACCOUNT_CIRCUIT_PEAK_RESET_PENDING_KEY] = True
    save_state(path, state)


def update_peak_equity_total_krw(state, current_krw: float, path: Path) -> float:
    """레거시 함수명 유지(내부는 ``peak_total_equity`` 단일 키만 사용)."""
    apply_phase5_trailing_week_and_cooldown(state, float(current_krw), path)
    return get_phase5_peak_total_equity(state)


def get_peak_equity_total_krw(state) -> float:
    """레거시 함수명 유지(반환값은 ``peak_total_equity``)."""
    return get_phase5_peak_total_equity(state)


def _normalize_strategy_type(strategy_type: str | None) -> str:
    s = str(strategy_type or "TREND_V8").strip().upper()
    return "SWING_FIB" if s == "SWING_FIB" else "TREND_V8"


def _normalize_market_tag(market: str | None) -> str:
    m = str(market or "KR").strip().upper()
    if m in ("KR", "US", "COIN"):
        return m
    return "KR"


_COOLDOWN_HOURS_PROFIT = 1.0
_COOLDOWN_HOURS_STOP_LOSS = 24.0
_COOLDOWN_HOURS_TIME_STOP = 24.0


def _is_time_stop_exit(reason: str) -> bool:
    r = reason or ""
    if "V8_TIME_STOP" in r or "SWING_TIME_STOP" in r:
        return True
    if "TIME_STOP" in r.upper():
        return True
    if "타임스탑" in r or ("타임" in r and "스탑" in r):
        return True
    rl = r.lower()
    return "time stop" in rl or "timestop" in rl


def _is_stop_loss_exit(reason: str, profit_rate: float | None) -> bool:
    """하드스탑·손절·컷로스 (타임스탑 제외)."""
    if _is_time_stop_exit(reason):
        return False
    r = reason or ""
    if "하드스탑" in r or "손절" in r:
        return True
    if "지하실" in r or "좀비" in r:
        return True
    rl = r.lower()
    if "hard stop" in rl or "stop loss" in rl or "cut loss" in rl:
        return True
    if "hard" in rl and "stop" in rl:
        return True
    try:
        if profit_rate is not None and float(profit_rate) < 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


def _is_profit_exit(reason: str) -> bool:
    """익절·트레일링·분할 익절 등 빠른 재진입 대상."""
    r = reason or ""
    rl = r.lower()
    if "scale-out" in rl or "scale out" in rl or "분할 익절" in r:
        return True
    if "swing-sell" in rl or "[swing-sell]" in rl:
        return True
    if "익절" in r or "take profit" in rl:
        return True
    if "샹들리에" in r or "chandelier" in rl:
        return True
    if "트레일" in r or "trailing" in rl:
        return True
    if "lock" in rl or "락" in r:
        return True
    if "추세 종료" in r:
        return True
    return False


def _classify_exit_cooldown_bucket(reason: str, profit_rate: float | None) -> str:
    """
    매도 사유 → 쿨다운 버킷.

    * ``profit`` — 익절·트레일링·분할 익절 (1h)
    * ``time_stop`` — 타임스탑 (24h)
    * ``stop_loss`` — 하드스탑·손절 (24h)
    """
    if _is_time_stop_exit(reason):
        return "time_stop"
    if _is_stop_loss_exit(reason, profit_rate):
        return "stop_loss"
    if _is_profit_exit(reason):
        return "profit"
    try:
        if profit_rate is not None and float(profit_rate) >= 0:
            return "profit"
    except (TypeError, ValueError):
        pass
    return "stop_loss"


def compute_ticker_cooldown_hours(
    *,
    reason: str,
    profit_rate: float | None = None,
    strategy_type: str | None = None,
    market: str | None = None,
    remaining_qty: float | None = None,
) -> float | None:
    """
    Layer 2 ``ticker_cooldowns`` 에 적용할 시간(h).

    * ``remaining_qty > 0`` (분할 익절 후 잔량 보유) → ``None`` (부여 안 함).
    * 전량 청산 시 매도 사유별: 익절 1h / 손절·타임스탑 24h.
    """
    if remaining_qty is not None:
        try:
            if float(remaining_qty) > 0.0:
                return None
        except (TypeError, ValueError):
            pass

    bucket = _classify_exit_cooldown_bucket(reason or "", profit_rate)
    if bucket == "profit":
        return _COOLDOWN_HOURS_PROFIT
    if bucket == "time_stop":
        return _COOLDOWN_HOURS_TIME_STOP
    return _COOLDOWN_HOURS_STOP_LOSS


def set_ticker_cooldown_after_sell(
    state: dict,
    ticker: str,
    reason: str = "",
    *,
    profit_rate: float | None = None,
    strategy_type: str | None = None,
    market: str | None = None,
    remaining_qty: float | None = None,
) -> None:
    """``ticker_cooldowns[ticker]`` 에 매도 시각 + 사유·전략·시장별 쿨다운 만료 시각(ISO)을 기록."""
    key = str(ticker or "").strip()
    if not key:
        return
    hrs = compute_ticker_cooldown_hours(
        reason=reason or "",
        profit_rate=profit_rate,
        strategy_type=strategy_type,
        market=market,
        remaining_qty=remaining_qty,
    )
    if hrs is None:
        print(f"  [쿨다운 패스] {key} - 분할 익절 잔여 물량 있음")
        return
    until = datetime.now() + timedelta(hours=float(hrs))
    state.setdefault("ticker_cooldowns", {})[key] = until.isoformat(timespec="seconds")
    st_disp = _normalize_strategy_type(strategy_type)
    hrs_disp = int(hrs) if hrs == int(hrs) else hrs
    print(f"  [쿨다운 적용] {key} | 전략: {st_disp} | 사유: {reason} | 차단: {hrs_disp}시간")


def in_ticker_cooldown(state: dict, ticker: str) -> bool:
    """현재 시각이 ``ticker_cooldowns`` 만료 시각 이전이면 True."""
    key = str(ticker or "").strip()
    raw = (state.get("ticker_cooldowns") or {}).get(key)
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(str(raw))
        return datetime.now() < until
    except Exception:
        return False


def ticker_cooldown_human(state: dict, ticker: str) -> str:
    """로그용 만료 시각 문자열."""
    key = str(ticker or "").strip()
    raw = (state.get("ticker_cooldowns") or {}).get(key)
    return str(raw) if raw else ""
