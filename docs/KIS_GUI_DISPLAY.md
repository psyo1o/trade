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

1. `mark_balance_live_sync` — 이번 갱신만 실조회 허용  
2. `refresh_balance(sync_first=True, force_kis=True)` — **KIS 보유 → 장부 동기화·자동복구** 후 라벨 실조회  
3. `build_account_snapshot_for_report(..., force_kis_labels=True, ledger_only=False)`  
4. 성공 시 `last_kis_display_snapshot` 저장  
5. 끝에서 `clear_balance_live_sync` — **다음** 일반 갱신은 다시 장부+시세

### 비장중(휴장·장 마감) 동작

- **국·미 모두** KIS 라벨 재조회 시도 (`force_kis_labels` 이면 `is_market_open` 무시).  
- **국장:** `_maybe_reject_off_hours_force_label_anomaly` — 총평 급감·급증(직전 ≥ 약 50만원/500USD 이후 12%↑)만 거부. 예수 단독 변동(매수 직후 등)은 총평이 안정이면 반영.  
- **미장 1겹:** 총평 0·45% 급감·예수 0+보유 있음은 heartbeat·force 공통 자동 거부. **12% 급증**은 heartbeat만 1겹에서 거부.  
- **미장 2겹 (`force_kis`):** 급증은 `_maybe_reject_off_hours_force_label_anomaly` 로 — 직전 총평이 **낮은(낡은) 스냅샷**이면 catch-up 허용, **이미 큰 직전값**에서만 12%↑ 거부.  
- **GUI 확인창:** `kis_label_anomaly_prompt` — 급변 시 사용자가 **적용/유지** 선택 (`_resolve_kis_label_anomaly`).  
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
- `persist_kr_cash_from_balance` / `persist_us_cash_from_balance` — **no-op** (국·미 표시는 스냅샷 단일 저장소).

---

## 4. 로그로 모드 구분

로그만 보고 **실 KIS 조회**인지 **장부+시세**인지 구분한다.

### KIS 강제 새로고침 (`🔁 [KIS 강제 새로고침]`)

```
🔁 [KIS 강제 새로고침] 국·미 예수·총평 KIS 실조회 (비장중·쿨다운 무시…)
🔄 [KIS 강제 새로고침] 예수금·보유종목 갱신 중…
  🔁 [KIS 강제 새로고침] 실조회 모드 — …
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
🔄 예수금 및 보유종목 데이터를 갱신합니다… (장부+시세·GUI 백그라운드)
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

1. **HTS/MTS와 숫자 맞추기** → 🏦 **KIS 강제 새로고침** → 로그에 `✅ [KIS 강제 새로고침] last_kis_display_snapshot 저장` 확인. 급변 시 확인창에서 **적용** 선택.  
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
py -3.11 -m unittest tests.test_ledger_label_coalesce tests.test_account_snapshot_force_kis tests.test_trade_history_ledger tests.test_ledger_apply_merge -v
```

