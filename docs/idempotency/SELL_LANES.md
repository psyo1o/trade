# 매도 lane — 멱등 키 설계

`order_key` 의 `side` 자리에 lane 을 붙입니다.

```
{MARKET}:{TICKER}:sell:{lane}:{cycle_tag}:{slice_index}
```

예: `KR:005930:sell:swing_half:202605281430:0`

## lane 목록

| lane | 트리거 | sell_inflight | TWAP 슬라이스 |
|------|--------|---------------|----------------|
| `swing_half` | `check_swing_exit` → HALF | ✅ | 0만 |
| `swing_full` | `check_swing_exit` → FULL | ✅ | 0만 |
| `scale_out` | V8 Scale-Out 50% | ❌ (슬라이스 키만) | 0..n-1 |
| `exit` | 타임스탑·하드스탑·샹들리에·스윙 트레일 전량 | ✅ | 0만 |
| `manual` | GUI 수동 매도 | ✅ | 0 (부분도 0) |
| `phase5` | 합산 서킷 `manual_sell` 위임 | ✅ | 0 |

## 잔고 검증

- **KIS:** `kis_balance_stock_qty` — 매도 전후 **감소량** ≥ 기대 수량×0.85
- **코인:** `coin_base_qty_from_balances` 동일

## 바이낸스

- `binance_client_order_id(order_key)` → `market_sell_base(..., new_client_order_id=)`
