# 모듈화 통합 가이드 (Single Source of Truth)

_최종 갱신: 2026-06-06_

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
- B-1: `execution/market_cycles/{kr,us,coin}_cycle.py` + `TradingCycleContext` (중간 산출물 `_market_cycles_extracted.py` 제거)
- 하락장 헷지: `strategy/hedge_universe.py` (티커 하드코딩 단일 출처, `market_cycles/*_buy_cycle`)
- B-2(1차): `strategy/entry_router.py`, `strategy/exit_router.py` 도입 및 market cycles 연동
- B-2(1.5): KR/US 매수 → `execution/market_cycles/{kr,us}_buy_cycle.py` (`run_bot._run_*` 위임)
- B-2(2차): COIN 매수 → `execution/market_cycles/coin_buy_cycle.py`
- B-3: TWAP 매수 → `execution/order_executor.py` (`run_bot._execute_*` 위임)
- C-1: `execution/state_schema.py` — load/save **검증·경고만**, positions **비파괴** · save 시 `bot_state.bak` 자동 백업
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

(`tests/__init__.py` 로 discover 가능. Windows에서 emoji 로그 UnicodeError 시 `$env:PYTHONIOENCODING='utf-8'`)

---

## 3) 다음 진행 단계 (실행 순서)

## Step C-2 (선택)
- `strategy/rules.py` 스윙/V8 파일 분리 (`swing_rules.py`, `v8_rules.py`)
- 반드시 라우터와 세트로 진행

---

## 4) 테스트/관측 체크포인트

- 라우터:
  - `tests/test_strategy_router.py`
  - `tests/test_exit_routing.py`
- 시장 분리 회귀:
  - `tests/test_market_cycles_smoke.py` — **실제 `bot_state.json` 저장 금지** (`save_state`·스냅샷 mock)
- 장부 스키마:
  - `tests/test_state_schema.py`
- 파서 회귀:
  - `tests/test_kis_parsers.py`

운영 로그 핵심 태그:
- `[V8-BUY]`, `[SWING-BUY]`, `[SWING-SELL]`
- `[Phase5·KR|US|COIN]`
- `[Phase 4 발동]`, `[헷지 유니버스 KR|US]` — `docs/HEDGE_UNIVERSE.md`
- `[매수 패스]`

---

## 5) 지금 바로 실행할 권장 작업

1. **C-2**(선택): `strategy/rules.py` 스윙/V8 분리
2. `run_bot.py` 오케스트레이션 추가 축소 (수동매도·스크리너 등)

---

## 6) 참고

- GUI/잔고 정책: `docs/KIS_GUI_DISPLAY.md`
- 하락장 헷지 티커: `docs/HEDGE_UNIVERSE.md`, `strategy/hedge_universe.py`
- 장부·매매내역 정합: `docs/idempotency/LEDGER_RECONCILE.md`
- Phase5 정책: `docs/PHASE5_ACCOUNT_CIRCUIT.md`
- 프로젝트 인덱스: `PROJECT_STRUCTURE.txt`
