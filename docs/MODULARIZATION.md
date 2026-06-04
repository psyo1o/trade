# 모듈화 통합 가이드 (Single Source of Truth)

_최종 갱신: 2026-06-04_

이 문서는 기존
- `MODULARIZATION_WORKBOOK.md`
- `MODULARIZATION_NEXT_STEPS_2026-04-21.md`
- `MODULARIZATION_ROADMAP_2026-04-22.md`
- `MODULARIZATION_REVIEW_2026-04-21.md`

를 하나로 합친 **유일한 기준 문서**입니다.

---

## 1) 현재 상태 요약

### 완료된 단계
- A-1: `execution/phase5_ops.py` 분리 (Phase5·대기청산)
- A-2: `services/heartbeat_report.py`, `services/report_formatter.py`
- A-3: `services/account_display.py` (스냅샷/표시 진입점)
- A-4: `api/kis_parsers.py` output1/2 파서 확장 + `run_bot` 위임
- B-1: `execution/market_cycles/{kr,us,coin}_cycle.py` + `TradingCycleContext`
- B-2(1차): `strategy/entry_router.py`, `strategy/exit_router.py` 도입 및 market cycles 연동
- 장부: `services/trade_history_ledger.py` — 매매내역 BUY → `positions` 복구·보강 (`execution/ledger_apply.py` 병합 레이스 수정)

### 현재 병목
- `run_bot.py` (~5.1k): 오케스트레이션/헬퍼/수동매도/스크리너
- `strategy/rules.py` (~1.7k): V8/스윙 규칙 혼재
- `run_gui.py` (~2k): UI + 워커

---

## 2) 원칙 (회귀 방지)

- 한 번에 한 단계만 진행
- 동작 동일성 우선: 함수 이동/호출 위치 변경부터
- 저장/주문/멱등 타이밍 불변 (`save_state`, order idempotency)
- 단계별 검증 필수:
  - `py_compile`
  - 관련 `unittest`
  - 시장 사이클 스모크 1회

권장 검증 기본셋:

```powershell
py -3.11 -m py_compile run_bot.py run_gui.py strategy/rules.py execution/guard.py
py -3.11 -m unittest discover -s tests -p "test_*.py" -v
```

---

## 3) 다음 진행 단계 (실행 순서)

## Step B-2 (2차)
- 목표: 라우터 계약 강화
- 작업:
  - `entry_router`/`exit_router` 반환 타입 정리(TypedDict 또는 dataclass 통일)
  - market cycles 호출부 메타 전달 일관화
- 완료 기준:
  - 라우터/사이클 테스트 통과
  - direct `rules` 호출 없음 유지

## Step B-3
- 목표: 주문 실행 어댑터
- 파일: `execution/order_executor.py`
- 범위: 주문 실행 공통 패턴만 모으고 브로커 차이는 유지

## Step C-1
- 목표: 장부 스키마 검증
- 파일: `execution/state_schema.py`
- 테스트: `tests/test_state_schema.py`

## Step C-2 (선택)
- `strategy/rules.py` 스윙/V8 파일 분리 (`swing_rules.py`, `v8_rules.py`)
- 반드시 라우터와 세트로 진행

---

## 4) 테스트/관측 체크포인트

- 라우터:
  - `tests/test_strategy_router.py`
  - `tests/test_exit_routing.py`
- 시장 분리 회귀:
  - `tests/test_market_cycles_smoke.py`
- 파서 회귀:
  - `tests/test_kis_parsers.py`

운영 로그 핵심 태그:
- `[V8-BUY]`, `[SWING-BUY]`, `[SWING-SELL]`
- `[Phase5·KR|US|COIN]`
- `[매수 패스]`

---

## 5) 지금 바로 실행할 권장 작업

1. **B-2 2차**(라우터 계약 강화) 먼저
2. 완료 후 **B-3**(주문 실행 어댑터)
3. 이후 **state_schema**

---

## 6) 참고

- GUI/잔고 정책: `docs/KIS_GUI_DISPLAY.md`
- 장부·매매내역 정합: `docs/idempotency/LEDGER_RECONCILE.md`
- Phase5 정책: `docs/PHASE5_ACCOUNT_CIRCUIT.md`
- 프로젝트 인덱스: `PROJECT_STRUCTURE.txt`
