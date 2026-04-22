# 모듈화 다음 할 일 로드맵 (2026-04-21)

## 목적
- 현재 1~4단계 모듈화 이후, 추가로 어떤 작업을 하면 유지보수성이 더 올라가는지 정리한다.
- "무엇을 먼저/어떻게/어떤 기준으로" 진행할지 실행 가능한 형태로 제시한다.

## 현재 기준 (갱신)
- 완료:
  - 조회 계층 통합(`services/account_read_facade.py`)
  - snapshot 독립화(`services/account_snapshot.py` 의존성 주입형)
  - GUI 테이블 어댑터 분리(`services/gui_table_adapter.py`)
  - 트레이딩 사이클 앞단 단계화(1차)
- 완료 추가:
  - 5단계 `KIS 파싱 공통화` (`api/kis_parsers.py` + `tests/test_kis_parsers.py`)
  - 고도화 1번 `run_trading_bot` 본문 2차 분리(저위험 helper 추출 중심)
  - 관측성: `account_read_facade` / `account_snapshot` / 매수 루프 — 폴백·스킵 시 한 줄 로그

---

## A. 다음 우선순위 (5단계 이후)

### A-1) 트레이딩 루프 2차 분리 (강력 권장) — 완료
- 대상:
  - `run_trading_bot()`의 KR/US/COIN 매도·매수 본문
- 목표:
  - 시장별 실행 단위를 함수로 분리해 회귀 범위를 축소
- 완료 기준:
  - 로그/매매 결과 동일성 유지
  - 시장별 실패 격리(한 시장 장애가 전체 흐름을 덜 오염)
- 적용 메모:
  - 통 함수 분리 대신 저위험 helper 추출 중심으로 2차 분리 완료
  - 공통 계산/조건/로그 블록을 모듈화해 본문 복잡도를 낮춤

### A-2) KIS 파서 확장 (옵션)
- 후보 파일: `api/kis_parsers.py`
- 목표:
  - 현재 도입한 핵심 파서 외에 `output1` 파생 계산(평가금/평단/현재가)까지 확장
- 제안 함수 예시:
  - `parse_kr_positions(output1, ...)`
  - `parse_us_positions(output1, ...)`
- 완료 기준(DoD):
  - `run_bot.py`, `run_gui.py`, `services/account_snapshot.py`에서 파싱 중복 코드 제거
  - `list has no attribute get` 류 방어가 파서 내부로 일원화

### A-3) 파싱 회귀 테스트 확대
- 현재 상태:
  - `tests/test_kis_parsers.py` 최소 세트 구축 완료
- 다음 목표:
  - 실제 운영 로그 기반 샘플(빈 output1, 누락 필드, 타입 혼합) 케이스 추가

---

## B. 중기 우선순위 (운영 안정성 강화)

### B-1) `run_trading_bot` 시장별 루프 3차 분리 (선택적)
- 현재 상태:
  - 2차 분리(저위험 helper 추출)는 완료
- 다음 목표:
  - 필요 시 KR/US/COIN 루프를 시장 단위 함수로 한 단계 더 크게 분리
- 권장 함수 예시:
  - `_run_kr_cycle(state, weather, macro_mult, ...)`
  - `_run_us_cycle(state, weather, macro_mult, ...)`
  - `_run_coin_cycle(state, weather, macro_mult, ...)`
- 완료 기준:
  - 함수 분리 후에도 로그/매매 결과 동일
  - 사이클 단위 에러 범위가 시장별로 격리됨

### B-2) 메시지/로그 빌더 분리
- 후보 파일: `services/report_formatter.py`
- 목표:
  - heartbeat 텔레그램 메시지 포맷과 운영 로그 문구 조립을 분리
- 효과:
  - 메시지 변경 시 엔진 코드 영향 최소화

---

## C. 선택 과제 (시간 여유 있을 때)

### C-1) 타입 강화 (TypedDict/dataclass)
- 대상:
  - snapshot payload, holdings row, parser output 스키마
- 효과:
  - 필드 오타/누락을 정적 검사로 조기 발견

### C-2) 정책 객체화
- 대상:
  - `allow_kis_fetch`, `with_backoff`, 쿨다운 정책
- 효과:
  - GUI/헤드리스 정책을 설정 객체로 명확히 분리 가능

---

## 추천 실행 순서 (추가 모듈화, 갱신)
1. 파서 확장(`output1` 파생값)
2. 파서 테스트 케이스 확대
3. 메시지/로그 포맷터 분리
4. 타입 강화
5. 정책 객체화(호출/백오프/쿨다운)

---

## 리스크와 대응
- 리스크: 파싱 공통화 중 숫자 필드 단위(원/달러) 혼동
  - 대응: 함수명에 단위 명시(`*_krw`, `*_usd`) + 테스트 케이스 고정
- 리스크: 시장별 루프 3차 분리 시 상태 저장 타이밍 변경
  - 대응: 저장 지점/호출 순서는 기존과 동일하게 유지하고 함수 경계만 이동
- 리스크: 성능 저하
  - 대응: 호출 횟수 증가 금지, 기존 공용 fetch 1회 원칙 유지

---

## 한 줄 결론
- 초기 5단계 모듈화와 고도화 1번(트레이딩 루프 2차 분리)은 완료되었고, 다음 핵심은 **파서/테스트 확장 + 타입 강화**이다.

