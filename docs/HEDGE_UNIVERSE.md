# 하락장 헷지 유니버스 (안전자산)

_티커 하드코딩의 **단일 출처**: [`strategy/hedge_universe.py`](../strategy/hedge_universe.py)  
검색: `HEDGE_UNIVERSE`, `HEDGE_ASSETS`, `하락장 헷지`

---

## 종목 목록 (기본값)

| 시장 | 코드 | 한글/설명 |
|------|------|-----------|
| **KR** | `133690` | TIGER 미국나스닥100 |
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

구현: `execution/market_cycles/kr_buy_cycle.py`, `us_buy_cycle.py`, **`coin_buy_cycle.py`**  
호출: `execution/market_cycles/kr_cycle.py`, `us_cycle.py`  
하위 호환: `run_bot._run_kr_buy_cycle` / `_run_us_buy_cycle` → 위 모듈 위임

1. **후보 병합** — 스캔/유니버스 타겟에 헷지 3종을 항상 추가 (`_merge_hedge_into_buy_targets`).
2. **Phase 4** — `market_buy_allowed[시장] == false` 이면 일반 종목을 제거하고 헷지만 남김.  
   로그(KR/US): `🚨 [Phase 4 발동] 주식 매수 차단 -> …`  
   로그(COIN): `🚨 [Phase 4 발동] 일반 코인 매수 차단 -> 하락장 헷지 자산(금 토큰)만 매수 검토`
3. **MAX_POSITIONS** — 헷지 티커는 슬롯 한도 검사 우회 (`_can_open_new_respecting_hedge_bypass`).  
   예수금 부족·`portfolio_heat_max_pct` 차단은 **예외 없음**.
4. **Phase 3 AI** — 헷지는 `evaluate_false_breakout_filter` 미호출, `false_breakout_prob=0` 처리.
5. **지수 급락·BEAR·V8/스윙 시그널** — Phase4 **헷지 전용 모드**(`hedge_only`)일 때만 예외:  
   KOSPI/S&P `INDEX_CRASH_*` 차단 무시, `HEDGE_PHASE4` 방어 진입(V8/스윙·갭 필터 생략).

Phase 4 거시 지표 자체는 [`strategy/macro_guard.py`](../strategy/macro_guard.py) · [`api/macro_data.py`](../api/macro_data.py) 와 동일합니다.  
헷지는 **차단된 시장 안에서만** 살아남는 예외 경로입니다.

---

## 그 외 게이트 (헷지도 동일)

- KOSPI/S&P **지수 급락** — 일반 모드만 차단; Phase4 **헷지 전용**이면 예외
- **BEAR** 날씨 V8 차단 — 헷지 전용 모드에서 `HEDGE_PHASE4` 진입으로 예외
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
