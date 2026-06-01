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
- 체결 직후 `persist_position_set` + `scale_out_done` 반영

## 3. GUI + 봇 동시 저장

- `guard.save_state` — `state_gen` 증가 + RLock
- GUI `update_max_price_if_higher` — 저장 직전 `merge_disk_if_newer` (디스크가 더 최신이면 봇 저장분 병합 후 curr_p/max_p 재적용)

극단적 동시 편집은 이론상 남을 수 있음 → 수동은 GUI에서 **새로고침 후** 조작 권장.

## 운영 체크리스트

1. 로그에 `장부 정합` / `장부 등록 최종 실패` 있는지
2. `order_idempotency` 에 `sell:*:현재cycle: filled` 인데 positions에 전량 남아 있는지 → 다음 사이클 정합 대상
3. 유령 종목은 `sync`·`[장부 점검]` 로그 확인 (멱등과 별도)

## 코드 위치

- `execution/ledger_apply.py`
- `execution/idempotency.py` — `reconcile_positions_for_cycle`, `lane_has_filled_sell`
- `run_bot.py` — 사이클 시작 정합, KR/US/COIN 매도 저장
