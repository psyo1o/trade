# -*- coding: utf-8 -*-
"""
장부(JSON) 입출력과 **시장별·합산** 리스크 플래그.

``bot_state.json`` 스키마(일부)
    * ``positions`` — 티커별 ``buy_p``, ``sl_p``, ``tier``, ``qty``(실계좌·매수 시 동기화, 주말 GUI 표시) 등.
    * ``cooldown`` — 매수 직후 짧은 재진입 방지(분 단위, ``in_cooldown``).
    * ``ticker_cooldowns`` — **매도 후** 티커별 절대 만료 시각(ISO). 톱날 재진입 방지.
    * ``peak_equity_{KR|US|COIN}`` — 시장별 MDD 브레이크용 고점.
    * ``peak_total_equity`` / ``last_reset_week`` / ``account_circuit_peak_reset_pending`` /
      ``peak_equity_total_krw``(레거시 미러) / ``account_circuit_cooldown_until`` — Phase5 합산 서킷(월요일 주차 MDD).

V7.1: 모든 보유 종목을 액티브 매매·샹들리에 동일 적용. 포지션별 ``scale_out_done`` 은
``load_state`` 시 기본값 ``false`` 로 보강된다.
"""
import json
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

def load_state(path: Path):
    """
    bot_state.json 로드. 파일 없음 / 빈 파일 / JSON 깨짐 시 기본 장부 반환(크래시 방지).
    깨진 내용은 *_corrupt_타임스탬프.bak 으로 한 번 복사해 둔다.
    """
    empty: dict = {"positions": {}, "cooldown": {}, "ticker_cooldowns": {}, "last_kis_display_snapshot": {}}
    if not path.exists():
        return empty
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return empty
    if not raw.strip():
        try:
            path.write_text(json.dumps(empty, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        return empty
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
        try:
            path.write_text(json.dumps(empty, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        print(
            "⚠️ [guard] bot_state.json 이 비어 있거나 JSON이 깨져 있어 기본 장부로 재생성했습니다."
            + (f" (백업: {bak.name})" if bak else "")
        )
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("positions", {})
    data.setdefault("cooldown", {})
    data.setdefault("ticker_cooldowns", {})
    # KIS 최종 성공 조회 시점의 국·미 표시용 스냅샷(주말 점검 시 GUI/텔레 재사용)
    data.setdefault("last_kis_display_snapshot", {})
    # V7.1: 분할 익절 1회 플래그(기존 장부 호환)
    for _tk, _pos in list(data.get("positions", {}).items()):
        if isinstance(_pos, dict) and "scale_out_done" not in _pos:
            _pos["scale_out_done"] = False
    return data

def save_state(path: Path, state):
    """``state`` 를 UTF-8 JSON으로 덮어쓴다. 부모 디렉터리가 없으면 생성."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

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
    coin_count = sum(1 for k in positions.keys() if str(k).startswith("KRW-"))
    us_count = len(positions) - kr_count - coin_count

    if str(ticker).startswith("KRW-"):
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
PEAK_EQUITY_TOTAL_KRW_KEY = "peak_equity_total_krw"  # 레거시·GUI·스크립트 호환용 미러
PEAK_TOTAL_EQUITY_KEY = "peak_total_equity"
LAST_RESET_WEEK_KEY = "last_reset_week"
ACCOUNT_CIRCUIT_PEAK_RESET_PENDING_KEY = "account_circuit_peak_reset_pending"


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


def _mirror_legacy_peak_total_krw(state: dict) -> None:
    """``peak_total_equity`` 를 ``peak_equity_total_krw`` 에 복제(adjust_capital·구버전 호환)."""
    if PEAK_TOTAL_EQUITY_KEY in state and state.get(PEAK_TOTAL_EQUITY_KEY) is not None:
        try:
            state[PEAK_EQUITY_TOTAL_KRW_KEY] = float(state[PEAK_TOTAL_EQUITY_KEY])
        except (TypeError, ValueError):
            pass


def _migrate_legacy_peak_to_total(state: dict) -> bool:
    """``peak_total_equity`` 가 비어 있고 레거시 고점만 있으면 복사. 변경 시 True."""
    try:
        pt = float(state.get(PEAK_TOTAL_EQUITY_KEY, 0.0) or 0.0)
    except (TypeError, ValueError):
        pt = 0.0
    if pt > 0:
        return False
    try:
        leg = float(state.get(PEAK_EQUITY_TOTAL_KRW_KEY, 0.0) or 0.0)
    except (TypeError, ValueError):
        leg = 0.0
    if leg > 0:
        state[PEAK_TOTAL_EQUITY_KEY] = leg
        return True
    return False


def get_phase5_peak_total_equity(state: dict) -> float:
    """Phase5 MDD 계산용 주차 트레일링 고점(원화). 레거시 키만 있으면 그 값을 반환."""
    try:
        pt = float(state.get(PEAK_TOTAL_EQUITY_KEY, 0.0) or 0.0)
    except (TypeError, ValueError):
        pt = 0.0
    if pt > 0:
        return pt
    try:
        return float(state.get(PEAK_EQUITY_TOTAL_KRW_KEY, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def apply_phase5_trailing_week_and_cooldown(state: dict, current_total_krw: float, path: Path) -> None:
    """
    합산 총자산 기준으로 주차 고점·쿨다운 후 리셋·상향 추적을 한 번에 반영한다.

    순서
        1) 레거시 ``peak_equity_total_krw`` → ``peak_total_equity`` 이주
        2) 서킷 쿨다운이 **끝난 뒤** ``account_circuit_peak_reset_pending`` 이면 고점을 현재 총자산으로 리셋(무한 발동 방지)
        3) **서울 기준 월요일**이고 ``last_reset_week`` 가 이번 주와 다르면 고점을 현재 총자산으로 덮어쓰고 주차 갱신
        4) 그 외 ``현재 > 고점`` 이면 고점 상향
    """
    cur = float(current_total_krw)
    mutated = False
    if _migrate_legacy_peak_to_total(state):
        mutated = True

    in_cd = in_account_circuit_cooldown(state)

    if not in_cd and state.get(ACCOUNT_CIRCUIT_PEAK_RESET_PENDING_KEY) is True:
        state[PEAK_TOTAL_EQUITY_KEY] = cur
        state[ACCOUNT_CIRCUIT_PEAK_RESET_PENDING_KEY] = False
        _mirror_legacy_peak_total_krw(state)
        mutated = True
        print(f"  📌 [Phase5] 쿨다운 해제 후 고점 리셋(영점) → {cur:,.0f}원")

    seoul = _seoul_now()
    if seoul.weekday() == 0:
        wl = week_label_seoul(seoul)
        if str(state.get(LAST_RESET_WEEK_KEY, "")).strip() != wl:
            state[PEAK_TOTAL_EQUITY_KEY] = cur
            state[LAST_RESET_WEEK_KEY] = wl
            _mirror_legacy_peak_total_krw(state)
            mutated = True
            print(f"  📌 [Phase5] 월요일 주차 고점 앵커 ({wl}) → {cur:,.0f}원")

    peak = float(state.get(PEAK_TOTAL_EQUITY_KEY, 0.0) or 0.0)
    if peak <= 0.0 or cur > peak:
        state[PEAK_TOTAL_EQUITY_KEY] = cur
        _mirror_legacy_peak_total_krw(state)
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
    """레거시 이름 유지: 주차 MDD·쿨다운 리셋 규칙을 포함한 합산 고점 갱신 후 고점 반환."""
    apply_phase5_trailing_week_and_cooldown(state, float(current_krw), path)
    return get_phase5_peak_total_equity(state)


def get_peak_equity_total_krw(state) -> float:
    return get_phase5_peak_total_equity(state)


def sell_reason_cooldown_hours(reason: str, profit_rate: float | None = None) -> float:
    """
    매도 사유별 **재매수 금지** 시간(시간 단위).

    * 타임스탑 / 손절·하드스탑 계열 → **120시간**(5일)
    * 익절·샹들리에 등 그 외 → **48시간**(2일)
    * ``수동`` 이 포함되면: ``profit_rate < 0`` 이면 120h, 아니면 48h
    """
    r = reason or ""
    if "타임" in r and "스탑" in r:
        return 120.0
    if "타임스탑" in r:
        return 120.0
    if "하드스탑" in r or "손절" in r:
        return 120.0
    rl = r.lower()
    if "time stop" in rl or "timestop" in rl:
        return 120.0
    if "hard stop" in rl or "stop loss" in rl:
        return 120.0
    if "수동" in r or "manual" in rl:
        try:
            if profit_rate is not None and float(profit_rate) < 0:
                return 120.0
        except (TypeError, ValueError):
            pass
        return 48.0
    return 48.0


def set_ticker_cooldown_after_sell(
    state: dict,
    ticker: str,
    reason: str = "",
    *,
    profit_rate: float | None = None,
) -> None:
    """``ticker_cooldowns[ticker]`` 에 매도 시각 + 사유별 쿨다운 만료 시각(ISO)을 기록."""
    key = str(ticker or "").strip()
    if not key:
        return
    hrs = sell_reason_cooldown_hours(reason or "", profit_rate)
    until = datetime.now() + timedelta(hours=hrs)
    state.setdefault("ticker_cooldowns", {})[key] = until.isoformat(timespec="seconds")


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
