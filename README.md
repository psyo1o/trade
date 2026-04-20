# c-bot 운영 개요

_업데이트: 2026-04-19 — V7.1(액티브 전용·분할 익절)·`scale_out`·장부 `scale_out_done` 반영; `adjust_capital.py`·`circuit_aux`·텔레그램 보유시간_

**일반 운영:** `run_bot.bat` 또는 `py -3.11 run_gui.py` 로 **GUI만** 쓰면 됩니다.  
`run_bot.py` 단독(헤드리스)은 콘솔 서버·개발용으로만 쓰는 선택 경로입니다.

## 1) 최상위 실행/설정 파일

- `run_bot.py`
  - 국장/미장/코인 통합 자동매매 메인 엔진.
  - `config.json`, `bot_state.json`, `trade_history.json`을 읽고 쓰며,
    `strategy/*` + `execution/*` + `api/*` 모듈을 오케스트레이션.
  - 핵심 루프: `run_trading_bot()` (동기화 → 매도/리스크 → 신규매수).
  - KIS **잔고 표시 스냅샷**(`last_kis_display_snapshot` 저장/로드): 평일 조회 성공 시 갱신,
    증권사 주말 점검 창에서는 텔레·GUI에 직전 값 재사용.
  - Phase 5 합산 MDD용 **`circuit_aux_last_*`** (국·미·코인 스냅샷): 매매 루프에서 실조회 후 갱신.
    **`refresh_circuit_aux_from_brokers(state, path)`** 로 동일 키만 브로커 기준 재기록 가능(`adjust_capital.py` 등).
  - 텔레그램 **생존신고·보유 종목 줄**: 장부 `buy_time` / `buy_date` 기준 **보유 기간** 접미사(` | 보유 N일 …`).
    **수동 매도** 성공 알림에도 동일.

- `adjust_capital.py`
  - **수동 입·출금** 반영 시 Phase 5 **`peak_equity_total_krw`(합산 고점)** 만 입금액만큼 가산/출금액만큼 감산.
  - 실행 시 먼저 **`refresh_circuit_aux_from_brokers`** 로 잔고 스냅샷을 맞춘 뒤 금액 입력(출금 한도·총자산 일치 완화).
  - 기록은 `capital_adjustments` 배열에 append·`bot_state.json` 저장. (`config.json` 필요, `py -3.11 adjust_capital.py`)

- `run_gui.py`
  - PyQt5 기반 운영 GUI.
  - `run_bot` 모듈을 import해서 엔진/잔고/수동매도/로그를 UI로 제어.
  - **고점 보정(입출금) 탭**: `adjust_capital.py`와 동일 로직으로 `circuit_aux_*` 갱신 후 `peak_equity_total_krw`를 가산/감산하고, `capital_adjustments`에 기록.
  - 매매 타이머는 KST **분기**(`:00/:15/:30/:45`); 텔레그램 heartbeat 는 **기동 즉시 1회**
    뒤 KST **`:00/:30`** 정렬(매매 주기와 별도).
  - KIS 주말 점검 창에는 국·미 **API를 부르지 않고** `last_kis_display_snapshot` + 장부로 상단·테이블을 표시.
    보유종목 테이블의 **수량**은 `bot_state.json` 의 `positions[티커].qty`(평일 동기화·매수 시 저장)를 사용하고, 없으면 예전과 같이 표시용 `1` 폴백.
  - 설정(`config.json`)은 `run_bot` import 시 로드되므로 변경 후 GUI 재시작 필요.

- `screener.py`
  - 한국투자 API 기반 **국장** 스크리너.
  - HTS 조건검색 결과를 합쳐 `kr_targets.json` 에 저장.
  - 스케줄러(`run_bot.start_scanner_scheduler`)에서 **매 거래일 14:50 KST**
    (국장 매수 창 전형적으로 15:00 직전이므로 **약 10분 전 갱신**) 에 실행.

- `us_screener.py`
  - **미장 감시 유니버스** 재빌드 전용 (`run_bot.get_top_market_cap_tickers(force_refresh=True)` 호출).
  - 결과는 `us_universe_cache.json` 에 반영(GICS 섹터 포함).
  - 스케줄러에서 **매 거래일 15:20 US/Eastern**
    (미장 매수 창 **15:30 ET 기준 약 10분 전**, DST 자동 반영).

- `config.json`
  - API 키, 텔레그램, `test_mode`, TWAP, 거시필터 등 런타임 설정.

- `bot_state.json`
  - 장부/상태 파일.
  - `positions`(티커별 `buy_p`, `sl_p`, `tier`, **`qty`** 등): **`qty`** 는 평일 `sync_all_positions` 시 실계좌 수량과 맞추고,
    매수 체결 시에도 기록되어 **주말(KIS 미조회) GUI 보유수량 표시**에 사용된다. 구버전 장부에 없으면 첫 평일 동기화·매매 후 채워짐.
  - `cooldown`(단기), `ticker_cooldowns`(매도 후 익절/손절·타임스탑 등 사유별 **재진입 쿨다운** ISO 시각),
    `stats`, Phase5 키 저장.
  - **Phase 5 / 합산 서킷:** `peak_equity_total_krw`(합산 평가 고점), `account_circuit_cooldown_until`(쿨다운 만료 시각),
    **`circuit_aux_last_kr_krw`** / **`circuit_aux_last_usd_total`**(미장 합산 USD) / **`circuit_aux_last_coin_krw`**
    — 루프마다 또는 `refresh_circuit_aux_from_brokers` 로 갱신·원화 합산 시 환율(`estimate_usdkrw`) 적용.
  - **`capital_adjustments`**: `adjust_capital.py` 가 남긴 입출금·고점 보정 이력(선택).
  - `last_kis_display_snapshot`: 국·미 **예수금·총평가·수익률** 및 `saved_at`(직전 KIS 조회 성공 시각).
    주말 점검 구간에는 API를 부르지 않고 이 스냅샷으로 GUI·텔레 생존보고 숫자를 채움.

- `us_universe_cache.json`
  - 미장 **S&P500 시총 상위 100 + Nasdaq100 중 Tier1 제외 상위 50**(최대 150종) 및 GICS 섹터 캐시.
  - **24시간 TTL** 후 재조회; `us_screener` 가 강제 재빌드할 때도 갱신.
  - 위키 HTML 파싱 실패 시 S&P100 단독 등으로 단계적 폴백(구현은 `run_bot.py` 참고).

- `trade_history.json`
  - 매매 이벤트 append 기록.

## 2) 디렉터리별 역할

- `api/`
  - 외부 브로커/데이터 API 래퍼 레이어.
  - `kis_api.py`: 한국투자 토큰/브로커/잔고/주문.
  - `upbit_api.py`: 업비트 잔고/주문.
  - `macro_data.py`: VIX, Fear&Greed 원시 지표 수집.

- `strategy/`
  - 매수/매도 판단 로직(규칙·필터).
  - `rules.py`: V5 전략 코어(OHLCV, 시그널, 청산 가격). `yfinance` 재시도·백오프, 국내 6자리는
    KIS 일봉 래퍼(`get_ohlcv_kis_domestic_daily` 등)와 병행.
  - `sector_lock.py`: 동일 GICS 섹터 과집중 제한(국·미 각각 한도는
    `max(1, (max_positions_XX + 1) // 2)` 형태로 **올림** 적용).
  - `ai_filter.py`: false-breakout(휩쏘) 필터.
  - `macro_guard.py`: 거시 방어막 정책(차단/축소/정상).

- `execution/`
  - 실행/리스크/동기화 유틸.
  - `guard.py`: 장부 I/O, `cooldown` / `ticker_cooldowns`, MDD, 합산 서킷, `can_open_new`(시장별 슬롯);
    매도 사유별 `ticker_cooldowns` 만료 시각 설정·조회 유틸. `load_state` 시 `positions[*].scale_out_done` 기본값 보강.
  - `sync_positions.py`: 실계좌↔장부 자동복구/유령정리/평단보정·**보유수량(`qty`) 반영**(평일 API 시드).
  - `order_twap.py`: 분할 매수/매도(TWAP) 슬라이스 계획/실행.
  - `scale_out.py`: **V7.1 조건부 50% 분할 익절(Scale-Out)** — 조건·수량·최소명목·장부 보정(`post_partial_ledger`)·국·미 수량 TWAP·코인 청크 매도.
  - `circuit_break.py`: 합산 자산 drawdown 판정 유틸.

- `utils/`
  - 공통 보조 모듈.
  - `logger.py`: 자정 롤오버(30일) 로깅 + stdout/stderr 리다이렉트.
  - `telegram.py`: 알림 및 atexit 종료 핸들러. 메시지 본문은 호출부(`run_bot` heartbeat·수동매도 등)에서 구성.
  - `helpers.py`: 티커 정규화, 이름 조회, JSON 입출력, 코인 최소수량 기준,
    분·쿼터·반시간 정렬(`seconds_until_next_half_hour` 등),
    **`kis_equities_weekend_suppress_window_kst()`** — KST 기준 증권사 주말 점검 창에서 **국·미 KIS 호출 차단** 여부 판단(업비트 제외).

- `tests/`
  - `test_lab.py`: Phase 샌드박스/실험 스크립트.

## 3) 현재 핵심 공통 상수/정책

- **V7.1 액티브 전용:** 코어 자산 예외 목록은 **없음**. 계좌에 있는 모든 종목이 매도·샹들리에·슬롯 카운트 대상이다.
- **분할 익절(Scale-Out):** `execution/scale_out.py` + `run_bot.py` 매도 루프(국·미·코인).
  - 장부: `positions[티커].scale_out_done` — `false`(또는 없음)이면 미실시, 조건 충족 시 **1회** 분할 매도 성공 후 `true`(이미 분할 익절함·동일 포지션에서 재시도 안 함). 신규 매수로 다시 잡히면 새 항목부터 다시 `false`.
  - 조건(요약): 수익률 ≥ **30%**, 진입·평가 중 큰 명목(원화) ≥ **300만 원**, `scale_out_done == false`.
  - 수량: 국·미 정수 주 `// 2`(1주만 있으면 스킵); 코인은 `0.5×` 후 소수 **truncate**. 최소 매도 명목은 국·미 **1주 시가**, 코인 **5,000원**.
  - 주문: 매도 명목이 `config.json`의 **TWAP 기준**(원화·미장은 USD→원화 환산) 초과 시 분할 시장가, 이하이면 일괄 시장가. 성공 시 장부 `qty`·`buy_p` 등 보정.
- 코인 먼지 수량 기준: `utils/helpers.py`
  - `COIN_MIN_POSITION_QTY (= 0.0001)`
  - `coin_qty_counts_for_position(qty)`로 실질 보유 여부 통일 판정.

## 3-1) Phase 1~5 (운영 레이어 매핑)

코드와 로그에 **`PhaseN`** 이라 붙어 있는 기능 묶음입니다.
(`tests/test_lab.py` 에 동일 이름의 샌드박스 블록도 있습니다.)

| Phase | 역할 | 주요 모듈·경로 |
|-------|------|----------------|
| **Phase 1** | 동일 **GICS 섹터** 과다 보유 방지 — 신규 매수 전 섹터 한도 검사 | `strategy/sector_lock.py` (`allow_kr_sector_entry`, `allow_us_sector_entry`) |
| **Phase 2** | **TWAP** — 대액 시장가 **분할** 매수·매도, 슬리피지·체결 부담 완화; V7.1 **분할 익절** 매도에도 동일 임계값 경로 사용 | `execution/order_twap.py`, `execution/scale_out.py`, `run_bot.py`(매수·Scale-Out, 로그 `[Phase2 TWAP …]` 등) |
| **Phase 3** | **AI 휩쏘(False Breakout)** 필터 — 급등·틀린 돌파 의심 구간 완화 | `strategy/ai_filter.py`, `config.json` 의 `ai_false_breakout_*` |
| **Phase 4** | **거시 방어막** — VIX·Fear&Greed 등으로 신규 매수 완화·차단 | `strategy/macro_guard.py`, `api/macro_data.py`, 로그 `[Phase4 거시]` |
| **Phase 5** | **합산 계좌 서킷** — 합산 자산 고점 대비 DD·쿨다운, 필요 시 전량 청산 시도·신규 매수 차단 | `execution/circuit_break.py`, `execution/guard.py` (`peak_equity_total_krw`, `account_circuit_*`), `run_bot` (`circuit_aux_*`, `refresh_circuit_aux_from_brokers`), **`adjust_capital.py`**(수동 입출금 시 고점 보정), 로그 `[Phase5 서킷]` |

추가로 **MDD(종목·계좌 단위)** 등은 `execution/guard.py` 의 `check_mdd_break` 등과 연동되며,
매도 후 **재진입 쿨다운**은 `ticker_cooldowns`(별도 Phase 번호 없음)로 관리합니다.

## 4) 동작 흐름(요약)

1. **GUI로 켤 때:** `run_gui.py`가 `run_bot` 모듈을 import 하는 순간 config 로드·브로커/텔레그램/로거 준비가 끝남.  
   **헤드리스로 켤 때:** `run_bot.py`의 `main()`이 동일하게 초기화.
2. `run_trading_bot()` 주기 실행.
3. `sync_all_positions()`로 실보유와 장부 정합.
4. 리스크 체크(MDD/합산서킷/거시/섹터/AI필터).
5. 매도: **V7.1 분할 익절**(조건 충족 시 최우선) → 타임스탑·하드스탑·샹들리에 전량 등. 이후 신규 매수(시장·시간·슬롯 조건 충족 시).
6. 체결/상태를 `trade_history.json`, `bot_state.json`에 반영.

### 4-1) 미장 유니버스 · 국장/미장 스캐너 · 데이터 소스 · 재진입 쿨다운

- **미장 감시 목록:** `run_bot.get_top_market_cap_tickers` → S&P500 전 종목 시총 정렬 후 **상위 100**,
  Nasdaq100(위키에서 심볼 목록) 시총 정렬 후 **Tier1(S&P100)과 겹치지 않는 종목부터 최대 50** → 합쳐 최대 **150**.
  Nasdaq 위키 URL은 여러 개를 순차 시도(`Nasdaq-100` 본문 등); 실패 시 S&P100만으로도 캐시 가능.
- **스캐너 시계:** `start_scanner_scheduler()` 가 **국장 14:50 KST**(`screener.run_night_screener`),
  **미장 15:20 US/Eastern**(`us_screener.run_us_screener`) 두 잡을 등록 — 각각 매수 창 직전 갱신 목적.
- **OHLCV:** 국내 6자리 종목은 **KIS 일봉 우선**, 실패·부족 시 `yfinance` 보완; 미장은 주로 `yfinance`(재시도·백오프).
- **코인 시장가 매수:** 주문 직전 `get_balance("KRW")` 기준 가용 × **0.999** 캡, 최소 **5,000원** 미만이면 스킵(`run_bot`: `UPBIT_KRW_AVAILABLE_CAP_RATIO`, `UPBIT_COIN_MIN_ORDER_KRW`).
- **매도 후 재진입:** `ticker_cooldowns` 로 매도 사유별 대기(예: 손절/타임스탑 vs 익절); 매수 루프에서 만료 전 재매수 스킵.

### 4-2) 증권사(KIS) 주말 점검 창 · 표시 스냅샷

- **차단 창(KST):** 토요일 **08:00** 이후 ~ 월요일 **07:00** 직전까지 — 국장·미장 **잔고·보유 REST 조회 및 이에 의존하는 매매 루프 일부**를 호출하지 않음(SSL/접속 오류 방지).
- **예외:** **업비트(코인)** 잔고·매매는 **항상** 동작.
- **장부:** `sync_positions` 는 코인 시드만 갱신하고, 국·미는 API 없이 **기존 `positions`·`get_held_*`(장부 기반)** 로 유령 정리 판단.
- **GUI / 텔레그램:** 국·미 숫자는 `bot_state.json` 의 **`last_kis_display_snapshot`**(평일 마지막 성공 조회)으로 채움. 스냅샷은 평일에 `save_last_kis_display_snapshot` 경로로 갱신.
- **보유종목 테이블(국·미):** 점검 구간에는 KIS 잔고 대신 장부 **`positions[*].qty`** 로 수량을 표시(없으면 표시용 `1` 폴백). 평일 한 번이라도 동기화되면 이후 주말에도 맞는 수량이 나옴.
- **주의:** 한 번도 평일에 성공 조회가 없으면 스냅샷이 비어 국·미 표시가 0에 가까울 수 있음.

### 4-3) 수동 입·출금과 합산 고점(`adjust_capital.py`)

- 예수금만 입금/출금하면 **평가금 변동 없이** 총자산이 바뀌어, 기록된 **고점(`peak_equity_total_krw`)** 대비 드로다운이 왜곡될 수 있음.
- **`adjust_capital.py`** 실행 → 실계좌 기준 **`circuit_aux_*` 갱신** 후, 입금/출금 선택 및 원화 금액 입력 → 고점에 동액 반영·`capital_adjustments` 기록.
- 직후 봇이 돌면 `_maybe_run_account_circuit` 이 보정된 고점과 갱신된 합산 스냅샷으로 MDD를 계산.

## 5) 수정 시 권장 원칙

- 분할 익절 임계(수익률·300만·최소 명목 등): `execution/scale_out.py` 상수·함수; TWAP 임계는 `config.json`의 `twap_*` + `run_bot.py`의 `TWAP_ENABLED` 연동.
- 코인 최소수량(먼지·주문 하한): `utils/helpers.py`의 `COIN_MIN_POSITION_QTY`, `run_bot.py`의 `UPBIT_COIN_MIN_ORDER_KRW` 등.
- TWAP 정책 변경: `config.json(twap_*)` 우선, 구조 변경 시 `execution/order_twap.py`·`execution/scale_out.py` 확인.
- 장부 스키마 변경: `execution/guard.py` + `execution/sync_positions.py` + `run_bot.py` 함께 확인
  (`last_kis_display_snapshot`, `positions` 하위 **`qty`**, **`scale_out_done`**, **`circuit_aux_*`** / **`capital_adjustments`** 등).

## 6) 빠른 시작 (Quick Start)

### 6-1. 환경 준비

```bash
py -3.11 -m pip install -r requirements.txt
```

### 6-2. 필수 파일 확인

- `config.json` (API 키/계좌/텔레그램)
- `bot_state.json` (없으면 실행 중 생성/초기화)
- `trade_history.json` (없으면 기록 시 생성)

### 6-3. 실행 방법

- **권장 — GUI (일상 운영)**

```bash
run_bot.bat
```

또는

```bash
py -3.11 run_gui.py
```

- **선택 — 헤드리스** (콘솔만, GUI 없음 · 서버/실험용)

```bash
py -3.11 run_bot.py
```

- 샌드박스 테스트:

```bash
py -3.11 tests/test_lab.py
```

- **수동 입출금 → Phase 5 고점 보정** (브로커 스냅샷 갱신 포함):

```bash
py -3.11 adjust_capital.py
```

### 6-4. 설정 변경 반영

- `config.json`은 프로세스 시작 시 1회 로드.
- 값 변경 후 **반드시 GUI/봇 재시작**.

### 6-5. 매매 사이클 시계 (GUI = 헤드리스와 동일 리듬)

- **GUI(`run_gui.py`) — 매매 엔진:** 기동 직후 `do_trade()` **1회**, 이후 `seconds_until_next_quarter_hour` 로
  다음 KST **`:00` / `:15` / `:30` / `:45`**까지 대기 후 반복.
- **GUI — 텔레그램 생존보고(heartbeat):** 기동 **즉시 1회**, 이후 `seconds_until_next_half_hour` 로
  KST **`:00` / `:30`** 벽시계에 맞춰 전송(매매 주기와 별도).
- **헤드리스(`run_bot.py`) — 선택:** `main()`에서 장부 동기화 후 `run_trading_bot()` **1회** 실행한 뒤,
  `schedule`으로 **매 시 KST `:00` / `:15` / `:30` / `:45`**에 `run_trading_bot`을 등록.

## 7) `config.json` 핵심 키 가이드

### 7-1. 브로커/알림

- `kis_key`, `kis_secret`, `kis_account`, `kis_hts_id`
- `upbit_access`, `upbit_secret`
- `telegram_token`, `telegram_chat_id`

### 7-2. 안전 운용

- `test_mode`
  - `true`: 주문 대신 로그/알림 중심(드라이런 성격).
  - `false`: 실주문.

### 7-3. TWAP (분할 주문)

- `twap_enabled`
- `twap_krw_threshold`
- `twap_usd_threshold`
- `twap_slice_delay_sec`

### 7-4. 시장/리스크

- `buy_window_minutes_before_close`
- `account_circuit_enabled`
- `account_circuit_mdd_pct`
- `account_circuit_cooldown_hours`

### 7-5. 필터

- `ai_false_breakout_enabled`
- `ai_false_breakout_threshold`
- `ai_false_breakout_threshold_coin`
- `ai_false_breakout_provider`
- `macro_guard_enabled` 및 `macro_*` 계열 키

## 8) 트러블슈팅

### 8-1. `config.json` 바꿨는데 적용 안 됨

- 원인: 런타임 중 재로드 안 함.
- 조치: 프로세스 완전 종료 후 재실행.

### 8-2. `NameError: List is not defined`

- 원인: `typing.List` 미import.
- 현재 기준: `run_bot.py`는 `list[str]` 형태로 정리됨.

### 8-3. Windows 콘솔에서 로그 이모지 깨짐/인코딩 에러

- 원인: `cp949` 콘솔 인코딩.
- 현재 기준: `utils/logger.py`에서 안전 출력 처리되어 프로세스 중단은 방지.
- 참고: 글자 깨짐은 콘솔 표시 이슈일 수 있음(파일 로그는 UTF-8).

### 8-4. 코인 먼지 잔고가 장부에 남는 문제

- 기준: `utils/helpers.py`의 `COIN_MIN_POSITION_QTY`.
- 이 값 이하 수량은 실질 보유로 보지 않아 동기화/집계/장부에서 제외.

### 8-5. V7.1 분할 익절(Scale-Out)을 바꿀 때

- 로직·상수: `execution/scale_out.py` (`SCALE_OUT_PROFIT_PCT`, `SCALE_OUT_MIN_NOTIONAL_KRW`, `post_partial_ledger` 등).
- 호출 순서·브로커 주문: `run_bot.py` 국·미·코인 매도 루프(신규 매수 보호 직후, 타임스탑·샹들리에 전).
- TWAP 분할 기준: `config.json`의 `twap_krw_threshold`, `twap_usd_threshold`, `twap_enabled`, `twap_slice_delay_sec`.

## 9) `config.json` 예시 템플릿 (상세)

아래는 바로 복붙해서 시작할 수 있는 템플릿입니다.
민감정보는 실제 값으로 교체하세요.

```json
{
  "kis_key": "YOUR_KIS_APP_KEY",
  "kis_secret": "YOUR_KIS_APP_SECRET",
  "kis_account": "12345678-01",
  "kis_hts_id": "YOUR_HTS_ID",

  "upbit_access": "YOUR_UPBIT_ACCESS_KEY",
  "upbit_secret": "YOUR_UPBIT_SECRET_KEY",

  "telegram_token": "123456789:AA...",
  "telegram_chat_id": "123456789",

  "test_mode": true,

  "twap_enabled": true,
  "twap_krw_threshold": 5000000,
  "twap_usd_threshold": 5000,
  "twap_slice_delay_sec": 90,

  "buy_window_minutes_before_close": 30,

  "account_circuit_enabled": true,
  "account_circuit_mdd_pct": 15.0,
  "account_circuit_cooldown_hours": 24.0,

  "ai_false_breakout_enabled": true,
  "ai_false_breakout_threshold": 70,
  "ai_false_breakout_threshold_coin": 80,
  "ai_false_breakout_provider": "gemini",

  "macro_guard_enabled": true,
  "macro_vix_block_threshold": 25.0,
  "macro_fgi_reduce_threshold": 80,
  "macro_fgi_budget_multiplier": 0.5,
  "macro_vix_fallback": 22.0,
  "macro_fgi_fallback": 60
}
```

### 9-1. 키별 상세 설명

#### 브로커/인증

- `kis_key`, `kis_secret`
  - 한국투자 OpenAPI 앱 키/시크릿.
  - 틀리면 토큰 발급 실패 또는 주문/조회 실패.

- `kis_account`
  - 한국투자 계좌번호. 코드에서 `12345678-01` 형태를 분리해 사용.

- `kis_hts_id`
  - 스크리너(`screener.py`)와 일부 API 호출에서 필요.

- `upbit_access`, `upbit_secret`
  - 업비트 API 키.

#### 알림

- `telegram_token`, `telegram_chat_id`
  - 체결/에러/상태 메시지 알림.
  - 토큰만 맞고 chat_id가 틀리면 전송 실패.

#### 주문 안전 스위치

- `test_mode`
  - `true`: 드라이런 모드. 주문 대신 로그/텔레그램 중심.
  - `false`: 실주문.
  - 운영 전 반드시 의도 확인.

#### TWAP (대액 분할)

- `twap_enabled`
  - 분할 로직 사용 여부.

- `twap_krw_threshold`
  - 원화 주문(국장/코인)에서 이 금액 초과 시 분할.

- `twap_usd_threshold`
  - 달러 주문(미장)에서 이 금액 초과 시 분할.

- `twap_slice_delay_sec`
  - 분할 슬라이스 사이 대기 시간(초).
  - 너무 짧으면 체결가 흔들림, 너무 길면 타이밍 지연.

#### 매수 시간창

- `buy_window_minutes_before_close`
  - 장 마감 N분 전만 매수 허용.
  - 과거 종가 기준 진입 일관성을 위한 시간 제약.

#### 계좌 레벨 리스크

- `account_circuit_enabled`
  - 합산 계좌 서킷 브레이커 사용 여부.

- `account_circuit_mdd_pct`
  - 합산 자산 고점 대비 하락 임계치(%).
  - 초과 시 신규매수 차단 + 정책에 따른 방어 동작.

- `account_circuit_cooldown_hours`
  - 서킷 발동 후 매수 재개까지 대기 시간(시간).

#### AI 필터

- `ai_false_breakout_enabled`
  - 휩쏘 차단 필터 활성화.

- `ai_false_breakout_threshold`
  - 주식 차단 임계(확률 %).

- `ai_false_breakout_threshold_coin`
  - 코인 차단 임계(확률 %). 보통 주식보다 보수적으로 높게 사용.

- `ai_false_breakout_provider`
  - `gemini` / `openai` / `auto` 계열 (현재 코드 기준 사용값).

#### 거시 방어막

- `macro_guard_enabled`
  - VIX/FGI 기반 예산 조절/차단 사용.

- `macro_vix_block_threshold`
  - VIX가 이 값 이상이면 신규매수 차단.

- `macro_fgi_reduce_threshold`
  - FGI가 이 값 이상이면 예산 축소 구간.

- `macro_fgi_budget_multiplier`
  - 축소 구간 예산 배수(예: 0.5 = 절반).

- `macro_vix_fallback`, `macro_fgi_fallback`
  - 외부 데이터 조회 실패 시 대체값.

## 10) 운영 팁 (처음 쓰는 경우 필독)

### 10-1. 안전한 시작 순서

1. `test_mode: true` 로 먼저 실행.
2. GUI/로그에서 신호·포지션·알림 흐름 확인.
3. 소량 예산/높은 임계치로 1~2일 관찰.
4. 이상 없으면 `test_mode: false` 전환 후 재시작.

### 10-2. 자주 하는 실수

- 설정 바꾸고 재시작 안 함 → 값이 반영 안 됨.
- `kis_hts_id` 누락 → 스크리너 실패.
- 텔레그램 chat_id 오타 → 알림 무반응.
- `test_mode` 확인 없이 실주문 전환.

### 10-3. 상태 파일 이해

- `bot_state.json`
  - 실시간 운용 상태(positions·**qty**·**`scale_out_done`**, cooldown, ticker_cooldowns, 일부 누적 통계,
    **last_kis_display_snapshot**, Phase5용 **`peak_equity_total_krw`** / **`circuit_aux_last_*`** / **`capital_adjustments`** 등).
  - 깨지면 `execution/guard.py`에서 기본 구조로 복구 시도.

- `trade_history.json`
  - append 기록 로그 성격. 전략 회고/검증에 활용.

### 10-4. 슬롯·먼지·분할 익절 요약

- **포지션 슬롯:** `execution/guard.py`의 `can_open_new` — 시장별(국·미·코인) `max_positions`만 적용, **코어 예외 없음**.
- **분할 익절:** `positions[*].scale_out_done`, 조건·수량·TWAP 연동은 본문 **「3) 현재 핵심 공통 상수/정책」** 및 `execution/scale_out.py` 참고.
- 코인 최소수량: `utils/helpers.py`의 `COIN_MIN_POSITION_QTY`.
  - 이 값 이하는 장부/집계에서 사실상 제외.

## 11) 변경 후 점검 체크리스트

- [ ] `config.json` 문법(JSON) 정상인지 확인(쉼표/따옴표).
- [ ] 봇/GUI 완전 종료 후 재기동.
- [ ] 시작 로그에 에러 없는지 확인.
- [ ] 텔레그램 테스트 메시지 수신 확인.
- [ ] `test_mode` 의도와 실제 운용 모드 일치 확인.
- [ ] 장부(`bot_state.json`)와 실제 잔고가 동기화되는지 확인.

