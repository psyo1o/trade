# 잔고 조회 — 설계 (주문 멱등과 분리)

## 한 줄 요약

| 구분 | 멱등? | 이 프로젝트 처리 |
|------|--------|------------------|
| **잔고 API 조회** | 원래 멱등 (읽기) | `execution/balance_read.py` TTL 캐시 |
| **KIS 전역 호출** | — | `api/kis_rate_limit.py` 슬라이딩 윈도우 (EGW00201 완화) |
| **주문 API** | 멱등 아님 | `execution/idempotency.py` 키·in-flight·clientOrderId |

조회를 여러 번 해도 계좌가 바뀌지 않지만, **한 슬라이스 안에서 before/after 를 서로 다른 시점 응답으로 비교**하면 “체결됐는데 실패”·“실패했는데 체결” 오판이 난다. 그래서 **스냅샷 + 주문 후에만 refresh** 로 단순화했다.

## 규칙 (3가지)

1. **TWAP·체결 검증 시작** — `stock_qty(..., refresh=False)`  
   - TTL 캐시. 같은 시장(KR/US/COIN)은 한 응답을 재사용.

2. **주문 제출 후 잔고 확인** — `stock_qty(..., refresh=True)`  
   - 캐시 무시, API 1회. `balance_qty_fn` 이 이 경로만 탄다.

3. **사이클 시작·주문 성공/실패 직후** — `bal_read.invalidate()` 또는 `invalidate(market)`  
   - 다음 티커/다음 슬라이스가 **낡은 잔고**를 쓰지 않게 한다. US 시 `get_us_cash_real` 캐시도 함께 삭제.

## 코드 위치

```
api/kis_rate_limit.py
  wait_for_slot() — KIS HTTP 직전 전역 스로틀

execution/balance_read.py
  kr_stock_qty / us_stock_qty / coin_stock_qty
  stock_qty(market, ticker, refresh=...)
  invalidate(market=None)

api/kis_api.py
  _fetch_kr_balance_with_backoff / _fetch_us_positions_with_backoff
  get_us_cash_real (TTL 캐시, refresh=True 시 무효화)

run_bot.py
  _kis_balance_qty_for_ticker → bal_read.stock_qty
  _kis_post_trade_balance_pause — 매도 직후 대기(기본 2.5초)
  TWAP: qty_before=refresh False, _qty_now=refresh True
  _prepare_cycle_state → bal_read.invalidate()
  build_account_snapshot_for_report → kr/us/coin_balance_for_report
  get_safe_balance("KR"|"US"|"COIN") → balance_read
  manual_sell 체결 후 → bal_read.invalidate(시장)

execution/market_cycles/kr_cycle.py, us_cycle.py
  매도 루프 현재가 — 잔고 output1 prpr (fetch_price 생략)

run_gui.py
  BalanceUpdaterThread → build_account_snapshot(..., fresh_balances=True)
  heartbeat_report → fresh_balances=False (TTL 공유, API 절감)
```

## 주문 멱등과의 관계

```
[사이클 시작] invalidate 전체
     ↓
[슬라이스] qty_before (캐시 OK)
     ↓
[주문 API] idempotency order_key
     ↓
[검증]     qty_after (refresh=True)
     ↓
[성공/실패] invalidate(시장) + (실패 시) persist_idempotency
```

**장부 `positions`** 는 여전히 `sync_positions`·GUI 스냅샷이 담당. 잔고 API는 **체결 여부 판정 전용**으로만 쓴다.

## 복잡해 보였던 이유 (정리)

- 슬라이스마다 `get_balance_with_retry()` 를 **재시도마다** 호출 → API 폭주·수량 들쭉날쭉.
- KIS `output1` 파싱이 여러 곳에 흩어져 있음 → `kis_balance_stock_qty` 한곳 + `balance_read` 진입으로 통일.
- 잔고·시세·일봉·US 예수금이 **키 단위 한도(≈20건/초)** 를 공유 → 전역 `kis_rate_limit` 추가.

## 설정

### balance_read

- 기본 TTL: **20초** (`BOT_KIS_BALANCE_CACHE_TTL_SEC`)
- 최소 API 간격: **4초** (`BOT_KIS_BALANCE_MIN_INTERVAL_SEC`)
- 한도·오류 시: 직전 정상 응답 **stale** 최대 90초 (`BOT_KIS_BALANCE_STALE_SEC`)
- 체결 검증 허용 오차: **85%** (`idempotency.balance_suggests_fill` / `balance_suggests_sell_fill`)

### kis_rate_limit (전역)

| 변수 | 실전 기본 | 설명 |
|------|-----------|------|
| `BOT_KIS_MAX_CALLS_PER_SEC` | 12 | 20건/초 한도 대비 여유 |
| `BOT_KIS_MAX_CALLS_PER_SEC_MOCK` | 0.8 | 모의투자 |
| `BOT_KIS_RATE_LIMIT_COOLDOWN_SEC` | 6 | EGW00201 후 재시도 전 대기 |

### 기타

- `BOT_KIS_US_CASH_CACHE_SEC` — `get_us_cash_real` TTL (기본 20초)
- `BOT_KIS_POST_SELL_DELAY_SEC` — 매도 직후 잔고 조회 전 pause (기본 2.5초)
- `config.json` `kis_ohlcv_min_interval_sec` — 국장 KIS 일봉 간격 (기본 0.35초)

## KIS 잔고 `on_trade` (표시 vs 매매)

봇 전용 매매 가정 시 **평상시 GUI·사이클은 장부+현재가**로 예수·총평을 보고, **실 KIS 잔고**는 매수 창·체결·강제 새로고침·입출금 때만 호출한다.

- 정책: `execution/balance_policy.py`, `kis_balance_sync_mode` in `config.json`
- 장부 합산: `services/ledger_valuation.py` (`coalesce_ledger_kis_labels` — 예수·총평 이중 합산 방지)
- Phase5·서킷·쿨다운: [`../PHASE5_ACCOUNT_CIRCUIT.md`](../PHASE5_ACCOUNT_CIRCUIT.md)
- **GUI 강제 새로고침·로그·비장중 방어:** [`../KIS_GUI_DISPLAY.md`](../KIS_GUI_DISPLAY.md)

`get_balance_with_retry()` / `get_us_positions_with_retry()` 는 내부적으로 `balance_read.kr_balance_raw` / `us_balance_raw` 를 탄다. `refresh=True` 이면 실 API(전역 스로틀·백오프·캐시 갱신).

### 로그 접두사 (GUI)

| 접두사 | 의미 |
|--------|------|
| `🔁 [KIS 강제 새로고침]` | KIS 실조회 + `last_kis_display_snapshot` 저장 |
| `[표시] 장부+시세` | KIS 국·미 잔고 생략, 직전 KIS 스냅샷 예수 + 장부 보유평가 |
| `[잔고 캐시-KR]` / `[잔고 캐시-US]` | 한도·간격 미만 — stale 재사용 |
