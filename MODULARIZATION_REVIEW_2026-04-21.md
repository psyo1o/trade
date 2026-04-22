# 모듈화 진행 리포트 (2026-04-21 갱신)

## 목적
- 현재 구조에서 **속도/안정성 유지**를 전제로 추가 모듈화 가능 지점을 식별한다.
- "어디를 먼저 분리해야 효과가 큰지"를 우선순위로 제시한다.

## 현재 구조 진단 요약 (갱신)
- `run_bot.py`는 여전히 대형 오케스트레이터지만, 조회/스냅샷/GUI 테이블 구성 결합은 이전 대비 크게 완화됨.
- `run_gui.py`는 UI + 워커 제어 중심으로 정리 중이며, 테이블 조립 로직은 서비스로 분리 완료.
- `services/account_snapshot.py`는 `run_bot` 직접 import를 제거하고 의존성 주입형으로 전환 완료.
- `KIS 파싱 공통화(5번)`와 고도화 1번(`run_trading_bot` 본문 2차 분리)은 완료됨.
- 남은 핵심 과제는 시장별 루프 3차 분리(선택)와 파서 확장/테스트 강화/타입 강화 영역.

## 우선순위 높은 모듈화 후보

### 1) Account Read Facade 분리 (최우선) — 완료
- 결과 파일: `services/account_read_facade.py`
- 대상 로직:
  - `run_bot.py`의 `get_held_stocks_kr/us/coins`, `get_held_stocks_*_info/detail`
  - KIS 응답 파싱(`output1/output2`) 및 장부 폴백 규칙
- 기대 효과:
  - GUI/heartbeat/트레이딩 루프가 동일한 조회 계약(Contract)을 공유
  - "장중 API 실패 시 빈 테이블" 류 이슈 재발 감소
- 적용 결과:
  - `run_bot.py`의 보유 조회 함수들은 동일 시그니처 래퍼를 유지하고 내부를 facade 호출로 치환.
  - 동작 동일성 유지(기존 호출부 수정 최소화).

### 2) Snapshot Service 독립화 (높음) — 완료
- 이전 이슈:
  - `services/account_snapshot.py`가 `import run_bot`에 강결합
- 적용 결과:
  - `resolve_display_current_price(...)` / `build_account_snapshot_for_report(...)`를 의존성 주입형으로 전환
  - `run_bot.py`는 얇은 래퍼로 deps를 주입해 기존 공개 API 유지
- 효과:
  - 순환참조 리스크 완화
  - 서비스 단위 테스트 가능성 상승

### 3) GUI Data Adapter 분리 (높음) — 완료
- 결과 파일: `services/gui_table_adapter.py`
- 대상 로직:
  - `BalanceUpdaterThread.run()` 내 rows 생성/시장별 분기/표시 포맷 변환
- 적용 결과:
  - `BalanceUpdaterThread.run()`의 테이블 rows 조립 분기(국/미/코인, 장중/장외 폴백)를 adapter로 이동
  - `run_gui.py`는 어댑터 호출 + UI 반영에 집중

### 4) Trade Cycle 단계 함수화 (중간~높음) — 2차 완료 (고도화 1번 완료)
- 적용 범위:
  - 앞단 공통 단계 분리
    - `_prepare_cycle_state()`
    - `_sync_positions_for_cycle(state)`
    - `_build_market_context(state) -> (weather, macro_mult)`
  - 본문 2차 저위험 분리(동작 동일성 유지)
    - KR/US/COIN 공통 계산/조건/로그 보조 helper 다수 추출
    - 예: 보유시간 계산/로그, GUI 현재가 override, 수익률/하드스탑/타임스탑 조건, 코인 전처리 반복 블록
- 비고:
  - 시장별 대형 루프를 통째로 이동하지 않고, 저위험 블록 단위로 분리하여 회귀 리스크를 최소화.

### 5) KIS Parsing 전용 모듈 (중간) — 완료
- 결과 파일:
  - `api/kis_parsers.py`
  - `tests/test_kis_parsers.py`
- 적용 내용:
  - `output2` list/dict 정규화(`as_row_dict`)
  - KR 예수금/총평가 추출(`parse_kr_cash_total`)
  - US 외화예수금 fallback 추출(`parse_us_cash_fallback`)
  - US 보유수량 추출 통합(`parse_us_qty`)
  - `run_bot.py`, `services/account_snapshot.py`, `services/account_read_facade.py`에 적용
- 검증:
  - `python tests/test_kis_parsers.py` 통과
- 후보 파일: `api/kis_parsers.py`
- 대상:
  - `output1/output2` 리스트/딕트 혼합 파싱
  - 잔고/예수금/평가액/평단/현재가 공통 추출
- 기대 효과:
  - `list has no attribute get` 류 파싱 오류 방지
  - KR/US 파싱 규칙 재사용

## 지금 당장 보류해도 되는 후보
- 전략 규칙(`strategy/rules.py`)의 세부 인디케이터 분해: 효과는 크지만 회귀 리스크도 큼.
- 텔레그램 메시지 템플릿 완전 분리: 가독성은 좋아지지만 현재 장애 빈도 대비 우선순위 낮음.

## 권장 실행 순서 (안전 우선)
1. `account_read_facade` 신설 (완료)
2. `account_snapshot`의 `run_bot` 직접 의존 제거 (완료)
3. GUI 테이블 조립 로직 adapter 이동 (완료)
4. `run_trading_bot()` 단계 분할 1차 (완료)
5. KIS 파서 통합 (완료)

## 현재 진행률
- 단계 기준: **5 / 5 완료 (100%)**
- 고도화 1번(`run_trading_bot` 본문 2차 분리): **완료**
- 운영 관점 모듈화 성숙도: **약 95%+**

## 단계별 완료 기준 (DoD)
- 공통:
  - `py_compile` 통과
  - GUI 시작/heartbeat/트레이드 1사이클 로그 정상
  - 장외/장중/강제새로고침에서 라벨/테이블 누락 없음
- 계좌 조회 계층:
  - KR/US/COIN 각각 "정상 응답 / 빈 응답 / 예외" 시 폴백 동작이 동일

## 관측성(운영 로그) 보강 — 2026-04-22
- `run_bot.py`: 국·미·코인 **매수** 경로에서 예산·예수·최소주문·정수주 0·TWAP 미체결·BEAR+ADX 스킵 등
  조용한 `continue`를 `[KR …]`, `[US …]`, `[COIN …]` 태그 로그로 정리. 모듈 docstring·섹션 주석에 추적 순서 명시.
- `services/account_read_facade.py`: 주말/장부 폴백/API 빈 응답 시 **한 줄 이유 로그** (`📌`/`⚠️`) 추가, 모듈 docstring에 로그 정책 기술.

## Phase 5 합산 서킷 — 월요일 주차 트레일링 MDD (갱신)
- **장부 키:** `peak_total_equity`, `last_reset_week`(예 `2026-W16`), `account_circuit_peak_reset_pending`; 레거시 `peak_equity_total_krw`는 동일 값 미러.
- **동작:** 서울 기준 **월요일**에 새 ISO 주차가 `last_reset_week`와 다르면 고점을 당시 합산 총자산으로 덮어쓴 뒤 주차 갱신; 그 외에는 고점 상향만 추적. 서킷 **쿨다운 만료 후** 한 번 고점을 현재 총자산으로 리셋해 연쇄 발동을 완화.
- **발동:** `(peak - current) / peak * 100 >= account_circuit_mdd_pct`(기본 15%) — `execution/circuit_break.evaluate_total_account_circuit` + `execution/guard.apply_phase5_trailing_week_and_cooldown`.

## 리스크 및 방지책
- 리스크: 조회 경로를 건드릴 때 GUI 표시가 사라질 수 있음
  - 방지: "빈 리스트 반환 금지, 항상 source=ledger라도 반환" 원칙
- 리스크: 서비스 분리 중 순환 import 발생
  - 방지: 상위 모듈(`run_bot`) 참조 제거 + 의존성 주입
- 리스크: 성능 저하
  - 방지: 현재처럼 스레드 유지, 호출 횟수는 늘리지 않고 공용 fetch 1회 원칙 유지

## 결론
- 저리스크 고효율 축(조회 통합 + snapshot 독립화)은 완료되었고, 실제 운영 안정성에 유의미한 개선이 반영됨.
- 초기 계획한 5단계 모듈화 + 고도화 1번(본문 2차 분리)이 완료되었음.
- 다음 개선 축은 파서 확장/테스트 강화, 타입 강화, 메시지 포맷터 분리 영역.

