# KIS 잔고 표시 · GUI 강제 새로고침

국·미 **상단 라벨(예수금·총평가)** 과 **15분 사이클**·**GUI 새로고침**이 어떻게 다른지 정리한다.

관련 코드:

| 역할 | 파일 |
|------|------|
| `on_trade` / 실조회 플래그 | `execution/balance_policy.py` |
| KIS TTL·장부 synthetic | `execution/balance_read.py`, `services/ledger_valuation.py` |
| KIS 전역 호출 간격 | `api/kis_rate_limit.py` |
| 라벨 조립·강제 조회·스냅샷 저장 | `services/account_snapshot.py` |
| **표시 진입점(단일)** | **`services/account_display.py`** |
| GUI 진입·로그 | `run_gui.py` (`BalanceUpdaterThread`, `force_refresh_kis`) |
| `run_bot` | `build_account_snapshot_for_report` → `account_display` 위임 |
| 매매내역 → 장부 복구 | `services/trade_history_ledger.py`, `scripts/restore_positions_from_trade_history.py` |

잔고 API TTL·체결 검증: [`idempotency/BALANCE_READS.md`](idempotency/BALANCE_READS.md)  
장부·매매내역 정합: [`idempotency/LEDGER_RECONCILE.md`](idempotency/LEDGER_RECONCILE.md)  
Phase5·서킷: [`PHASE5_ACCOUNT_CIRCUIT.md`](PHASE5_ACCOUNT_CIRCUIT.md)

---

## 1. 두 가지 표시 모드

`config.json` 기본값 `kis_balance_sync_mode: "on_trade"` (키 생략 시 동일).

| 모드 | 언제 | 국·미 KIS 잔고 API | 상단 예수·총평 |
|------|------|-------------------|----------------|
| **장부+시세** | GUI 일반 새로고침, 기동 직후, `:15` 매매 후 갱신, Phase5 보조(`on_trade`) | **호출 안 함** | `last_kis_display_snapshot` **예수** + 장부 보유 × 표시 시세 (`coalesce_ledger_kis_labels`) |
| **KIS 실조회** | **KIS 강제 새로고침**, 매수 직전, 체결·입출금 후 `balance_live_sync_required`, `always` 모드 | **호출** | KIS 응답으로 계산 후 **`last_kis_display_snapshot` 저장** |

코인은 KIS가 아니며, 업비트/바이낸스 잔고는 **두 모드 모두** 거래소 조회(정책 동일).

**단일 저장소:** 국·미 예수·총평의 **영구 저장**은 `last_kis_display_snapshot` 만 사용한다. 장부+시세 경로는 스냅샷을 **덮어쓰지 않는다** (이중 합산·KIS 값 훼손 방지).

---

## 2. KIS 강제 새로고침 (GUI 🏦)

**버튼:** `run_gui.py` → `force_refresh_kis()`

### 용어 정리 (혼동 방지)

| 이름 | 무엇인가 | KIS 강제 새로고침과 관계 |
|------|----------|-------------------------|
| **고점 보정 (입출금)** | GUI 탭 — `peak_total_equity`·`capital_adjustments` 수동 반영 (`adjust_capital.py`) | **별개**. 강제 새로고침 버튼과 무관 |
| **종목 최고가 (`max_p`)** | 보유 종목별 트레일링·손절용 장부 필드 | 새로고침 중 시세 갱신 시 **자동** 갱신. 확인창 없음 |
| **KIS 라벨 확인** | 상단 **예수금·총평가** 라벨 (`last_kis_display_snapshot`) | 직전 값과 다르면 `kis_label_anomaly_prompt` |
| **보유 동기화** | `positions` ↔ KIS 실보유 | 차이 있으면 `holdings_sync_prompt` (국·미 각각) |

### 확인창 (2026-06)

| 시점 | 내용 | 기본 |
|------|------|------|
| 장부 동기화 전 (차이 있을 때만) | **국장 보유** — KIS 실보유 ↔ `positions` 차이(신규등록·유령·평단·수량) 적용 여부 | 아니오 |
| 장부 동기화 전 (차이 있을 때만) | **미장 보유** — 위와 동일, **국장과 별도** 확인 | 아니오 |
| 예수·총평 변동 시 | **국·미 KIS 라벨** — 직전 스냅샷 vs KIS 실조회를 **한 창**에 묶어 적용 여부 (`kis_label_anomaly_prompt`) | 예 |

- 보유 차이가 **없으면** 해당 시장 확인창은 뜨지 않음.
- 보유 동기화 **아니오** → `user_skip_kr_sync` / `user_skip_us_sync` 로 해당 시장 복구·유령삭제·평단보정 **생략**.
- 보유 동기화 **예** → 최근 매수(15분) grace·비장중 API 0건 held 보강·ambiguous 보류도 **우회**해 유령 삭제 (`force_ghost_cleanup_kr/us`).

### 미체결 매수·유령 종목 (예: 상한가 미체결)

| 상황 | 일반 동기화 | KIS 강제 새로고침 + 보유 확인 **예** |
|------|-------------|--------------------------------------|
| 접수만 되고 미체결 → 장부에만 남음 | 매수 후 15분 이내는 유령 삭제 **보류** | `force_user_confirm` 으로 확인창에 표시 (`최근 매수 직후·미체결 가능`) |
| 비장중 KIS 보유 0건·장부만 있음 | `held` 보강으로 유령 삭제 **보류** | `force_ghost_cleanup_*` 시 보강 **생략** 후 삭제 가능 |

확인창에서 **아니오**를 누르면 해당 시장은 `user_skip_*_sync` 로 동기화 전체를 생략한다.

### 처리 순서

1. `mark_balance_live_sync` — 이번 갱신만 실조회 허용  
2. `refresh_balance(sync_first=True, force_kis=True)`  
3. `preview_equity_sync_diff(..., force_user_confirm=True)` → 차이 있으면 국·미 보유 확인창 (`holdings_sync_prompt`)  
4. `sync_all_positions(..., force_equity_sync=True, user_skip_*_sync, force_ghost_cleanup_*)` — KIS 보유·자동복구  
5. `build_account_snapshot_for_report(..., force_kis_labels=True, ledger_only=False)`  
6. 성공 시 `last_kis_display_snapshot` 저장  
7. 끝에서 `clear_balance_live_sync` — **다음** 일반 갱신은 다시 장부+시세

### 비장중(휴장·장 마감) 동작

- **국·미 모두** KIS 라벨 재조회 시도 (`force_kis_labels` 이면 `is_market_open` 무시).  
- **국장:** `_maybe_reject_off_hours_force_label_anomaly` — 총평 급감·급증(직전 ≥ 약 50만원/500USD 이후 12%↑)만 거부. 예수 단독 변동(매수 직후 등)은 총평이 안정이면 반영.  
- **미장 1겹:** 총평 0·45% 급감·예수 0+보유 있음은 heartbeat·force 공통 자동 거부. **12% 급증**은 heartbeat만 1겹에서 거부.  
- **미장 2겹 (`force_kis`):** 급증은 `_maybe_reject_off_hours_force_label_anomaly` 로 — 직전 총평이 **낮은(낡은) 스냅샷**이면 catch-up 허용, **이미 큰 직전값**에서만 12%↑ 거부.  
- **GUI 확인창:** `kis_label_anomaly_prompt` — 장중·비장중 **강제 새로고침** 시 국·미 예수·총평 변동을 **한 번에** 묻고 적용/유지 선택 (`_apply_pending_kis_label_batch`).  
- **보유 확인창:** `holdings_sync_prompt` — `preview_equity_sync_diff` 로 차이 요약 후 국·미 각각 적용 여부.  
- **고점 보정(입출금) 탭** 직후 1회는 `capital_label_refresh_once` 로 방어 **건너뜀** (`trust_live_labels`).

### 자동 호출 간격

- 강제 새로고침: **쿨다운 없음** (연속 클릭은 `refresh_inflight` 중이면 안내 후 스킵).  
- 일반 GUI의 `_allow_kis_fetch`: 시장별 **최소 약 25초** (`run_gui._KIS_REFRESH_MIN_INTERVAL_SEC`).

---

## 3. 장부+시세 — 이중 합산 방지 (2026-05)

**증상:** KIS 강제 새로고침으로 맞는 숫자가 나온 뒤, `:15` 매매 사이클 직후 **예수·총평이 약 2배**로 보임.

**원인:** 예수 필드에 총평이 섞인 채 `예수 + 보유평가` 를 다시 더하거나, 장부+시세 경로가 **`last_kis_display_snapshot` 을 잘못 덮어씀**.

**대응:**

- `services/ledger_valuation.coalesce_ledger_kis_labels()` — 스냅샷 예수 우선, `cash + 보유 > 스냅샷 총평` 이면 예수 보정.  
- `_build_ledger_display_snapshot()` — **`save_last_kis_display_snapshot` 호출 안 함** (KIS 실조회 값 보호).  
- `execution/balance_read._persist_live_cash` — **no-op**. TWAP 체결 폴링·잔고 캐시 조회가 스냅샷을 덮지 않음.  
  표시용 스냅샷은 **`refresh_and_save_kis_snapshot`** 등 명시적 실조회만 갱신.

### 3.1 매수 직후 예수 미반영 → 총평 부풀림 (2026-07)

**증상:** 미장 헷지 등 매수 체결 직후 한때는 예수·총평이 맞다가, 곧 **옛 예수($950) + 신규 보유(TLT)** 로 총평이 다시 커짐.

**원인:**

1. KIS 주문가능 예수가 체결 직후에도 매수 전 값을 유지 반환(또는 캐시).  
2. `_sanitize_kis_cash_total_persist` 가 1차로 `prev_total − stock` 역산한 뒤, 다음 조회에서 `stock_grew` 조건이 깨져 **재오염**.  
3. (과거) 잔고 폴링마다 `persist_us_cash_from_balance` 가 `force=True` 로 스냅샷을 씀.

**대응 (Phase 2 — `services/ledger_valuation._sanitize_kis_cash_total_persist`):**

표시 전용 3규칙. **서킷·MDD는 `market_equity_for_risk`** — sanitize 결과를 쓰지 않는다.

| 규칙 | 요약 |
|------|------|
| **Rule1** | 이중합산 — 총평↑·예수 정체 → `prev_total − stock` 역산 |
| **Rule1b** | 보유 정체·예수=`현금+보유`/`직전총평` → 역산 (마감 후 재발 방지) |
| **Rule2** | 예수 미차감 / 매수 후 예수 0 → 최근 매입대금(`buy_time` 24h) 차감 |
| **Rule3** | 매도 없이 총평 10%↓ → 직전 총평 유지 (매수 직후 고정) |

레거시 `_buy_cash_guard` 키는 읽을 때 무시·저장 시 삭제 (Phase 2).

### 3.2 국장 예수·총평 필드 (한투 TTTC8434R)

개발자센터 앱 설정이나 `FUND_STTL_ICLD_YN` 으로는 안 바뀐다. **응답 필드를 문서대로 고르는 문제**다.

| 필드 | 한투 의미 | 봇에서 |
|------|-----------|--------|
| `dnca_tot_amt` | 예수금총금액 | **표시 예수** |
| `prvs_rcdl_excc_amt` | D+2 가수도 (주문가능) | 매수 예산. 당일 매수 직후 0일 수 있음 |
| `scts_evlu_amt` | 유가평가 | 보유 평가 |
| `tot_evlu_amt` | 유가 + **D+2만** | 표시 총평으로 쓰지 않음 |
| `nass_amt` | 순자산 | **표시 총평** |

당일 매수 후 D+2=0 이면 `tot_evlu_amt` 가 보유평가만 남아 GUI 예수 0·총평 급감처럼 보였다. `parse_kr_cash_total` 은 `dnca`·`nass` 를 쓴다.

**매도 직후 (2026-08-24):** MTS는 예수(dnca)만 낮고 D+2·총자산은 매도대금 포함인 경우가 많다.  
- 파서: 보유≈0이면 `total = max(nass, tot_evlu, D+2, …)`  
- **보유 0(전액 현금)이면 표시 예수 = 총평 = NAV** (dnca만 보여 예수≠총평이 되지 않게)  
- GUI 강제 새로고침: `max(cash+hold, nav)`. 로그 `📌 [KR raw]`.  
- 매수 예산만 `prvs_rcdl_excc_amt`(D+2) — `parse_kr_orderable_cash`.

---

## 4. 로그로 모드 구분

로그만 보고 **실 KIS 조회**인지 **장부+시세**인지 구분한다.

### KIS 강제 새로고침 (`🔁 [KIS 강제 새로고침]`)

```
🔁 [KIS 강제 새로고침] 국·미 예수·총평 KIS 실조회 (보유·총평 차이 있으면 각각 확인창)
🔄 [KIS 강제 새로고침] 예수금·보유종목 갱신 중…
  🔁 [KIS 강제 새로고침] 실조회 모드 — …
  (차이 있을 때) 국장 보유 동기화 / 미장 보유 동기화 확인창
  📌 [장부 동기화] 사용자 선택 — 국장|미장 보유·평단 동기화 생략
  🔁 [KIS 강제 새로고침] 국·미 KIS 실조회 — …
  🔁 [KIS 강제 새로고침] 국·미 예수·총평 라벨 KIS 조회 시작
  ✅ [KIS 강제 새로고침] last_kis_display_snapshot 저장 — KR … / US …
✅ [KIS 강제 새로고침] 완료 — … last_kis_display_snapshot 갱신
```

비장중 감소·급증 유지 시:

`⚠️ [KIS 강제 새로고침·US] 비장중 — 총평 직전보다 … — 직전 라벨 유지`

또는 GUI 확인 후:

`✅ [KIS 강제 새로고침·US] 사용자 확인 — KIS 새 값 적용`

### 일반 갱신 (`[표시] 장부+시세`)

```
🔄 장부 및 보유종목 데이터를 갱신합니다… (장부+시세·GUI 백그라운드)
  [표시] 장부+시세 — 국·미 KIS 잔고 API 생략 …
  [표시] 장부+시세 — KIS 국·미 잔고 API 생략 (상단 라벨: last_kis_display_snapshot + 장부 보유평가)
✅ [표시] 장부+시세 갱신 완료 — … (국·미 상단 라벨은 직전 KIS 스냅샷 예수 + 장부 시세)
```

`:15` 매매 직후에도 위와 같은 **`[표시]`** 로그가 나오는 것이 정상이다. 상단 숫자를 KIS와 맞추려면 **강제 새로고침**을 다시 누른다.

---

## 5. `bot_state.json` 키

| 키 | 용도 |
|----|------|
| `last_kis_display_snapshot` | 마지막 **KIS 실조회**로 저장한 국·미 예수·총평 (장부+시세 **폴백·예수** 기준) |
| `last_kr_cash_krw` / `last_us_cash_usd` | 레거시 예수 — 스냅샷 비었거나 예수=총평 오염 시 `display_cash_from_state` 폴백만 |
| `circuit_aux_last_coin_krw` | 코인 총평(원) — Phase5·표시 |
| `_phase5_aux_sync` | 루프마다 `kr_ok`/`us_ok`/`coin_ok`; **장부+시세 루프** 시 `ledger_only`+`kr_krw`/`usd_total` (Phase5 비중용 추정 총평) |
| `balance_live_sync_required` | 체결·입출금·강제 새로고침 직후 1회 실조회 플래그 |

국·미 총평 **읽기:** GUI 표시는 `coalesce_ledger_kis_labels`, Phase5·`kis_display_total()` 은 스냅샷 — 단, `ledger_only` 루프에서는 `_phase5_aux_sync` 의 장부+시세 추정 총평을 우선한다.

---

## 6. 운영 체크리스트

1. **HTS/MTS와 숫자 맞추기** → 🏦 **KIS 강제 새로고침** → 보유·총평 확인창에서 의도에 맞게 선택 → 로그에 `✅ [KIS 강제 새로고침] last_kis_display_snapshot 저장` 확인.  
2. **:15 사이클 후 상단이 튀면** → 강제 새로고침 직전 값이 맞았는지, 직후 로그가 `[표시] 장부+시세` 인지 확인 (2배 현상은 §3 보정 후 재발 시 이슈 제보).  
3. **Phase5 비중과 GUI 총평이 어긋나면** → 장부+시세 모드에서는 Phase5가 `_phase5_aux_sync` 추정값을 쓰므로, KIS 실조회 직후에는 강제 새로고침으로 스냅샷을 맞춘다.  
4. **EGW00201** 빈번 시 → **🏦 강제 새로고침 연속 클릭 금지**. GUI만 쓰면 `:15` 사이클은 장부+시세라 잔고 API가 적음. 그래도 나면 `BOT_KIS_MAX_CALLS_PER_SEC=8`, `BOT_KIS_BALANCE_MIN_INTERVAL_SEC=6` — [`BALANCE_READS.md`](idempotency/BALANCE_READS.md) 참고.

---

## 7. GUI 글꼴

`run_gui.py` — 대시보드 전역 **15** (`_dashboard_stylesheet` + `QFont("Malgun Gothic", 15)`):

| 영역 | 크기 |
|------|------|
| 기본 위젯·라벨·버튼·표·입력·탭 | **15px** (QSS) / **15pt** (`setFont`) |
| 실시간 로그·가이드 (`#LogConsole`) | **15px**, Consolas/맑은 고딕 monospace |

일괄 변경: `_dashboard_stylesheet()` 내 `font-size` 및 `initUI` 의 `base_ui_font`·`font_value`.

---

## 8. 테스트

```bash
py -3.11 -m unittest tests.test_ledger_label_coalesce tests.test_account_snapshot_force_kis tests.test_sync_positions_preview tests.test_trade_history_ledger tests.test_trade_history_reconcile tests.test_kr_upper_limit tests.test_idempotency tests.test_ledger_apply_merge -v
```

> **주의:** `tests/test_sync_positions_preview.py` 는 `sync_all_positions` 를 **임시 경로**에서만 호출한다. 실제 `bot_state.json` 을 테스트 대상으로 쓰지 않는다.

