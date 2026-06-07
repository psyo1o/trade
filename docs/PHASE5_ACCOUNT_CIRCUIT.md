# Phase5 계좌 서킷 · 잔고 조회 정책

**전제:** HTS/MTS 수동 매매 없음 — **봇만** 매매한다.

**한 줄 요약 (2026-06):**

- **청산:** 기본은 **시장별(KR/US/COIN) 포트폴리오 비중** 하한 미만일 때 **해당 시장만** 전량 청산.
- **합산 MDD 서킷:** `account_circuit_use_total: true` 일 때만 (레거시).
- **KIS 잔고 API:** 평상시 **장부+현재가** 표시, **매매·강제 새로고침·입출금** 때만 실조회 (`kis_balance_sync_mode: on_trade`).

---

## 1. 시장별 비중 서킷 (기본)

### 판정

| 항목 | 설명 |
|------|------|
| 합산 대상 | `circuit_aux` 가 **OK인 시장만** 합산 (API 실패 시장 제외 → 오발동 방지) |
| 비중 | `시장 평가액(KRW) / OK 시장 합계 × 100` |
| 하한 | `config` 의 `account_circuit_min_share_kr_pct` 등 (기본 KR·US **8%**, COIN **5%**) |
| 앵커 | 주차(서울)마다 `phase5_share_anchor` 저장, `비중 < 앵커×anchor_min_ratio` 도 발동 가능 |

코드: `execution/circuit_break.py` → `evaluate_per_market_share_circuits`  
루프: `run_bot._maybe_run_account_circuit`

### 발동 시 동작

1. 텔레그램: `🚨 [Phase5 {시장} 비중 서킷]`
2. **해당 시장만** `_phase5_liquidate_market(시장)` (lane `phase5`)
3. `account_circuit_market_cooldowns.{시장}` → **그 시장 신규 매수만** 24h 차단 (기본 `account_circuit_cooldown_hours`)
4. `phase5_pending_liquidation_markets` 에 시장 추가 → 비장중이면 장 개시 후 재시도

**다른 시장(예: 코인만 하한 미만)은 국·미 포지션을 건드리지 않음.**

### 로그 예

```
🛡️ [Phase5·KR] 비중 6.2% / 하한 8.0% → 발동 | KR 비중 6.2% < 하한 8.0% — 시장 단위 서킷
🛡️ [Phase5·US] 비중 52.0% / 하한 26.0% → 정상 | ...
```

---

## 2. 레거시 합산 MDD 서킷 (옵션)

`config.json`:

```json
"account_circuit_use_total": true
```

| 항목 | 동작 |
|------|------|
| 고점 | `peak_total_equity` — 월요일(서울) 주차 앵커·상향 추적 |
| 발동 | `(peak - 합산) / peak × 100 >= account_circuit_mdd_pct` (기본 15%) |
| 청산 | **KR + US + COIN 전부** `_phase5_emergency_liquidate_all` |
| 쿨다운 | `account_circuit_cooldown_until` — **전 시장** 매수 차단 |

**기본값은 `false`** — 새 설치·일반 운영에서는 켜지 않음.

---

## 3. 대기 청산 큐 (`phase5_pending_*`)

장중이 아니거나 API 한도로 청산이 안 끝나면 다음 루프에서 재시도.

| 장부 키 | 의미 |
|---------|------|
| `phase5_pending_liquidation_markets` | `["KR"]`, `["US"]` 등 **시장 목록** (표준) |
| `phase5_pending_liquidation` | 레거시 boolean — **합산 모드**에서만 `true`면 전 시장으로 해석 |

### 시장별 모드에서의 정리 (중요)

매 루프 **비중 판정 후**:

1. **`_phase5_prune_stale_pending`** — 현재 비중 서킷이 **정상**인 시장은 큐에서 **제거** (예전 합산 서킷 잔여 오청산 방지)
2. **`_phase5_try_pending_liquidation`** — 큐에 남은 시장만 재청산

예전에 `phase5_pending_liquidation: true` 만 남아 있으면 **비중이 정상인데 KR/US/COIN 청산**이 반복될 수 있었음 → 위 정리로 차단.

레거시 boolean 단독(시장 목록 없음)은 시장별 모드에서 **자동 해제** (`_phase5_migrate_legacy_pending_flag`).

---

## 4. 쿨다운 종류 (헷갈리기 쉬움)

| 종류 | 장부 키 | 범위 | 기간 |
|------|---------|------|------|
| **시장 Phase5 매수 차단** | `account_circuit_market_cooldowns` | KR / US / COIN 각각 | 기본 24h |
| **레거시 전역 매수 차단** | `account_circuit_cooldown_until` | 전 시장 | 합산 서킷만 |
| **종목 재진입 차단** | `cooldown` / `ticker_cooldowns` | 해당 티커만 | Phase5 청산 시 often 1h |

Phase5 청산 직후 로그 ` [쿨다운 적용] 379810 | 사유: Phase5 서킷 청산 | 차단: 1시간` 은 **종목** 쿨다운이지, 국장 전체 매수 차단이 **아님**.

매수 게이트: `in_account_circuit_cooldown(state, "KR")` 등 — **시장별**만 검사.

---

## 5. KIS 잔고 API — `on_trade` 모드 (기본)

**목적:** EGW00201(초당 거래건수 초과) 등 **잔고 API 폭주** 방지.

`config.json` (기본값은 키 생략 시 `on_trade`):

```json
"kis_balance_sync_mode": "on_trade"
```

| 상황 | KIS 국·미 잔고 API |
|------|-------------------|
| GUI 일반 새로고침 / 15분 사이클(매도만) | **안 함** — 장부 `qty` × 현재가 + `last_kis_display_snapshot` 예수 |
| 매수 창 직전 | **1회** (`kr_balance_raw(refresh=True)`) |
| 체결 직후 | **1회** + `balance_live_sync_required` |
| GUI **강제 KIS 새로고침** | **1회** — 비장중 포함, 급변 시 확인창·스냅샷 저장 |
| `adjust_capital.py` 입출금 보정 | **1회** (`refresh_circuit_aux_from_brokers`) |
| 코인 | KIS 아님 — 업비트/바이낸스 잔고는 별도 |

표시 모드 로그: `[표시] 장부+시세 — …` (일반 갱신·`:15` 사이클 후)  
강제 새로고침 로그: `🔁 [KIS 강제 새로고침] …` — 상세는 [`KIS_GUI_DISPLAY.md`](KIS_GUI_DISPLAY.md)

장부+시세 모드는 **`last_kis_display_snapshot` 을 덮어쓰지 않음** (예수+보유 이중 합산 방지).

코드:

- `execution/balance_policy.py` — 플래그·판정
- `services/ledger_valuation.py` — 장부+시세 합산
- `execution/balance_read.py` — TTL 캐시·한도 시 stale·최소 호출 간격

환경 변수(선택): `BOT_KIS_BALANCE_CACHE_TTL_SEC`, `BOT_KIS_BALANCE_MIN_INTERVAL_SEC`, `BOT_KIS_BALANCE_STALE_SEC`

자세한 TTL·주문 검증 규칙: [`idempotency/BALANCE_READS.md`](idempotency/BALANCE_READS.md)

---

## 6. `circuit_aux` · Phase5 보조

| 키 | 용도 |
|----|------|
| `last_kis_display_snapshot` | 국·미 예수·총평 **영구 저장** (KIS 실조회 시 갱신) |
| `circuit_aux_last_coin_krw` | 코인 총평(원) |
| `last_kr_cash_krw` / `last_us_cash_usd` | 레거시 예수 폴백 (`display_cash_from_state`) |
| `phase5_share_anchor` | 주차별 시장 비중 앵커 |
| `_phase5_aux_sync` | 루프마다 `kr_ok` / `us_ok` / `coin_ok`; **장부+시세** 루프 시 `ledger_only: true` 와 `kr_krw` / `usd_total` (Phase5 비중 판정용 추정 총평) |

`on_trade` 모드: `refresh_circuit_aux_from_brokers` 대신 `update_circuit_aux_from_ledger` 로 **장부+시세 추정** (코인은 거래소 조회 병행).

Phase5 비중 계산은 `ledger_valuation.kis_display_total()` 을 사용한다. `ledger_only` 루프에서는 `_phase5_aux_sync` 의 추정 총평을 우선해 **스냅샷 총평이 낡아도** 보유 시세 변동을 반영한다. KIS 실조회·강제 새로고침 후에는 스냅샷과 추정값이 다시 맞춰진다.

---

## 7. config 참조

```json
{
  "account_circuit_enabled": true,
  "account_circuit_use_total": false,
  "account_circuit_min_share_kr_pct": 8,
  "account_circuit_min_share_us_pct": 8,
  "account_circuit_min_share_coin_pct": 5,
  "account_circuit_share_anchor_min_ratio": 0.5,
  "account_circuit_cooldown_hours": 24,
  "account_circuit_mdd_pct": 15,
  "kis_balance_sync_mode": "on_trade"
}
```

또는 시장별 하한 객체:

```json
"account_circuit_min_share_pct": { "KR": 8, "US": 8, "COIN": 5 }
```

---

## 8. 코드 위치

```
execution/circuit_break.py     # 비중·합산 MDD 판정
execution/guard.py               # 쿨다운·앵커·peak_total_equity
execution/balance_policy.py    # on_trade / live sync 플래그
execution/balance_read.py        # KIS TTL·stale·ledger_only
services/ledger_valuation.py   # 장부+시세 synthetic balance
execution/phase5_ops.py   # maybe_run_account_circuit · 대기청산 · 시장별 청산
run_bot.py                # _maybe_run_account_circuit → phase5_ops 위임
  build_account_snapshot_for_report(ledger_only=...)
run_gui.py                     # BalanceUpdaterThread · force_refresh_kis
docs/KIS_GUI_DISPLAY.md        # GUI 표시 모드·로그·강제 새로고침
adjust_capital.py              # 입출금 후 live sync
tests/test_phase5_share_circuit.py
```

매도 lane: `docs/idempotency/SELL_LANES.md` — Phase5는 `phase5`.

---

## 9. 운영 체크리스트

| 증상 | 확인 |
|------|------|
| 비중 정상인데 청산됨 | `phase5_pending_liquidation*` · 레거시 boolean — 봇 재시작 후 prune 로그 확인 |
| `output1 없음` / EGW00201 | `on_trade`·캐시 동작 여부, 출동+새로고침 동시 클릭 줄이기 |
| 한 시장만 막혀야 하는데 전 시장 매수 불가 | `account_circuit_use_total` 이 true 인지, `account_circuit_cooldown_until` 존재 여부 |
| 예수금 표시 어긋남 | 강제 KIS 새로고침 1회 또는 `adjust_capital` 로 live sync |
| Phase5 비중 %가 GUI와 다름 | `ledger_only` 루프 — `_phase5_aux_sync` 추정값 사용 여부 확인; KIS 강제 새로고침으로 스냅샷 정렬 |

수동으로 대기 큐 비우기 (`bot_state.json`):

```json
"phase5_pending_liquidation": false,
"phase5_pending_liquidation_markets": []
```

---

## 10. 변경 이력 (요약)

| 시기 | 내용 |
|------|------|
| 기존 | 합산 `peak_total_equity` MDD → **전 시장** 청산, API 실패 시 코인까지 오발동 사례 |
| 2026-06 | **시장별 비중 서킷** 기본, 합산은 옵션 |
| 2026-06 | KIS 잔고 **on_trade** + TTL 캐시 |
| 2026-06 | 대기 청산 **stale prune** · 레거시 pending 플래그 정리 |
| 2026-06 | 국·미 표시 **단일 스냅샷** · Phase5 `ledger_only` 추정 총평 · US force_kis 급증 가드 정리 |
