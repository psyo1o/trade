# 잔고 스냅샷 · 서킷 오발동 근본 해결 계획

> **2026-08-23:** 계좌 -5% `check_mdd_break` 매수 차단은 **비활성(항상 허용)**. Phase5 15%만 유효. `peak_equity_*`는 Phase5 고점용.

**작성:** 2026-08-20  
**배경:** 매수 직후 KIS 잔고 라벨 보정(`_sanitize`)이 총평을 잘못 깎아 Phase5 15% 서킷·5% MDD가 오발동. 국장(411060)·미장(TLT) 사례.

**관련 문서:** [`PHASE5_ACCOUNT_CIRCUIT.md`](PHASE5_ACCOUNT_CIRCUIT.md), [`KIS_GUI_DISPLAY.md`](KIS_GUI_DISPLAY.md), [`EQUITY_SNAPSHOT_CIRCUIT_PLAN.md`](EQUITY_SNAPSHOT_CIRCUIT_PLAN.md)

---

## 1. 문제 요약

| 구분 | Phase5 15% 서킷 | 5% MDD 브레이크 |
|------|----------------|----------------|
| 임계 | 고점 대비 15% → **청산 + 24h 매수 차단** | 고점 대비 5% → **신규 매수만 중단** |
| 입력 | `kis_display_total()` / 스냅샷 | 매매 사이클 in-memory 총평 |
| 오발동 패턴 | 스냅샷 총평 급감 (예: $3,465→$2,313) | 동일 |

**공통 직접 원인:** `services/ledger_valuation.py` Case **B3** — 이미 차감된 예수에서 매입대금을 **한 번 더** 빼 총평 반토막.

**구조적 원인:** GUI용 `last_kis_display_snapshot` = 서킷 판정 입력 (역할 혼재). 매매 후 `force=True` 저장이 급감 가드 우회.

---

## 2. 왜 예전엔 안 그랬나

| 예전 | 지금 |
|------|------|
| 매 루프 KIS API → `cash + stock` | `on_trade`: 평소 API 생략 |
| 총평 단일 경로 | 스냅샷 / 장부 / aux / sanitize / coalesce 다중 경로 |
| MDD = API 총평 | MDD = sanitize 거친 스냅샷 |

`on_trade`는 API 한도(EGW00201) 대응으로 필요. **표시와 리스크 입력을 분리**하면 패치 누적 없이 유지 가능.

---

## 3. 사건 타임라인 (미장 2026-08-20)

```
04:31  총 $3,394 (현금 $2,225 + DXCM $1,169) — 정상
04:32  TLT ~$1,078 매수 체결
04:32  Case B3 → 총평 $2,313 저장 (force=True)
04:46  Phase5 US DD 33% → DXCM 청산, US 24h 쿨다운
04:48  API 정상화 → 총평 ~$3,381 (실손 ~2%)
```

국장(2026-08-14)도 동일 B3 패턴 (658,895→351,060).

---

## 4. 해결 로드맵

진행 상태는 `[ ]` / `[x]` 로 갱신한다.

### Phase 0 — 응급 (오발동 즉시 차단)

| # | 작업 | 파일 | 상태 |
|---|------|------|------|
| 0-1 | Case B3: **예수 미차감일 때만** 역산 (`nc >= prev_cash×0.85`, 이미 차감이면 스킵) | `ledger_valuation.py` | [x] |
| 0-2 | 매매 후 스냅샷: **force여도** 총평 10%↓ + 매도 없음 → 저장 거부 | `run_bot.py` | [x] |
| 0-3 | `market_equity_for_risk()` — 서킷·MDD 전용 (장부 예수 + 보유) | `ledger_valuation.py` | [x] |
| 0-4 | Phase5·circuit_aux가 **risk 경로** 사용 | `phase5_ops.py`, `ledger_valuation.py` | [x] |
| 0-5 | 매수 후 **60분** 해당 시장 Phase5 판정 유예 | `guard.py`, `phase5_ops.py` | [x] |
| 0-6 | **US 쿨다운 해제** + 고점 리셋 | `scripts/reset_phase5_market_circuit.py` | [x] |
| 0-7 | 회귀 테스트 (B3·force 가드·risk equity) | `tests/` | [x] |

### Phase 1 — 역할 분리 (2~3일)

| # | 작업 | 상태 |
|---|------|------|
| 1-1 | `check_mdd_break` / KR·US 매매 사이클 → `market_equity_for_risk` | [x] |
| 1-2 | `_sanitize_*` 호출을 GUI·persist 경로로만 제한 | [x] |
| 1-3 | `PHASE5_ACCOUNT_CIRCUIT.md` — risk vs display 표 정리 | [x] |

### Phase 2 — sanitize 단순화 (1주)

| # | 작업 | 상태 |
|---|------|------|
| 2-1 | Case B2~B5 → 규칙 3개 (이중합산 / 예수미차감 / 급감거부) | [x] |
| 2-2 | `_buy_cash_guard` 제거 또는 규칙 3에 흡수 | [x] |
| 2-3 | 매수 직후 1~2루프 총평 고정 옵션 | [x] |

**규칙 요약 (`_sanitize_kis_cash_total_persist`):**

| 규칙 | 조건 | 동작 |
|------|------|------|
| **Rule1 이중합산** | 총평↑ + 예수 정체 + 보유↑ (또는 저예수 재오염) | `cash = prev_total − stock` |
| **Rule1b** | 보유 정체 + 예수≈`prev_cash+stock` 또는 예수≈`prev_total` + 총평↑ | `cash = prev_cash` / `prev_total−stock` |
| **Rule2 예수 미차감** | 최근 매수 + 예수 안 줄음 / 예수 0 | `cash −= recent_spend` |
| **Rule3 급감 거부** | 매도 없이 총평 10%↓ | `total = prev_total` 유지 |
| **write 상방 가드** | 매수·force 없이 총평 +5%↑ | 스냅샷 저장 거부 |
| **장외 freeze** | KR/US 비장중 `refresh_circuit_aux` | live로 snap 미갱신 |

`_buy_cash_guard` TTL 제거 → Rule3 + `write_kis_display_snapshot_part` 급감 가드.

### Phase 3 — 관측·운영 (지속)

| # | 작업 | 상태 |
|---|------|------|
| 3-1 | 루프마다 `risk_total` vs `snap_total` divergence 로그 | [x] |
| 3-2 | Phase5 발동 직전 **항상** KIS 강제 재조회 후 MDD 2차 판정 (괴리 조건 제거, 2026-08-24) | [x] |
| 3-3 | 통합 fixture: post_buy_us / post_buy_kr | [x] |

---

## 5. 설계 원칙 (Phase 1 이후 고정)

```
┌─────────────────────────────────────────────────────────┐
│  market_equity_for_risk(state, market)                  │
│  = display_cash_from_snapshot + Σ(qty × curr_p)         │
│  → Phase5 · 5% MDD · 비중 서킷 ONLY                     │
│  예외: ledger total < snap_total×0.95 → snap_total     │
│       (매도 직후 Orphaned Cash / 예수 미정산, 쿨다운 없음) │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  last_kis_display_snapshot (+ sanitize)                 │
│  → GUI · 텔레 · 리포트 ONLY                             │
│  → 서킷에 절대 직접 사용 금지                           │
└─────────────────────────────────────────────────────────┘
```

**Case B3 올바른 조건 (0-1, Phase 2 Rule2에 통합):**

- 발동: 예수 **미차감** + 최근 매수 (전액현금·저예수·스냅 총평 오염 등)
- 스킵: `nc ≈ prev_cash` **且** `nc + stock ≈ prev_total` (이미 정산된 스냅샷)

---

## 6. US 쿨다운 수동 해제

오발동 후 장부 상태 예:

```json
"account_circuit_market_cooldowns": { "US": "2026-08-21T04:47:39" },
"peak_equity_US": 3465.42,
"account_circuit_market_peak_reset_pending": { "US": true }
```

**해제:**

```bash
py -3.11 scripts/reset_phase5_market_circuit.py --market US
# 장부 미장 보유 0·스냅샷 총평만 있을 때:
py -3.11 scripts/reset_phase5_market_circuit.py --market US --peak 3381.33
```

동작:

1. `account_circuit_market_cooldowns.US` 삭제  
2. `peak_equity_US` → 현재 risk 총평(장부+스냅샷 예수)  
3. `account_circuit_market_peak_reset_pending.US` → false  
4. `phase5_pending_liquidation*` 에 US 있으면 제거  

봇 **재시작 불필요** (다음 루프부터 반영). 이미 떠 있으면 state 파일만 갱신됨.

---

## 7. 검증 체크리스트

매수 직후:

- [ ] 로그에 `persist 최근매수 예수 미반영` **없음** (정상 차감 시)
- [ ] `🛡️ [Phase5·US/KR]` DD가 매수 전후 **급변하지 않음**
- [ ] 스냅샷 총평 vs `[risk]` 로그 총평 diff **10% 미만**

서킷 발동 시:

- [ ] HTS/MTS 총평과 bot risk 총평 대조
- [ ] diff > 15%면 **오발동 의심** → 이 문서 Phase 0 재확인

---

## 8. 변경 이력

| 날짜 | 내용 |
|------|------|
| 2026-08-20 | Phase 3 — equity divergence 로그, Phase5 발동 전 KIS 재확인, 통합 fixture |
| 2026-08-20 | Phase 2 — sanitize Rule1/2/3, `_buy_cash_guard` 제거 |
