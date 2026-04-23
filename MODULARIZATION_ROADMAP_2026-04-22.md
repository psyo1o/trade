# 모듈화 로드맵 (멀티전략 기준, 2026-04-22)

## 목적
- 현재 반영된 구조(V8 + SWING 공존, Phase5 Monday Trailing MDD, 관측성 로그 강화)를 기준으로
  **회귀 리스크를 낮추면서 유지보수성을 높이는 다음 모듈화 순서**를 제시한다.
- 원칙은 "대형 리라이트 금지, 동작 동일성 유지, 작은 단위 분리"다.

## 현재 기준선(요약)
- 이미 완료:
  - 조회 계층 통합: `services/account_read_facade.py`
  - 스냅샷 공용화: `services/account_snapshot.py`
  - GUI 테이블 어댑터 분리: `services/gui_table_adapter.py`
  - KIS 파싱 공통화: `api/kis_parsers.py` + `tests/test_kis_parsers.py`
  - 트레이딩 루프 2차 분리(저위험 helper 추출)
  - Phase5 월요일 주차 트레일링 MDD 반영
  - 멀티전략 최소 통합: 진입(V8 우선, Swing 보조) / 매도(`strategy_type` 분기)
- 현재 핵심 병목:
  - `run_bot.py` 오케스트레이션 본문이 여전히 큼(시장별 매수/매도 루프 혼재)
  - 전략 진입/청산 분기와 주문 실행이 같은 레이어에 섞여 있음
  - 장부(`positions`) 키 확장(`strategy_type`, `entry_fib_level`, `scale_out_done`, `qty`)에 대한 검증/테스트가 부족

---

## 권장 모듈화 우선순위 (P0 -> P2)

### P0. 시장별 사이클 핸들러 분리 (최우선)
- 목표:
  - `run_trading_bot()`에서 시장별 실행 블록을 함수 경계로 분리
  - 예: `_run_kr_cycle(...)`, `_run_us_cycle(...)`, `_run_coin_cycle(...)`
- 범위:
  - "함수 이동 + 의존성 인자화"만 수행(로직 변경 금지)
  - 호출 순서(동기화 -> 리스크 -> 매도 -> 매수)는 유지
- 기대 효과:
  - 회귀 범위를 시장 단위로 격리
  - 버그 발생 시 KR/US/COIN 영향 범위를 빠르게 좁힘
- DoD:
  - 기존 로그 태그/순서가 동일하게 유지
  - 1사이클 실행 결과(매도/매수/스킵 사유)가 분리 전후 동일

### P0. 전략 라우터 계층 신설
- 목표:
  - "어떤 전략으로 진입/청산할지" 결정을 주문 코드와 분리
- 제안 파일:
  - `strategy/entry_router.py`
  - `strategy/exit_router.py`
- 권장 인터페이스:
  - `decide_entry_strategy(df, context) -> (strategy_type, meta)`
  - `decide_exit_action(pos_info, df, context) -> (action, reason, meta)`
- 기대 효과:
  - V8/SWING 외 신규 전략 추가 시 `run_bot.py` 수정 범위 최소화
  - `strategy_type` 분기 일관성 확보(진입과 청산의 계약 정렬)

### P1. 주문 실행 어댑터 통합 (KR/US/COIN 공통 패턴)
- 목표:
  - 현재 시장별로 유사한 "수량 계산 -> 시장가/TWAP -> 장부 반영" 패턴을 공통 인터페이스로 정리
- 제안 파일:
  - `execution/order_executor.py` (시장별 브리지 호출만 분기)
- 핵심:
  - 주문 타입(일괄/분할), 최소 주문 금액, 반올림/truncate 정책을 한 곳에서 표준화
- 기대 효과:
  - "시장별 처리 차이"는 유지하면서 중복 감소
  - 부분청산(HALF) 관련 버그 가능성 감소

### P1. 포지션 장부 스키마 검증 계층 신설
- 목표:
  - `positions[*]`에 대한 최소 스키마/기본값/타입 보정을 load/save 경계에서 통일
- 제안 파일:
  - `execution/state_schema.py`
- 권장 항목:
  - 필수/선택 키 정리: `qty`, `strategy_type`, `entry_fib_level`, `scale_out_done`, `entry_atr`, `current_atr`
  - 기본값/정규화 규칙 제공
- 기대 효과:
  - "구버전 장부 + 신규 키" 혼합 상황에서 예외 감소

### P2. 리포트/알림 포맷터 분리
- 목표:
  - heartbeat/수동매도/오류 알림 문자열 조립을 엔진 로직에서 분리
- 제안 파일:
  - `services/report_formatter.py`
- 기대 효과:
  - 로그/메시지 변경 시 트레이딩 로직 영향 최소화

### P2. 테스트 확장 (모듈화 완료 조건)
- 목표:
  - 최소한 다음 3개는 자동화:
    1) 전략 라우팅(V8 우선 -> Swing fallback)
    2) `strategy_type=SWING_FIB` 시 V8 매도 미진입 보장
    3) 장부 스키마 보정(누락 키/구버전 키 케이스)
- 제안 파일:
  - `tests/test_strategy_router.py`
  - `tests/test_exit_routing.py`
  - `tests/test_state_schema.py`

---

## 단계별 실행안 (안전 순서)

1. **사이클 함수 경계만 분리** (`run_bot.py` 내부 함수화)
2. **전략 라우터 도입** (기존 `check_swing_entry/exit`, `calculate_pro_signals` 호출 위치만 이동)
3. **주문 실행 어댑터 도입** (기존 주문 함수 재사용)
4. **장부 스키마 검증 계층 추가**
5. **리포트 포맷터 분리**
6. **테스트 확대**

각 단계마다 "동작 변경 없음"을 우선하고, 실패 시 즉시 롤백 가능한 작은 커밋 단위로 진행한다.

---

## 리스크와 방지책
- 리스크: 함수 분리 중 저장 타이밍 변경
  - 방지: `save_state`, `record_trade`, cooldown 기록 위치를 이동하지 말고 호출만 래핑
- 리스크: 전략 라우터 도입 후 분기 누락
  - 방지: 기본값을 항상 `TREND_V8`로 두고 미인식 전략은 V8 경로로 fail-safe
- 리스크: 코인 수량 처리 오차
  - 방지: 기존 `truncate`/최소주문 규칙을 공통 어댑터에서 그대로 재사용

---

## 완료 정의(DoD)
- 기능 DoD:
  - 스윙 포지션은 끝까지 스윙 청산 규칙만 사용
  - V8 포지션은 기존 청산 규칙 유지
  - Phase5(월요일 리셋/쿨다운 후 고점 리셋) 동작 불변
- 품질 DoD:
  - `py -3.11 -m py_compile run_bot.py run_gui.py strategy/rules.py execution/guard.py`
  - 신규 테스트 통과
  - 로그 태그(`[V8-BUY]`, `[SWING-BUY]`, `[SWING-SELL]`, `[Phase5 서킷]`) 유지

---

## 한 줄 결론
- 지금 시점에서 가장 효과적인 다음 모듈화는 **시장별 사이클 분리 + 전략 라우터 분리**이며,
  이 두 축을 먼저 끝내면 이후 주문/장부/리포트 분리가 안전하게 따라온다.

