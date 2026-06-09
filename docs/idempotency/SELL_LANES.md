# 매도 lane — 멱등 키 설계

`order_key` 의 `side` 자리에 lane 을 붙입니다.

```
{MARKET}:{TICKER}:sell:{lane}:{cycle_tag}:{slice_index}
```

예: `KR:005930:sell:swing_half:202605281430:0`

## lane 목록

| lane | 트리거 | sell_inflight | TWAP 슬라이스 |
|------|--------|---------------|----------------|
| `swing_half` | `check_swing_exit` → HALF | ✅ | 0만 — 체결·정합 시 **`scale_out_done=True`** (`post_partial_ledger`) |
| `swing_full` | `check_swing_exit` → FULL | ✅ | 0만 |
| `scale_out` | **TREND_V8** 1차 Scale-Out 50% (`entry_atr×3.0`) — **SWING_FIB 미사용** | ❌ (슬라이스 키만) | 0..n-1 |
| `scale_out_2` | **TREND_V8** 2차 Scale-Out (`entry_atr×6.0`, 1차 후 잔량 50%) | ❌ | 0..n-1 |
| `exit` | 타임스탑·하드스탑·샹들리에·스윙 트레일 전량 | ✅ | 0만 |
| `manual` | GUI 수동 매도 | ✅ | 0 (부분도 0) |
| `phase5` | 합산 서킷 `manual_sell` 위임 | ✅ | 0 |

**COIN `SWING_FIB`:** `swing_full` 중 **기술바닥 이탈**(`스윙 기술바닥 이탈 …`)은 진입 **2h 미만** + 수익 **-3% 초과** 시 `coin_cycle`에서 **유예**(lane 미발행). **-3% 이하** 하드컷·**2h 이후**·HALF·5MA·RSI FULL은 유예 없음. 코드: `run_bot._coin_swing_entry_noise_defers_tech_floor_full`. README §5-3 · `docs/PHASE5_ACCOUNT_CIRCUIT.md` §10.

## 잔고 검증

- **KIS:** `kis_balance_stock_qty` — 매도 전후 **감소량** ≥ 기대 수량×0.85
- **코인:** `coin_base_qty_from_balances` 동일

## 바이낸스

- `binance_client_order_id(order_key)` → `market_sell_base(..., new_client_order_id=)`
