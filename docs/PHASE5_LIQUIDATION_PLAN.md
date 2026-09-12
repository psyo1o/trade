# Phase5 청산 설계 (잔고 MDD + AI 오류판별)

**최종 갱신:** 2026-09-08  
**한 줄:** **지수 급락 = 매수 패스만.** **청산 = 시장별 잔고 고점 15% + KIS 재검증 + AI(실폭락 vs 데이터오류).**

관련: [`PHASE5_ACCOUNT_CIRCUIT.md`](PHASE5_ACCOUNT_CIRCUIT.md), [`EQUITY_SNAPSHOT_CIRCUIT_PLAN.md`](EQUITY_SNAPSHOT_CIRCUIT_PLAN.md)

---

## 1. 역할 분리 (헷갈리면 여기만 보면 됨)

| 기능 | 보는 것 | 동작 | 시장 |
|------|---------|------|------|
| **지수 급락** `INDEX_CRASH_*` | 벤치마크 **당일** 등락 (KODEX200/SPY/BTC) | **신규 매수만 중단** | KR·US·COIN **동일** |
| **종목 전고점** (스윙 천장 등) | 개별 종목 | **매수 패스** | 종목 단위 |
| **Phase5 청산** | `peak_equity_{KR\|US\|COIN}` vs `market_equity_for_risk` | 해당 시장 **전량 청산 후보** | KR·US·COIN **동일** |

**금지**

- 지수 6개월 고점 −15% → 청산 ❌ (2026-09-08 오발동: KODEX200)
- 종목 고점 −15% → 청산 ❌
- GUI 스냅샷만 믿고 청산 ❌ (과거 TLT·411060 B3 오발동)

---

## 2. 청산 파이프라인 (KR = US = COIN)

```
① 잔고 MDD ≥ 15% (peak_equity_* vs 현재 — KR/US risk, COIN native 견적통화)
      ↓
② 장부 보유 0 → 스킵
③ 매수 직후 유예(60분) → 스킵
      ↓
④ 청산 직전 재검증 (루프 내 sleep 없음)
   - KR/US: KIS 강제 재조회 → risk 재계산
   - 1회차 후에도 ≥15% → `phase5_reconfirm_pending` 저장, **이번 주기 보류**
   - **다음 봇 주기**에 2회차 재조회 → 여전히 ≥15%만 AI로
   - 중간에 MDD 해제되면 pending 삭제·청산 취소
   - COIN: 거래소 잔고 견적통화(업비트 원 / 바이낸스 USDT)
      ↓
⑤ AI 심사 (이미 있는 Phase5 AI)
   - 입력: 잔고 peak/current, cash·holdings, snap vs risk 괴리,
           보유 종목 PnL, (참고) 지수 당일/중기 DD, 최근 매도·정산 의심 플래그
   - 점수 ≥70 이고 llm_success 일 때만 청산
   - 데이터 오류·정산 지연 의심 → 낮은 점수 → 보류
      ↓
⑥ 해당 시장만 청산 + 24h 매수 쿨다운
```

**지수 MDD 청산 경로(`account_circuit_use_index`)는 폐지.** 지수는 매수 게이트·AI 참고용만.

---

## 3. AI가 구분해야 할 두 케이스

### A. 진짜 폭락 → 청산 OK

- risk 총평이 고점 대비 실질적으로 빠짐
- KIS 재조회 후에도 동일
- 보유 평가도 같이 하락 (또는 현금화 후 총자산 자체가 감소)
- (참고) 지수도 같이 약세면 설득력↑ — **필수 조건 아님**

### B. 잔고/스냅샷 오류 → 청산 금지

과거 사례:

- 매수 직후 sanitize B3로 총평만 반토막 (실계좌는 정상)
- 매도 후 예수 미반영 → risk 증발처럼 보임
- snap과 ledger 괴리 큼

AI 입력에 **괴리율·정산 지연 플래그·cash/holdings 분해**를 넣어 “숫자가 이상하면 낮은 점수”를 강제.

---

## 4. 구현 체크리스트

| # | 작업 | 상태 |
|---|------|------|
| 1 | Phase5 기본 = 잔고 MDD (KR/US/COIN 동일) | [x] 2026-09-08 |
| 2 | 지수 청산 경로 제거/비활성 | [x] |
| 3 | AI 컨텍스트: risk/snap 괴리·정산 의심·지수 참고 | [x] |
| 4 | AI 프롬프트: 오류 vs 실폭락 루브릭 강화 | [x] |
| 5 | 문서·CURRENT·CHANGELOG·README 역할표 | [x] |
| 6 | 단위 테스트 (컨텍스트 필드·프롬프트 키워드) | [x] |
| 7 | 라이브 스모크 (키 있을 때) | [ ] 재시작 후 `smoke_phase5_ai.py` |

---

## 5. config

| 키 | 의미 | 기본 |
|----|------|------|
| `account_circuit_mdd_pct` | 잔고 고점 대비 청산 임계 | 15 |
| `phase5_ai_liquidation_enabled` | AI 최종 승인 | true |
| `phase5_ai_liquidation_threshold` | AI 점수 하한 | 70 |
| `phase5_reconfirm_next_cycle` | 1회 재조회 후 다음 주기에 2회차 | true |
| `phase5_reconfirm_delay_sec` | (legacy) 루프 내 sleep — **미사용** | 0 |
| `INDEX_CRASH_KR/US/COIN` | **매수 중단만** (코드 상수) | -3.0 / -1.8 / -3.5 |

`account_circuit_use_index` — **사용하지 않음 (청산용 폐지).**
