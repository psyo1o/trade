import json
from pathlib import Path
from datetime import datetime, timedelta

def load_state(path: Path):
    if not path.exists():
        return {"positions": {}, "cooldown": {}}
    return json.loads(path.read_text(encoding="utf-8"))

def save_state(path: Path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def in_cooldown(state, code, minutes=120):
    cd = state.get("cooldown", {}).get(code)
    if not cd:
        return False
    try:
        t = datetime.fromisoformat(cd)
        return datetime.now() < t + timedelta(minutes=minutes)
    except:
        return False

def set_cooldown(state, code):
    state.setdefault("cooldown", {})[code] = datetime.now().isoformat(timespec="seconds")

def can_open_new(ticker, state, max_positions=5): # 메인에서 안 던져주면 기본값 5
    """[시장별 독립 슬롯] 메인에서 설정한 종목 수(max_positions)만큼 허락합니다!"""
    positions = state.get("positions", {})
    
    # 1. 봇의 장부에서 시장별로 개수를 따로 셉니다.
    kr_count = sum(1 for k in positions.keys() if k.isdigit())
    coin_count = sum(1 for k in positions.keys() if k.startswith("KRW-"))
    us_count = len(positions) - kr_count - coin_count
    
    # 2. 들어온 티커(ticker)가 어느 시장인지 확인하고, 메인이 요청한 제한(max_positions)과 비교!
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