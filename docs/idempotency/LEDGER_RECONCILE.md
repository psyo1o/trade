# 장부 정합 (positions ↔ order_idempotency)

**목적:** 체결은 됐는데 `bot_state.positions` 저장만 실패한 구멍, HALF/Scale-Out 중복 표시, GUI·봇 동시 저장을 완화한다.

## 두 장부 (의도적 분리)

| 저장소 | 역할 |
|--------|------|
| `order_idempotency` | 주문·슬라이스·lane별 **체결 기록** (재주문 방지) |
| `positions` | 전략·손절·scale_out_done 등 **포지션 상태** |

자동 1:1 연동은 하지 않는다. 대신 **사이클 시작**과 **매도 직후**에 보정한다.

## 1. 체결 성공 + save_state 실패

**증상:** 멱등에 `filled`, positions는 옛날 → 15분 루프는 멱등 때문에 재주문 없음.

**대응:**

1. `execution/ledger_apply.py` — `persist_position_set` / `persist_position_remove` (3회 저장 + reload 검증)
2. `run_bot` 매도·부분매도·매수 등록 경로에서 위 헬퍼 사용
3. 사이클 `load_state` 직후 `reconcile_positions_for_cycle(state, cycle_tag, STATE_PATH)` — 이번 15분 슬롯의 filled 매도를 positions에 반영

로그: `🔧 [장부 정합] ...`

유령·수량 불일치는 기존 `sync_positions` + 장부 점검 로그가 담당 (변경 없음).

## 2. HALF / Scale-Out — 장부만 실패

**증상:** 조건은 또 맞지만 주문은 멱등이 막음 → 로그/표시만 잠깐 중복.

**대응:**

- lane `swing_half` / `scale_out` 이 이미 filled 이면 `reconcile_ticker_lane` 후 전략 게이트 스킵
- 체결 직후 `persist_position_set` + `scale_out_done` 반영 (`swing_half`·`scale_out` lane 모두 **`scale_out_done=True`**)
- 스윙 HALF(`execution/market_cycles/*_cycle.py`) 체결 시에도 `post_partial_ledger(..., set_scale_out_done=True)` 로 즉시 러너 플래그 설정

## 3. GUI + 봇 동시 저장

- `guard.save_state` — `state_gen` 증가 + RLock
- GUI `update_max_price_if_higher` — 저장 직전 `merge_disk_if_newer` (디스크가 더 최신이면 봇 저장분 병합 후 curr_p/max_p 재적용)
- `ledger_apply.save_state_verified` — 병합 **후** 등록·삭제 예정 티커를 다시 반영 (GUI가 `state_gen`을 올린 직후 신규 매수가 지워지는 레이스 방지)

극단적 동시 편집은 이론상 남을 수 있음 → 수동은 GUI에서 **새로고침 후** 조작 권장.

## 4. 매매내역(`trade_history.json`) ↔ 장부 복구 (2026-06)

**증상:** KIS 체결·`trade_history` BUY 기록은 있는데 `positions` 저장만 실패 (또는 자동복구만 되어 SWING 메타 없음).

**두 저장소**

| 저장소 | 역할 |
|--------|------|
| `trade_history.json` | GUI 매매내역 탭·감사 로그. **2026-06~** BUY 시 장부 메타도 함께 저장 |
| `positions` | 손절·스윙 청산·익절 판단 (봇이 **여기만** 봄) |

**매수 시 매매내역에 추가되는 필드** (`run_bot._record_trade_event` → `ledger=` payload):

- `strategy_type`, `entry_fib_level`, `sl_p`, `entry_atr`, `buy_time`

**자동 경로** (`execution/sync_positions.py` + `services/trade_history_ledger.py`):

1. 실보유인데 장부 없음 → 매매내역 BUY 있으면 **`[매매내역 복구]`** (ATR `자동복구`보다 우선)
2. 장부에 있으나 `buy_time`/`entry_fib`/자동복구 티어 등 부족 → **`[매매내역 보강]`**
3. GUI **KIS 강제 새로고침** — `sync_first=True` + `force_equity_sync` 로 비장중에도 KIS 보유·복구 수행

**수동 복구 스크립트**

```bash
python scripts/restore_positions_from_trade_history.py ETN GM FAST
python scripts/restore_positions_from_trade_history.py          # 보강 필요 종목 전체
python scripts/restore_positions_from_trade_history.py --dry-run
```

SWING_FIB 이고 매매내역에 `reason: SWING_FIB` 만 있고 `entry_fib_level` 이 없으면(과거 기록):

- `buy_time` ← `timestamp` 파싱
- `entry_fib_level` ← 매수 평단·일봉으로 `infer_swing_entry_fib_from_ohlcv` (당시 HTS 값과 ±오차 가능)
- `entry_initial_risk_1r` / `sl_p` ← `register_swing_entry_risk_fields` + `swing_entry_sl_p`

**로그 태그:** `📜 [매매내역 보강]`, `🚨 [매매내역 복구]`

## 운영 체크리스트

1. 로그에 `장부 정합` / `장부 등록 최종 실패` 있는지
2. `order_idempotency` 에 `sell:*:현재cycle: filled` 인데 positions에 전량 남아 있는지 → 다음 사이클 정합 대상
3. 유령 종목은 `sync`·`[장부 점검]` 로그 확인 (멱등과 별도)
4. 체결됐는데 장부 없음 → `trade_history` BUY 확인 → 스크립트 또는 **KIS 강제 새로고침**

## 코드 위치

- `execution/ledger_apply.py` — 저장 검증·`merge_disk_if_newer` 후 pending 재적용
- `execution/idempotency.py` — `reconcile_positions_for_cycle`, `lane_has_filled_sell`
- `execution/sync_positions.py` — 자동복구·매매내역 복구/보강
- `services/trade_history_ledger.py` — 매매내역 → positions 매핑
- `scripts/restore_positions_from_trade_history.py` — 수동 일괄 보강
- `run_bot.py` — 매수 시 `ledger=` 로 매매내역 확장 기록
