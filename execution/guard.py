# -*- coding: utf-8 -*-
"""
장부(JSON) 입출력과 **시장별·합산** 리스크 플래그.

``bot_state.json`` 스키마(일부)
    * ``positions`` — 티커별 ``buy_p``, ``sl_p``, ``tier``, ``qty``(실계좌·매수 시 동기화, 주말 GUI 표시) 등.
    * ``cooldown`` — 매수 직후 짧은 재진입 방지(분 단위, ``in_cooldown``).
    * ``ticker_cooldowns`` — **매도 후** 티커별 절대 만료 시각(ISO). 톱날 재진입 방지.
    * ``peak_equity_{KR|US|COIN}`` — 시장별 MDD 브레이크용 고점.
    * ``peak_equity_total_krw`` / ``account_circuit_cooldown_until`` — Phase5 합산 서킷.

``CORE_ASSETS`` 는 **프로젝트 전역 단일 정의**이다. ``run_bot``·``strategy.rules`` 등은 여기서 import 한다.
``can_open_new`` 에서 포지션 슬롯 카운트에서 제외되는 대장주·코어 코인 묶음이다.
"""
import json
from pathlib import Path
from datetime import datetime, timedelta

CORE_ASSETS = [
    "005930", "000660", "QQQ", "NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "META", "GOOGL",
    "KRW-BTC", "KRW-ETH", "KRW-SOL",
]
# CORE_ASSETS 중 업비트 KRW 마켓 코어만 (스캔 폴백·룰 분기 등에서 재사용)
CORE_COIN_ASSETS = tuple(t for t in CORE_ASSETS if str(t).startswith("KRW-"))

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
    """[시장별 독립 슬롯] 메인에서 설정한 종목 수(max_positions)만큼 허락합니다!
    단, CORE_ASSETS(대장주·코어 코인 BTC/ETH/SOL)는 시장별 포지션 카운트에서 제외합니다.
    """
    positions = state.get("positions", {})
    
    # 1. 봇의 장부에서 시장별로 개수를 따로 셉니다. (코어 자산 제외)
    kr_count = sum(1 for k in positions.keys() if k.isdigit() and k not in CORE_ASSETS)
    coin_count = sum(1 for k in positions.keys() if k.startswith("KRW-") and k not in CORE_ASSETS)
    us_count = len(positions) - kr_count - coin_count
    # 미장 카운트 보정 (전체 - 국장 - 코인 - 미장코어)
    us_core_count = sum(1 for k in positions.keys() if k in CORE_ASSETS and not k.isdigit())
    us_count = us_count - us_core_count
    
    # 2. 들어온 티커(ticker)가 어느 시장인지 확인하고, 메인이 요청한 제한(max_positions)과 비교!
    # (코어 자산인 경우 무조건 True 반환 → 제한 없이 추가 매수 가능)
    if ticker in CORE_ASSETS:
        return True

    if ticker.startswith("KRW-"):
        return coin_count < max_positions
    elif ticker.isdigit():
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


# --- Phase 5: 합산 자산 서킷(쿨다운) ---
ACCOUNT_CIRCUIT_COOLDOWN_KEY = "account_circuit_cooldown_until"
PEAK_EQUITY_TOTAL_KRW_KEY = "peak_equity_total_krw"


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
    save_state(path, state)


def update_peak_equity_total_krw(state, current_krw: float, path: Path) -> float:
    """합산 평가금(KRW) 고점 갱신 후 고점 값 반환."""
    cur = float(current_krw)
    peak = float(state.get(PEAK_EQUITY_TOTAL_KRW_KEY, 0.0) or 0.0)
    if peak <= 0.0 or cur > peak:
        peak = cur
        state[PEAK_EQUITY_TOTAL_KRW_KEY] = peak
        save_state(path, state)
    return float(state.get(PEAK_EQUITY_TOTAL_KRW_KEY, cur))


def get_peak_equity_total_krw(state) -> float:
    return float(state.get(PEAK_EQUITY_TOTAL_KRW_KEY, 0.0) or 0.0)


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
