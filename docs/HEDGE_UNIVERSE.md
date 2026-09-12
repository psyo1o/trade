# 하락장 헷지 유니버스 (안전자산)

_티커 하드코딩의 **단일 출처**: [`strategy/hedge_universe.py`](../strategy/hedge_universe.py)  
검색: `HEDGE_UNIVERSE`, `HEDGE_ASSETS`, `하락장 헷지`

---

## 종목 목록 (기본값)

| 시장 | 코드 | 한글/설명 |
|------|------|-----------|
| **KR** | `261240` | KODEX 미국달러선물 |
| **KR** | `411060` | ACE KRX금현물 |
| **KR** | `304660` | KODEX 미국30년국채울트라선물(H) |
| **US** | `GLD` | SPDR Gold Shares |
| **US** | `TLT` | iShares 20+ Year Treasury Bond ETF |
| **US** | `UUP` | Invesco DB US Dollar Index Bullish Fund |
| **COIN** | `PAXG` | Paxos Gold (바이낸스 `USDT-PAXG` / 업비트 `KRW-PAXG`) |
| **COIN** | `XAUT` | Tether Gold (`USDT-XAUT` / `KRW-XAUT`) |

코드·이름을 바꿀 때는 `HEDGE_ASSETS_KR` / `HEDGE_ASSETS_US` / **`HEDGE_ASSETS_COIN`** 튜플만 수정한 뒤 **봇/GUI 재시작**합니다. (`config.json` 항목 아님)

---

## 매수 루프 동작

구현: execution/market_cycles/kr_buy_cycle.py, us_buy_cycle.py, **coin_buy_cycle.py**  
호출: execution/market_cycles/kr_cycle.py, us_cycle.py  
하위 호환: 
un_bot._run_kr_buy_cycle / _run_us_buy_cycle → 위 모듈 위임

1. **후보 병합** — 스캔/유니버스 타겟에 헷지 종목을 항상 추가 (_merge_hedge_into_buy_targets).  
   **BULL/SIDEWAYS 등 정상 장**에서는 병합된 헷지도 **일반 V8/SWING** 시그널 경로로 검토한다.
2. **Phase 4** — market_buy_allowed[시장] == false 이면 **신규 매수 전면 차단**(헷지 포함·현금 관망).  
   로그: 🚨 [Phase 4 발동] {시장} 신규 매수 전면 차단 (헷지 포함 · 현금 관망).
3. **BEAR 날씨** — buy_cycle에서 **즉시 return**. 헷지 포함 전면 차단(현금 관망).  
   로그: 📌 [KR|US|COIN] BEAR 날씨 — 신규 매수 전면 차단 (헷지 포함 · 현금 관망).
4. **MAX_POSITIONS** — 헷지 티커 **우회 없음** (_can_open_new_respecting_hedge_bypass ≡ can_open_new).
5. **Phase 3 AI** — 헷지도 일반 종목과 동일하게 evaluate_false_breakout_filter 적용.
6. **지수 급락** — INDEX_CRASH_* 시 시장 전체 매수 중단(헷지 예외 없음).

Phase 4 거시 지표 자체는 [strategy/macro_guard.py](../strategy/macro_guard.py) · [pi/macro_data.py](../api/macro_data.py) 와 동일합니다.  
**KR 환율 차단(2026-06):** 20일 Z-Score ≥ 2.0 **이면서** 당일 환율 상승(is_rising)일 때만 일반 주식 신규 매수 차단. 실시간 spot은 yfinance 1분봉 또는 261240 ETF 프록시.

---

## 그 외 게이트 (헷지도 동일)

- KOSPI/S&P/BTC **지수 급락** — 시장 전체 차단(헷지 예외 없음)
- **BEAR** / **Phase4** — 신규 매수 전면 차단(헷지 포함·현금 관망)
- **섹터 락**, 쿨다운, V8/스윙 **진입 시그널**
- **갭상승 5%**(국장)

---

## 관련 파일

| 파일 | 역할 |
|------|------|
| `strategy/hedge_universe.py` | ★ 티커 정의 |
| `run_bot.py` | 헷지 헬퍼·매수 루프 |
| `execution/market_cycles/kr_cycle.py` | 국장 사이클 → `_run_kr_buy_cycle` |
| `execution/market_cycles/us_cycle.py` | 미장 사이클 → `_run_us_buy_cycle` |
| `strategy/macro_guard.py` | Phase 4 `market_buy_allowed` |
| `README.md` | 운영·Phase 설명 |
| `run_gui.py` | **매매·전략 안내** 탭·보유표/장부 **전략** 열 (V8/스윙/**헷지**) |
