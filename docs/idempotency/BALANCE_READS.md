# 잔고 조회 — 설계 (주문 멱등과 분리)

## 한 줄 요약

| 구분 | 멱등? | 이 프로젝트 처리 |
|------|--------|------------------|
| **잔고 API 조회** | 원래 멱등 (읽기) | `execution/balance_read.py` TTL 캐시 |
| **주문 API** | 멱등 아님 | `execution/idempotency.py` 키·in-flight·clientOrderId |

조회를 여러 번 해도 계좌가 바뀌지 않지만, **한 슬라이스 안에서 before/after 를 서로 다른 시점 응답으로 비교**하면 “체결됐는데 실패”·“실패했는데 체결” 오판이 난다. 그래서 **스냅샷 + 주문 후에만 refresh** 로 단순화했다.

## 규칙 (3가지)

1. **TWAP·체결 검증 시작** — `stock_qty(..., refresh=False)`  
   - 12초 TTL 캐시. 같은 시장(KR/US/COIN)은 한 응답을 재사용.

2. **주문 제출 후 잔고 확인** — `stock_qty(..., refresh=True)`  
   - 캐시 무시, API 1회. `balance_qty_fn` 이 이 경로만 탄다.

3. **사이클 시작·주문 성공/실패 직후** — `bal_read.invalidate()` 또는 `invalidate(market)`  
   - 다음 티커/다음 슬라이스가 **낡은 잔고**를 쓰지 않게 한다.

## 코드 위치

```
execution/balance_read.py
  kr_stock_qty / us_stock_qty / coin_stock_qty
  stock_qty(market, ticker, refresh=...)
  invalidate(market=None)

run_bot.py
  _kis_balance_qty_for_ticker → bal_read.stock_qty
  TWAP: qty_before=refresh False, _qty_now=refresh True
  _prepare_cycle_state → bal_read.invalidate()
  build_account_snapshot_for_report → kr/us/coin_balance_for_report
  get_safe_balance("KR"|"US"|"COIN") → balance_read
  manual_sell 체결 후 → bal_read.invalidate(시장)

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

## 설정

- 기본 TTL: **12초** (`balance_read._DEFAULT_TTL_SEC`)
- 체결 검증 허용 오차: **85%** (`idempotency.balance_suggests_fill` / `balance_suggests_sell_fill`)
