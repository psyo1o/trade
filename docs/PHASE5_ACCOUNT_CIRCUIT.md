# Phase5 계좌 서킷 · 잔고 조회 정책

**전제:** HTS/MTS 수동 매매 없음 — **봇만** 매매한다.

> **잔고 스냅샷 오발동·근본 해결 로드맵:** [`EQUITY_SNAPSHOT_CIRCUIT_PLAN.md`](EQUITY_SNAPSHOT_CIRCUIT_PLAN.md)  
> Phase5·MDD 판정은 **장부 risk 총평** (`market_equity_for_risk`) 우선. 스냅샷은 GUI·표시 전용.

> **2026-08-23:** 계좌 -5% `check_mdd_break` **매수 차단은 폐지**. `peak_equity_*`는 Phase5(기본 15%) 고점 추적용으로만 유지.

**한 줄 요약 (2026-08):**

- **청산:** 기본은 **시장별 잔고 MDD** — `peak_equity_KR/US/COIN` 대비 `account_circuit_mdd_pct`(기본 15%) 이상 하락하면 **해당 시장만** 전량 청산. 지수 MDD는 `account_circuit_use_index: true` 옵션.
- **입출금:** GUI/스크립트에서 **어느 시장 계좌인지** 고르면 그 시장 고점만 가감. 미장 입금이 국장 서킷을 건드리지 않음.
- **비중 서킷:** `account_circuit_use_share: true` 일 때만 (예전 기본).
- **합산 MDD 서킷:** `account_circuit_use_total: true` 일 때만 (레거시).
- **KIS 잔고 API:** 평상시 **장부+현재가** 표시, **매매·강제 새로고침·입출금** 때만 실조회 (`kis_balance_sync_mode: on_trade`).

---

## 1. 시장별 잔고 MDD 서킷 (기본)

> 지수 MDD는 아래 「옵션」 또는 `account_circuit_use_index`.

### 판정 (잔고)

| 항목 | 설명 |
|------|------|
| 고점 | `peak_equity_{시장}` (KR=원, US=USD, COIN=업비트 원 / 바이낸스 USDT) |
| 현재 | KR/US=`market_equity_for_risk`, COIN=`circuit_aux_last_coin_native` |
| 발동 | 고점 대비 DD ≥ `account_circuit_mdd_pct` (기본 15%) |
| 장외 | KR/US는 장중에만 |
| 보유 0 | 스킵 |

코드: `evaluate_per_market_equity_circuits` · `_run_per_market_mdd_circuits`

## 1-opt. 시장별 지수 MDD 서킷 (옵션)

### 판정

| 항목 | 설명 |
|------|------|
| 벤치마크 | US **SPY** / KR **069500.KS** / COIN **거래소 BTC** |
| 고점 | 최근 **6mo** 일봉 종가 최고값 |
| 현재 | 최신 종가 (yfinance) |
| 발동 | `(고점 - 현재) / 고점 × 100 >= account_circuit_mdd_pct` (기본 15%) |
| 장외 | KR/US는 **장중**에만 판정 (기존과 동일) |
| API 실패 | 해당 시장 스킵 (발동 안 함) |

코드: `execution/phase5_index_circuit.py`  
루프: `execution/phase5_ops._run_per_market_index_circuits`

### 발동 시 동작

1. 텔레그램: `🚨 [Phase5 {시장} 지수 서킷]`
2. yfinance **2차 재조회** 후에도 임계 초과
3. **AI 청산 심사** — 보유·지수·계좌 맥락 → `liquidation_score` 0~100 (기본 **≥70** 청산). LLM 무응답 시 재시도(기본 3회) → 실패 시 보류
4. **해당 시장만** 전량 청산 · 24h 매수 쿨다운

코드: `phase5_index_circuit.py`, `phase5_ai_liquidation.py`, `phase5_ops._ai_gate_phase5_liquidation`

**config:** `phase5_ai_liquidation_enabled`, `phase5_ai_liquidation_threshold`, `phase5_ai_liquidation_provider`, `phase5_ai_liquidation_max_retries`

### 레거시 계좌 MDD

`peak_equity_*`·`market_equity_for_risk` 기반 Phase5는 **폐지**. 입출금 고점 보정(`adjust_capital`)은 레거시 키 유지용.

### 입출금

GUI **고점 보정** 탭 또는 `adjust_capital.py` 에서 **국장/미장/코인**을 고른 뒤 금액을 넣는다.

- 국장 입금 100만 → `peak_equity_KR` +100만, `peak_total_equity` +100만
- 미장 입금 100만 → `peak_equity_US` +(100만/환율) USD, 합산 고점 +100만
- 비중 앵커(`phase5_share_anchor`)도 현재 스냅샷으로 다시 잡음 (비중 모드 쓸 때)

### 로그 예

```
🛡️ [Phase5·KR] 658,357원 (고점 658,357원) DD=0.00% → 정상 | KR 고점 대비 0.00% 하락 — 임계(15%) 이내
🛡️ [Phase5·US] $3,463 (고점 $3,515) DD=1.48% → 정상 | ...
```

---

## 1-b. 시장별 비중 서킷 (옵션)

`config.json`:

```json
"account_circuit_use_share": true
```

합산 대비 비중이 하한 미만이면 해당 시장 청산. **다른 시장 입금·평가 상승만으로도** 비중이 내려가 오발동할 수 있어 기본은 끈다.

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
- `execution/balance_read.py` — TTL 캐시·한도 시 stale·최소 호출 간격(4초)
- `api/kis_rate_limit.py` — KIS HTTP 전역 스로틀(실전 기본 12건/초)

환경 변수(선택): `BOT_KIS_BALANCE_CACHE_TTL_SEC`, `BOT_KIS_BALANCE_MIN_INTERVAL_SEC`, `BOT_KIS_BALANCE_STALE_SEC`, `BOT_KIS_MAX_CALLS_PER_SEC`, `BOT_KIS_RATE_LIMIT_COOLDOWN_SEC`

자세한 TTL·주문 검증 규칙: [`idempotency/BALANCE_READS.md`](idempotency/BALANCE_READS.md)

---

## 5-b. Risk 총평 vs Display 스냅샷 (Phase 1)

| 용도 | 함수 / 저장소 | sanitize |
|------|--------------|----------|
| **Phase5 15% 서킷** | `market_equity_for_risk()` | 사용 안 함 |
| **5% MDD 매수 중단** | **폐지** (`check_mdd_break` 스텁 항상 True) | — |
| **매수 배정·비중** | KR/US 사이클 → risk 총평 | 사용 안 함 |
| **GUI·텔레·리포트** | `last_kis_display_snapshot` | `persist_display_cash_total()` |

코드: `services/ledger_valuation.py` — `market_equity_for_risk`, `persist_display_cash_total`  
상세 로드맵: [`EQUITY_SNAPSHOT_CIRCUIT_PLAN.md`](EQUITY_SNAPSHOT_CIRCUIT_PLAN.md)

### 청산 직전 재검증 (2026-08-24 → 2026-08-28 3단계)

KR/US가 MDD로 **1차 발동**되면 즉시 청산하지 않고:

1. `invalidate` + KIS `refresh=True` 강제 재조회  
2. `market_equity_for_risk` 재계산 → MDD **2차** 판정  
3. **타당성 게이트** — US=SPY / KR=069500.KS / COIN=거래소 BTC 최근 최대 DD와 계좌 DD 비교(차이 >12pp면 보류). 1루프 내 15%+ 절벽·무보유도 보류.  
4. 위를 모두 통과할 때만 청산 / 재조회·타당성 실패 시 **보류** + 텔레그램

코드: `phase5_ops._reconfirm_phase5_triggers_before_liquidation`, `phase5_plausibility.evaluate_phase5_liquidation_plausibility`

### 매도 정산 지연 (2026-08-28)

`market_equity_for_risk`: ledger `cash+stock` 이 `snap_total` 보다 5%+ 낮으면 **snap_total** 반환 (시간 유예 없음).

---

## 6. `circuit_aux` · Phase5 보조

| 키 | 용도 |
|----|------|
| `last_kis_display_snapshot` | 국·미 예수·총평 **영구 저장** (KIS 실조회 시 갱신) |
| `circuit_aux_last_coin_native` | 코인 총평(견적 통화) — **Phase5 MDD** (업비트 원 / 바이낸스 USDT) |
| `circuit_aux_last_coin_krw` | 코인 총평(원) — 합산·비중용 (바이낸스=native×환율) |
| `peak_equity_COIN_unit` | `"KRW"` \| `"USDT"` — COIN 고점 단위 |
| `last_kr_cash_krw` / `last_us_cash_usd` | 레거시 예수 폴백 (`display_cash_from_state`) |
| `phase5_share_anchor` | 주차별 시장 비중 앵커 |
| `_phase5_aux_sync` | 루프마다 `kr_ok` / `us_ok` / `coin_ok`; **장부+시세** 루프 시 `ledger_only: true` 와 `kr_krw` / `usd_total` (Phase5 비중 판정용 추정 총평) |

`on_trade` 모드: `refresh_circuit_aux_from_brokers` 대신 `update_circuit_aux_from_ledger` 로 **장부+시세 추정** (코인은 거래소 조회 병행).

Phase5·매수 배정은 **`market_equity_for_risk()`** (장부 예수 + 보유)를 쓴다. (구 5% `check_mdd_break` 매수 차단은 폐지.)  
`ledger_only` 루프에서는 `_phase5_aux_sync` 의 risk 추정값을 우선한다.  
**표시용** `last_kis_display_snapshot` 은 GUI·텔레 전용 — 서킷 입력으로 쓰지 않는다.

---

## 7. config 참조

```json
{
  "account_circuit_enabled": true,
  "account_circuit_use_total": false,
  "account_circuit_use_share": false,
  "account_circuit_mdd_pct": 15,
  "account_circuit_min_share_kr_pct": 8,
  "account_circuit_min_share_us_pct": 8,
  "account_circuit_min_share_coin_pct": 5,
  "account_circuit_share_anchor_min_ratio": 0.5,
  "account_circuit_cooldown_hours": 24,
  "kis_balance_sync_mode": "on_trade",
  "coin_swing_entry_noise_grace_hours": 2.0,
  "coin_swing_entry_hard_cut_pct": -3.0
}
```

또는 시장별 하한 객체:

```json
"account_circuit_min_share_pct": { "KR": 8, "US": 8, "COIN": 5 }
```

---

## 8. 코드 위치

```
execution/circuit_break.py     # 시장별 평가 MDD · 비중 · 합산 MDD 판정
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

## 10. COIN SWING 매도 — 진입 유예 (노이즈 방어)

**대상:** `SWING_FIB` 코인만 (`execution/market_cycles/coin_cycle.py`). KR/US는 **미적용**.

스윙 진입 직후 잔파동으로 `check_swing_exit` **기술바닥 FULL**(`스윙 기술바닥 이탈 …`)이 나와 허무하게 청산되는 것을 줄이기 위한 **2단계 보호**입니다.

| 단계 | 조건 | 동작 |
|------|------|------|
| **1) 신규 매수 보호** | 매수 후 **15분** + 수익률 **+1% 미만** | **모든** 매도 판정 스킵 (`_new_buy_sell_protection_blocks`, KR/US/COIN 공통) |
| **2) COIN 진입 유예** | 보유 **2h 미만** + 기술바닥 FULL + 수익률 **-3% 초과** | FULL **유예** — 로그 `🔰 … 기술바닥 이탈 검사 유예` |
| **하드컷** | 수익률 **≤ -3%** | 2h·유예 **무시**, 즉시 FULL |
| **2h 이후** | — | 기술바닥 FULL **100% 적용** (기존과 동일) |

**유예 대상 아님:** 1.5R HALF, 5MA·RSI FULL, 타임스탑, V8 경로.

`config.json` (선택):

```json
"coin_swing_entry_noise_grace_hours": 2.0,
"coin_swing_entry_hard_cut_pct": -3.0
```

코드:

- `run_bot.py` — `COIN_SWING_ENTRY_*`, `_coin_swing_entry_noise_defers_tech_floor_full`, `_log_coin_swing_entry_noise_defer`
- `execution/market_cycles/coin_cycle.py` — `decide_swing_exit` FULL 직전 검사
- `tests/test_coin_swing_entry_grace.py`

README §5-3 표와 동일 내용.

---

## 11. 운영 체크리스트

| 증상 | 확인 |
|------|------|
| 비중 정상인데 청산됨 | `phase5_pending_liquidation*` · 레거시 boolean — 봇 재시작 후 prune 로그 확인 |
| 입출금 직후 다른 시장 서킷 | 고점 보정에서 **해당 시장**을 골랐는지. 기본은 시장별 MDD라 다른 시장은 안 건드림 |
| 국장 총평이 갑자기 20만 등으로 급감 | 스냅샷 급감 가드·`kis_display_total` 가드. 강제 KIS 새로고침 |
| `output1 없음` / EGW00201 | `on_trade`·캐시·`kis_rate_limit` 동작, **강제 새로고침** 남용 줄이기, 환경 변수로 `BOT_KIS_MAX_CALLS_PER_SEC` 낮추기 |
| 한 시장만 막혀야 하는데 전 시장 매수 불가 | `account_circuit_use_total` 이 true 인지, `account_circuit_cooldown_until` 존재 여부 |
| 예수금 표시 어긋남 | 강제 KIS 새로고침 1회 또는 `adjust_capital` 로 live sync |
| Phase5 비중 %가 GUI와 다름 | `ledger_only` 루프 — `_phase5_aux_sync` 추정값 사용 여부 확인; KIS 강제 새로고침으로 스냅샷 정렬 |
| COIN 스윙 진입 직후 곧바로 FULL | `🔰 … 기술바닥 이탈 검사 유예` 로그 — 2h 미만이면 정상; -3% 이하면 하드컷 즉시 매도 |

수동으로 대기 큐 비우기 (`bot_state.json`):

```json
"phase5_pending_liquidation": false,
"phase5_pending_liquidation_markets": []
```

---

## 12. 변경 이력 (요약)

| 시기 | 내용 |
|------|------|
| 기존 | 합산 `peak_total_equity` MDD → **전 시장** 청산, API 실패 시 코인까지 오발동 사례 |
| 2026-06 | **시장별 비중 서킷** 기본, 합산은 옵션 |
| 2026-06 | KIS 잔고 **on_trade** + TTL 캐시 |
| 2026-06 | 대기 청산 **stale prune** · 레거시 pending 플래그 정리 |
| 2026-06 | 국·미 표시 **단일 스냅샷** · Phase5 `ledger_only` 추정 총평 · US force_kis 급증 가드 정리 |
| 2026-08 | **기본을 시장별 평가 MDD** 로 전환. 입출금은 시장 선택. 비중 서킷은 `account_circuit_use_share`. 총평 급감 가드. |
| 2026-08-20 | 스냅샷·서킷 오발동 근본 해결 Phase0~3 (market_equity_for_risk, sanitize Rule1~3). 상세: EQUITY_SNAPSHOT_CIRCUIT_PLAN.md, docs/CHANGELOG.md |
| 2026-08-21 | Phase5/MDD **장중 게이트**, US stale 현금 오탐 방어. 상세: docs/CHANGELOG.md |
