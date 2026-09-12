# 변경 내역 (CHANGELOG)

**시작 순서:** [AGENTS.md](../AGENTS.md) → [CURRENT.md](CURRENT.md) → **이 파일** → [docs/README.md](README.md).

형식: 최신이 위. 규칙: .cursor/rules/change-log-docs.mdc, gent-handoff.mdc.
**상태:** Aug 작업 상당수 미커밋일 수 있음 (git status).

---

## 2026-09-11 — KR 마감 후 예수 이중합산 재발 방지

- **무엇을:** sanitize **Rule1b**(예수=`현금+보유`/`직전총평`·보유 정체), 스냅샷 **상방 +5% 거부**, 비장중 snap freeze, `market_equity_for_risk` last_loop 대비 이중 점프 방어. 상태 복구: cash 436,653·total/aux/peak→~682,903 (`scripts/repair_kr_snapshot_double_count.py`).
- **왜:** 15:30 후 예수 436k→682k·총평 928k로 Phase5 합계 +24.5만 (실입금 아님).
- **주요 파일:** `services/ledger_valuation.py`, `run_bot.py`, `scripts/repair_kr_snapshot_double_count.py`
- **이어서 할 일 / 주의:** 봇 재시작. GUI 상단 예수·총평 확인.
- **테스트:** `pytest tests/test_ledger_label_coalesce.py -k "rule1b or unexplained or double_count" -q`

---

## 2026-09-10 — 스윙 RSI/HALF 사유: 코인 소수 가격 표기

- **무엇을:** `check_swing_exit` RSI FULL·HALF 사유에서 `:,.0f` 대신 `_format_swing_price_label` (SAHARA 등 저가 코인이 `현재가: 0`으로 찍히던 표시 버그).
- **왜:** 로그 점검 중 발견. 매도선 로직과 무관.
- **주요 파일:** `strategy/rules.py`
- **이어서 할 일 / 주의:** SAHARA RSI FULL은 매도선 이탈이 아님(+1~10% RSI 전용). V8 PROM 매도선 -4% 고정은 ATR×2.5 샹들리에가 절대하한 아래인 설계 결과.
- **테스트:** (표기만)

---

## 2026-09-09 — Phase5 COIN: 바이낸스=USDT / 업비트=원

- **무엇을:** COIN 시장별 잔고 MDD를 GUI와 같은 견적 통화로. `circuit_aux_last_coin_native` + 합산용 `circuit_aux_last_coin_krw` 이중 저장. `peak_equity_COIN_unit`으로 구 원화 고점→USDT 전환 시 **현재값 리셋**(오발동 방지).
- **왜:** 바이낸스 Phase5가 USDT×환율(원)이라 환율만으로 DD가 흔들림.
- **주요 파일:** `api/coin_broker.py`, `execution/phase5_ops.py`, `execution/guard.py`, `run_bot.py`, `execution/market_cycles/coin_cycle.py`, `adjust_capital.py`
- **이어서 할 일 / 주의:** 봇 재시작. 첫 COIN Phase5 루프에서 마이그레이션 로그(`고점 단위 마이그레이션`) 확인. 필요 시 `scripts/reset_phase5_market_circuit.py --market COIN`.
- **테스트:** `pytest tests/test_phase5_coin_native_unit.py tests/test_phase5_no_holdings_skip.py tests/test_phase5_market_mdd.py -q`

---

## 2026-09-08 — Phase5 재검증: 다음 주기 2회차 (8초 sleep 폐지)

- **무엇을:** 1회 KIS 재조회 후에도 MDD면 `phase5_reconfirm_pending` 저장 후 **이번 루프 청산 보류**. **다음 봇 주기**에 다시 KIS 재조회 → 여전히 MDD면 AI. 루프 내 8초 sleep 제거.
- **왜:** 8초는 정산·스냅샷 반영에 짧음. 다음 주기까지 두는 편이 안전.
- **설정:** `phase5_reconfirm_next_cycle`(기본 true). `phase5_reconfirm_delay_sec`는 legacy(미사용).
- **주요 파일:** `execution/phase5_ops.py`, `run_bot.py`
- **테스트:** `pytest tests/test_equity_circuit_integration.py tests/test_phase5_ai_liquidation.py -q` (16 passed)

---

## 2026-09-08 — Phase5 재검증: 텀 후 2회 KIS 조회 + AI 전후 숫자

- **무엇을:** (이후 항목으로 대체) 당시 설계는 8초 sleep 후 2회차. 지금은 **다음 주기** 방식.
- **테스트:** —

---


## 2026-09-08 — Phase5 청산 설계 고정: 잔고MDD+AI / 지수=매수패스

- **무엇을:** KR·US·COIN **동일** — 청산은 peak_equity_*만. 지수 MDD 청산 경로 폐지. AI에 risk/snap 괴리·정산 의심·지수 참고 컨텍스트. 설계 문서 docs/PHASE5_LIQUIDATION_PLAN.md.
- **왜:** 지수 고점 -15%로 국장 오청산. 지수는 원래 매수 중단용. 잔고 스냅샷 오류 vs 실폭락은 AI가 가려야 함.
- **주요 파일:** execution/phase5_ops.py, execution/phase5_ai_liquidation.py, docs/PHASE5_LIQUIDATION_PLAN.md
- **테스트:** pytest tests/test_phase5_ai_liquidation.py tests/test_equity_circuit_integration.py -q

---


## 2026-09-08 — Phase5 기본을 잔고 고점 MDD로 복구

- **무엇을:** Phase5 기본 판정을 **지수(KODEX/SPY) 고점**이 아니라 **시장별 잔고 `peak_equity_*` 고점 대비 15%** 로 복구. 지수 모드는 `account_circuit_use_index: true` 일 때만. 청산 직전 KIS 재검증·매수 직후 유예 유지.
- **왜:** 국장 KODEX200 6개월 고점 -24%로 강제 청산 오발동. 사용 의도는 **내 잔고 고점** MDD.
- **주요 파일:** `execution/phase5_ops.py`, `run_bot.py`, `execution/phase5_ai_liquidation.py`
- **이어서 할 일 / 주의:** 봇 재시작. `scripts/reset_phase5_market_circuit.py --market KR` 로 쿨다운·고점 정리.
- **테스트:** `pytest tests/test_equity_circuit_integration.py tests/test_phase5_no_holdings_skip.py tests/test_phase5_market_mdd.py -q`

---


﻿## 2026-09-01 — 시장 벤치마크 통일 (매수·Phase5)

- **무엇을:** `strategy/market_benchmark.py` 단일 소스 — KR `069500.KS`, US `SPY`, COIN 거래소 BTC. 매수(날씨·RS·급락)·Phase5 MDD가 **동일 티커** 사용. `^KS11`/`^GSPC`/네이버 KOSPI·`BTC-USD` 분산 제거.
- **왜:** 매수·청산 지수가 달라 판단 불일치·코드 중복.
- **주요 파일:** `strategy/market_benchmark.py`, `execution/phase5_index_circuit.py`, `run_bot.py`
- **테스트:** `pytest tests/test_market_benchmark.py tests/test_phase5_index_circuit.py -q`

---


## 2026-08-31 — Gemini 모델 갱신 · 429 한도 즉시 폴백

- **무엇을:** 폐기 모델(1.5·2.0-flash) 제거 → `gemini-flash-lite-latest`, `3.5/3.1-flash-lite` 등. **429 spend cap** 시 모델 재시도 중단 → OpenAI. `scripts/probe_gemini_models.py`.
- **왜:** 프로브 결과 2.5/3.x는 **429(월 한도)**, 1.5/2.0은 **404(폐기)** — 코드만 고쳐서는 한도 해제 전까지 Gemini 불가.
- **사용자:** [AI Studio spend cap](https://aistudio.google.com/) 상향 또는 `ai_gemini_model` 지정.
- **테스트:** `pytest tests/test_gemini_models.py -q`

---

## 2026-08-31 — Phase5 AI 매수 AI와 동일 LLM 경로 + 라이브 검증

- **무엇을:** `evaluate_llm_json_prompt` — Gemini→OpenAI 폴백 **매수 AI와 동일**. Phase5 전용 HTTP 중복 제거. `scripts/smoke_phase5_ai.py`, `tests/test_phase5_ai_live.py` 라이브 스모크.
- **왜:** bot.log Gemini 404만 찍힘 — 매수는 OpenAI 폴백으로 동작, Phase5는 별도 코드라 실패. 라이브 확인 전 배포 실수.
- **라이브 결과:** 매수 AI `engine=openai` OK · Phase5 AI `score=75 proceed=True` OK (Gemini 404 → OpenAI 폴백)
- **주요 파일:** `strategy/ai_filter.py`, `execution/phase5_ai_liquidation.py`, `scripts/smoke_phase5_ai.py`
- **테스트:** `pytest tests/test_phase5_ai_liquidation.py tests/test_phase5_ai_live.py -q -m live` · `python scripts/smoke_phase5_ai.py`

---

## 2026-08-31 — Phase5 보유 0 스킵 · KR 벤치 교체 · AI OpenAI 폴백

- **무엇을:** 장부 **보유 qty=0**이면 지수·AI·청산 전부 스킵(«청산 유지» 로그 제거). KR 벤치 `^KS11`→`069500.KS`(yfinance 오염). Gemini 실패 시 **OpenAI 폴백**. 지수 고점 99%分位·스파이크 필터.
- **왜:** bot.log — 보유 0인데 KOSPI ^KS11 9115 스파이크로 Phase5·AI 스팸. Gemini 전 모델 404.
- **주요 파일:** `execution/phase5_ops.py`, `execution/phase5_index_circuit.py`, `execution/phase5_ai_liquidation.py`, `tests/test_phase5_no_holdings_skip.py`
- **테스트:** `pytest tests/test_phase5_no_holdings_skip.py tests/test_phase5_ai_liquidation.py tests/test_phase5_index_circuit.py -q`

---

## 2026-08-28 — Phase5 AI 청산 심사 (종합 점수)

- **무엇을:** 지수 2차 통과 후 **AI 종합 판단** — 보유·현재가·지수·계좌(참고) → `liquidation_score` 0~100. **≥70**(기본)이면 청산. LLM 무응답 시 최대 3회 재시도, 실패 시 보류.
- **왜:** 지수만으로도 Black Swan은 막지만, 보유·맥락까지 보는 2차 안전장치 요청.
- **주요 파일:** `execution/phase5_ai_liquidation.py`, `execution/phase5_ops.py`, `run_bot.py`
- **설정:** `phase5_ai_liquidation_enabled`, `phase5_ai_liquidation_threshold`(70), `phase5_ai_liquidation_provider`, `phase5_ai_liquidation_max_retries`(3)
- **테스트:** `pytest tests/test_phase5_ai_liquidation.py -q`

---

## 2026-08-28 — Phase5 지수 MDD 전환 (계좌 총평 폐기)

- **무엇을:** 기본 Phase5를 **벤치마크 지수** 고점 대비 15% 하락으로 발동 — US=SPY, KR=^KS11, COIN=BTC-USD (6mo 고점). 계좌 `peak_equity_*`·KIS 재검증·타당성 게이트는 Phase5에서 **미사용**.
- **왜:** 매도·스냅샷 오류로 계좌 MDD 오발동 반복. 지수만 보면 «진짜 폭락»일 때만 청산.
- **주요 파일:** `execution/phase5_index_circuit.py`, `execution/phase5_ops.py`, `scripts/reset_phase5_market_circuit.py`
- **이어서 할 일 / 주의:** yfinance 실패 시 해당 시장 스킵. `account_circuit_mdd_pct`(기본 15)는 지수 DD 임계.
- **테스트:** `pytest tests/test_phase5_index_circuit.py tests/test_equity_circuit_integration.py -q`

---

## 2026-08-28 — Phase5 청산 타당성 게이트 ~~(계좌 모드, 폐기)~~

- **상태:** 지수 MDD 전환으로 대체.

---

## 2026-08-28 — risk 총평 snap_total 우선 (매도 정산 지연, 쿨다운 없음)

- **무엇을:** `market_equity_for_risk` — `cash+stock < snap_total×0.95`이면 `snap_total` 반환(보유·무보유 공통). `post_sell_grace` 60분 유예는 **제거**(시간 지연 대신 스냅 신뢰).
- **왜:** 장부만 갱신되고 예수 미반영 시 Phase5 오발동. 수식·스냅 우선순위로 근본 처리.
- **주요 파일:** `services/ledger_valuation.py`, `execution/phase5_ops.py`, `execution/ledger_apply.py`, `execution/guard.py`, `tests/test_ledger_label_coalesce.py`
- **이어서 할 일 / 주의:** snap_total도 같이 깎이면(risk=snap 동시 하락) 이 방어로는 부족 — KIS 강제 새로고침·재검증.
- **테스트:** `pytest tests/test_ledger_label_coalesce.py tests/test_equity_circuit_integration.py tests/test_phase5_market_mdd.py -q`

---

## 2026-08-28 — Phase5 매도 직후 60분 유예(post_sell_grace) ~~(폐기)~~

- **상태:** 시간 유예 방식 폐기 → 위 snap_total 우선 로직으로 대체.

---

## 2026-08-24 — KR 총평: D+2·NAV 반영 (강제새로고침 cash+보유만 쓰던 버그)

- **무엇을:** `parse_kr_cash_total` — 보유≈0이면 `total=max(nass, tot_evlu, D+2, …)` **및 전액현금 시 표시 예수=총평=NAV**. GUI `account_snapshot`은 파서 NAV를 버리고 `dnca+보유`만 쓰던 경로를 `max(cash+hold, nav)`로 수정. 강제 새로고침 시 `[KR raw]` 필드 로그.
- **왜:** 금현물 매도 후 MTS 총자산≈667k인데 봇이 dnca 35만만 총평/예수로 쓰거나, 예수≠총평로 보여 혼란·Phase5 오발동.
- **주요 파일:** `api/kis_parsers.py`, `services/account_snapshot.py`, `tests/test_kis_parsers.py`
- **이어서 할 일 / 주의:** GUI **KIS 강제 새로고침** 후 예수=총평≈66.7만 확인. 고점이 35만이면 `reset_phase5_market_circuit.py --market KR`.
- **테스트:** `pytest tests/test_kis_parsers.py -q`

---

## 2026-08-24 — Phase5 청산 전 KIS 강제 재검증(최후 진술)

- **무엇을:** `_reconfirm_phase5_triggers_before_liquidation` — KR/US 1차 발동 시 **괴리 여부와 무관**하게 캐시 무시 KIS 재조회 → `market_equity_for_risk` 2차 판정. 재조회 실패 시 청산 보류. 재검증용 `peaks` dict 키 덮어쓰기 버그도 수정.
- **왜:** 매도대금 미정산·API 랙으로 risk만 급감한 오발동을 청산 직전에 한 번 더 걸러냄.
- **주요 파일:** `execution/phase5_ops.py`, `services/ledger_valuation.py`, `tests/test_equity_circuit_integration.py`
- **테스트:** `pytest tests/test_equity_circuit_integration.py -q`

---

## 2026-08-24 — risk 총평 Orphaned Cash 방어 (매도 정산 지연)

- **무엇을:** `market_equity_for_risk`에서 `cash+stock < snap_total×0.95`이면 `snap_total` 반환. 장부만 비고 예수 미반영 시 Phase5 오발동 차단.
- **왜:** KR 금현물 등 전량 매도 후 스냅 예수 지연 → risk 급감 → 15% 서킷 오발동.
- **주요 파일:** `services/ledger_valuation.py`, `tests/test_ledger_label_coalesce.py`, `docs/EQUITY_SNAPSHOT_CIRCUIT_PLAN.md`
- **이어서 할 일 / 주의:** 스냅 `total` 자체도 stale이면 한계 있음. 실로그에 `매도 정산 지연 방어` 확인.
- **테스트:** `pytest tests/test_ledger_label_coalesce.py::test_market_equity_for_risk_orphaned_cash_after_kr_sell tests/test_equity_circuit_integration.py -q`

---

## 2026-08-23 — 세션 경계 백업(국·미 시가/종가) + 기존 일당 1개 정리

- **무엇을:** 아카이브를 `kr_open`/`kr_close`/`us_open`/`us_close` 전이 시에만 저장. `save_state`·`record_trade`의 일자 아카이브 제거(`.bak`는 유지). 기존 `backups/`는 종류×일자당 최신 1개만 남김.
- **왜:** 사이클마다 쌓이던 덤프를 줄이고, 복구 시점을 장 개장·마감에 맞춤.
- **주요 파일:** `execution/state_backup.py`, `run_bot.py`, `execution/guard.py`, `utils/helpers.py`, `scripts/organize_state_backups.py`, `tests/test_state_backup.py`
- **이어서 할 일 / 주의:** 15분 틱 근사라 시각은 시가/종가 ±15분. 봇이 꺼져 있으면 해당 경계 스냅은 다음 기동 edge/캐치업으로만 보완.
- **테스트:** `pytest tests/test_state_backup.py -q`

---

## 2026-08-23 — 레거시 bot_state_backups 연/월 정리

- **무엇을:** 평면 `bot_state_backups/`(~2852개)를 `backups/2026/06`(1083)·`07`(1769)로 이동 후 빈 폴더 삭제. 정리 스크립트에 레거시 폴더 마이그레이션 포함. `.gitignore`에 `bot_state_backups/` 추가.
- **왜:** 예전 사이클마다 쌓인 스냅샷을 새 아카이브 구조와 통일.
- **주요 파일:** `scripts/organize_state_backups.py`, `.gitignore`
- **이어서 할 일 / 주의:** 이후 세션 경계 백업으로 전환됨. 과거 분 단위는 일당 1개로 축소 완료.
- **테스트:** 이동 검증(월별 파일 수) · 코드상 `bot_state_backups` 쓰기 경로 없음

---

## 2026-08-23 — 장부·매매내역 연/월 백업 재개

- **무엇을:** `backups/YYYY/MM/` 아카이브 추가(`execution/state_backup.py`). `save_state`·`record_trade` 시 당일 1회 복사. 기존 circuit_reset 스냅샷 이동·잡파일 삭제. sidecar `bot_state.bak` 유지.
- **왜:** 그동안은 `.bak` 덮어쓰기만 있어 날짜별 이력이 없었음.
- **주요 파일:** `execution/state_backup.py`, `execution/guard.py`, `utils/helpers.py`, `scripts/organize_state_backups.py`, `.gitignore`
- **테스트:** `pytest tests/test_state_backup.py -q`

---


## 2026-08-23 — 계좌 MDD -5% 매수차단 폐지, Phase5 15%만

- **무엇을:** `check_mdd_break`를 항상 True 스텁으로 두고, KR/US/COIN cycle의 -5% MDD 매수 게이트를 제거. 계좌 보호는 Phase5(`account_circuit_mdd_pct` 기본 15%)만.
- **왜:** 5% MDD가 라벨 보정·스냅샷 노이즈에 민감하고 Phase5와 역할이 중복됨.
- **주요 파일:** `execution/guard.py`, `execution/market_cycles/{kr,us,coin}_cycle.py`, `execution/circuit_break.py`, `tests/test_phase5_market_mdd.py`, `tests/test_market_cycles_smoke.py`
- **이어서 할 일 / 주의:** `peak_equity_*`는 Phase5 고점 추적용으로 유지. `check_mdd_break` 이름은 import 호환용.
- **테스트:** `pytest tests/test_phase5_market_mdd.py tests/test_market_cycles_smoke.py -q`

---

## 2026-08-22 — AI 핸드오프 문서 체계 (AGENTS·CURRENT·docs 목차)

- **무엇을:** `AGENTS.md`, `docs/CURRENT.md`, `docs/README.md`(목차), `.cursor/rules/agent-handoff.mdc` 추가. `change-log-docs.mdc`를 CURRENT+GUI 정합까지 포함하도록 강화.
- **왜:** 다른 PC/AI에서 수정 내역을 빠르게 보고 이어서 작업하기 쉽게.
- **주요 파일:** `AGENTS.md`, `docs/CURRENT.md`, `docs/README.md`, `.cursor/rules/agent-handoff.mdc`, `.cursor/rules/change-log-docs.mdc`
- **이어서 할 일:** 새 세션은 `AGENTS.md` → `CURRENT.md` → `CHANGELOG.md` 순으로 시작.

---


## 2026-08-22 — GUI·README V8 72h·헷지 정책 문서 정합

- **무엇을:** GUI 매매·전략 안내의 Phase4/BEAR 헷지 예외 문구를 현금 관망으로 수정. README 상수표·타임스탑 절 336h→72h 잔여분 정리.
- **왜:** 코드는 반영됐으나 GUI/README 일부가 구정책으로 남아 있었음.
- **주요 파일:** `run_gui.py`, `README.md`

---


## 2026-08-21 — BEAR/Phase4 헷지 매수 예외 제거 (현금 관망)

- **무엇을:** Phase4·BEAR에서 헷지 포함 **신규 매수 전면 차단**. HEDGE_PHASE4 분기·MAX_POSITIONS/AI/지수급락/갭 예외 제거. 정상 장에서는 merge_hedge로 헷지를 후보에 넣어 일반 V8/SWING 검토.
- **왜:** 방어막 발동 시 현금 관망이 우선; 평시에는 헷지도 시그널 기반으로만 진입.
- **주요 파일:** run_bot.py, execution/market_cycles/{kr,us,coin}_buy_cycle.py, strategy/hedge_universe.py, docs/HEDGE_UNIVERSE.md, README.md, tests/test_hedge_no_bear_phase4_bypass.py
- **테스트:** pytest tests/test_hedge_no_bear_phase4_bypass.py -q

---

## 2026-08-21 — V8 타임스탑 336h→72h (3일)

- **무엇을:** V8_TIME_STOP_HOURS(=EQUITY/COIN 별칭) 336→**72**. 돌파 실패 조기 청산. 테스트 assert·발동 시점(71h 미발동 / 72h+ 발동) 갱신.
- **왜:** 장기 방치보다 조기 실패 청산으로 자금 회전.
- **주요 파일:** strategy/rules.py, tests/test_v8_time_stop.py, README.md
- **테스트:** pytest tests/test_v8_time_stop.py -q

---

## 2026-08-21 — V8·스윙 절대 손실 -4% 하드캡

- **무엇을:** `get_final_exit_price`·`get_swing_hard_stop_floor` 반환 직전, 평단(`buy_p`)×`ABSOLUTE_STOP_MAX_LOSS_MULT`(0.96)보다 깊으면 그 값으로 끌어올림. `buy_p` 없거나 ≤0이면 미적용.
- **왜:** ATR·피보 등 기술 매도선이 과도하게 깊어도 계좌 최대 손실을 -4%로 제한.
- **주요 파일:** `strategy/rules.py`, `tests/test_rules_v8_cap.py`
- **테스트:** `pytest tests/test_rules_v8_cap.py -q`

---


## 2026-08-21 — 스윙 60MA 이격 상한 축소

- **무엇을:** `SWING_MA60_MAX_EXTENSION_PCT_US` 15→8, `KR` 20→10, `COIN` 30→15. 로직 변경 없음.
- **왜:** 더 깊고 확실한 눌림목에서만 스윙 진입.
- **주요 파일:** `strategy/rules.py`, `tests/test_swing_ma60_extension.py`, `README.md`
- **테스트:** `pytest tests/test_swing_ma60_extension.py -q`

---

## 2026-08-21 — CHANGELOG 백필 (이 문서 확장)

- **무엇을:** Aug 13~21 대화·문서·커밋에서 확인된 변경을 날짜별로 전부 기록. 커밋된 5~7월 요약 섹션 추가.
- **왜:** 다른 PC에서 이어서 작업할 때 맥락 누락 방지.
- **주요 파일:** `docs/CHANGELOG.md`
- **이어서 할 일:** 새 작업은 이 파일 **맨 위**에만 추가. 백필은 날짜 블록 안을 보강.

---

## 2026-08-21 — 변경 내역 문서화 지침 도입

- **무엇을:** `docs/CHANGELOG.md` 신설 + Cursor 규칙 `change-log-docs.mdc` (alwaysApply). README 문서 목차에 링크.
- **왜:** PC/세션이 바뀌어도 수정 내역만 보고 이어서 작업.
- **주요 파일:** `docs/CHANGELOG.md`, `.cursor/rules/change-log-docs.mdc`, `README.md`

---

## 2026-08-21 — V8 거래량 폭발(Volume Surge) 필터

- **무엇을:** `calculate_pro_signals`(TREND_V8) 진입 **0순위**: 당일 `v` ≥ 직전 20봉 평균 × 2.0. 불가/미달 시 차단 + 로그 `거래량 부족 (돌파 모멘텀 미달, 당일 거래량 < 20일 평균의 2배)`.
- **왜:** 가짜 돌파(휩쏘) 완화. 스윙(`check_swing_entry`) 미적용.
- **주요 파일:** `strategy/rules.py` (`V8_VOLUME_SURGE_*`), `tests/test_v8_volume_surge.py`
- **이어서 할 일:** 실매매 전 봇 재시작. 소프트 1× `is_volume_surged`(MACD/RSI)는 별개로 유지.
- **테스트:** `pytest tests/test_v8_volume_surge.py -q` (3 passed)

---

## 2026-08-21 — 스윙 러너 트레일 5MA → 10MA

- **무엇을:** `SWING_RUNNER_TRAIL_MA_DAYS=10`. 로그/사유 문자열 10MA. `get_swing_ma10_*` / 장부 키 `swing_ma10_trail_high` (레거시 `swing_ma5_trail_high` 호환 max).
- **왜:** 러너 조기청산 완화 → 손익비 개선 (`analysis`에서 스윙 조기청산 지적).
- **주요 파일:** `strategy/rules.py`, `tests/test_swing_runner_trailing.py` 등
- **이어서 할 일:** V8 `ma5`/거래량5 로직과 혼동 금지.
- **테스트:** 관련 스윙 러너 테스트 통과 확인됨.

---

## 2026-08-21 — VKOSPI 동적 스파이크 차단 (고정 25 폐기)

- **무엇을:** 고정 ≥25 차단 제거. 시리즈(`^KSVK` yfinance → KRX 폴백)로 20MA 계산 후 **현재 > 20MA×1.3 且 현재 ≥ 20** 일 때만 KR 매수 차단. 로그: `VKOSPI 단기 급등 감지 (현재/20MA)`.
- **왜:** 절대값 25는 국면과 무관하게 과다 차단/오탐. 날씨(`_v8_trend_buy_allowed_in_weather`)는 불변.
- **주요 파일:** `api/macro_data.py` (`VKOSPI_SPIKE_*`), `run_bot.py` (`_apply_vkospi_kr_buy_block` — **국장 매수 루프에서만**), `tests/test_vkospi_kr_block.py`
- **이어서 할 일:** `_build_market_context`에 넣지 말 것(매 루프 스팸·비매수 구간 차단 이슈 이력).
- **테스트:** `pytest tests/test_vkospi_kr_block.py` (13 passed 이력)

---

## 2026-08-21 — Phase5/MDD 장중 게이트 · US 오탐 추가 방어

- **무엇을:** KR/US Phase5 평가를 **해당 시장 정규장 오픈 시** 위주로 (`_equity_market_session_open`). 비장중 stale 현금이 peak/risk를 깎아 오발동하는 경로 완화. cash≈total 스냅 오인 보강. 고점/`last_loop` 리셋 운영.
- **왜:** 8/20 패치 후에도 장외·스냅샷 stale로 US 서킷 스팸. 요청: MDD는 장중에만.
- **주요 파일:** `execution/phase5_ops.py`, `services/ledger_valuation.py`, `run_bot.py`, `scripts/reset_phase5_market_circuit.py`
- **이어서 할 일:** 실로그에서 risk vs snap divergence·장외 스킵 확인. 피크 비정상이면 리셋 스크립트.
- **문서:** `docs/EQUITY_SNAPSHOT_CIRCUIT_PLAN.md`, `docs/PHASE5_ACCOUNT_CIRCUIT.md`

---

## 2026-08-20 — 거래 복기·전략 리뷰 (`analysis/`)

- **무엇을:** 라이브 봇과 분리된 `analysis/` — `trade_history`→라운드트립, 매도 후 N일 수익, era(`strategy_eras.json`) 리포트. `STRATEGY_REVIEW.md` 작성.
- **왜:** 전략 타당성·개선점 데이터 판단.
- **주요 파일:** `analysis/run_analysis.py`, `analysis/trade_review/*`, `analysis/STRATEGY_REVIEW.md`, `analysis/output/*`, `analysis/cache/*`
- **핵심 결론 (당시):** 승률 ~38%, 기대값 ≈0. V8 약함. 코인 V6 상대 양호. 스윙 조기청산 흔함. → 이후 V8 Volume Surge·스윙 10MA로 일부 반영.
- **이어서 할 일:** 월 1회 `py -3.11 analysis/run_analysis.py --refresh`. BEAR era·US 승률·Phase3 AI 로그는 미해결.

---

## 2026-08-20 — 잔고 스냅샷·서킷 오발동 근본 해결 (Phase 0~3)

- **무엇을 (계획서 전부 [x]):**
  - **원인:** sanitize Case B3 예수 이중차감 → 총평 급감 → Phase5 15%·5% MDD 오발동 (KR 8/14, US TLT 8/20).
  - **0:** B3 조건 수정, force여도 급감 거부, `market_equity_for_risk()`, Phase5 risk 경로, 매수 후 60분 유예, US 쿨다운 리셋 스크립트·테스트.
  - **1:** MDD/사이클 → risk equity; sanitize는 GUI·persist; 문서 risk vs display.
  - **2:** sanitize Rule1/2/3 단순화, `_buy_cash_guard` 흡수.
  - **3:** divergence 로그, 발동 직전 KIS 재확인, 통합 fixture.
- **왜:** GUI 스냅샷과 리스크 입력 혼재 + on_trade API 생략이 오발동을 키움.
- **주요 파일:** `services/ledger_valuation.py`, `execution/phase5_ops.py`, `execution/guard.py`, `execution/circuit_break.py`, `run_bot.py`, `docs/EQUITY_SNAPSHOT_CIRCUIT_PLAN.md`, `docs/PHASE5_ACCOUNT_CIRCUIT.md`, `docs/KIS_GUI_DISPLAY.md`, `tests/test_equity_circuit_integration.py`, `tests/test_us_equity_after_sell.py` 등
- **이어서 할 일:** 8/21 장중 게이트와 함께 실운영 관찰. **표시 스냅샷을 서킷에 직접 넣지 말 것.**

---

## 2026-08-14 — 스윙 전고점 이격(Ceiling) 필터

- **무엇을:** `check_swing_entry` — 최근 60봉 최고가 대비 현재가가 5% 이내이면 스윙 매수 강제 패스. 로그 `전고점 바짝 근접 (상투 잡기 방지, …)`. **V8에는 미적용.**
- **왜:** 전고점 직전 얕은 조정을 눌림목으로 착각하는 상투 매수 방지.
- **주요 파일:** `strategy/rules.py` (`SWING_CEILING_*`, `_swing_ceiling_block`), `tests/test_swing_ceiling_filter.py`

---

## 2026-08-14 — 국장 MDD/서킷 상태 점검·해제 관련

- **무엇을:** 로그상 MDD/서킷이 다시 보인 건 조사. Phase5 쿨다운·peak·스냅샷 오염과 5% MDD 브레이크 구분·상태 정리.
- **왜:** 8/13 해제 후에도 “MDD 걸림” 로그 혼동.
- **주요 파일:** `scripts/reset_phase5_market_circuit.py`, `execution/guard.py`
- **이어서 할 일:** 쿨다운 vs 5% MDD vs Phase5 15%를 로그에서 구분 (`docs/PHASE5_ACCOUNT_CIRCUIT.md` §4).

---

## 2026-08-13 — Phase5 기본을 시장별 평가 MDD로 전환

- **무엇을:** 합산 비중 서킷 기본 OFF. 기본 `per_market_mdd` — `peak_equity_KR/US/COIN` 대비 MDD로 **해당 시장만** 청산·쿨다운. 입출금(`adjust_capital`·GUI) 시장 선택. 총평 급감 가드. 모드 전환 시 쿨다운 정리.
- **왜:** 국장 총평 흔들림(입출금·API)이 합산 비중을 깨 KR만 잘리는 오발동.
- **주요 파일:** `execution/circuit_break.py`, `execution/guard.py`, `execution/phase5_ops.py`, `adjust_capital.py`, `run_gui.py`, `run_bot.py`, `docs/PHASE5_ACCOUNT_CIRCUIT.md`, `tests/test_phase5_market_mdd.py`, `tests/test_phase5_share_circuit.py`
- **이어서 할 일:** `account_circuit_use_share` / `use_total` 은 옵션. 입출금은 반드시 시장 지정.

---

## 2026-08-13 — KIS 잔고 파서: dnca/nass 표시 (D+2 오인 수정)

- **무엇을:** 표시 예수=dnca_tot_amt, 표시 총평=
ass_amt. 주문가능 D+2(prvs_rcdl_excc_amt)·	ot_evlu_amt(유가+D+2만)를 표시/서킷에 쓰지 않음. 매수 예산은 D+2 우선, 0이면 표시 예수 폴백. 앱 설정(FUND_STTL_ICLD_YN 등)으로는 해결 불가 — 필드 선택 문제.
- **왜:** 매수 직후 D+2=0이면 예수 0·총평=보유만으로 보여 Phase5가 국장 붕괴로 오인.
- **주요 파일:** pi/kis_parsers.py (parse_kr_cash_total), pi/kis_api.py, 	ests/test_kis_parsers.py
- **이어서 할 일:** 봇 재시작 후 잔고 조회부터 적용. 이후 8/20 sanitize·risk 분리와 함께 볼 것.

---

## 2026-08-13 — 매수 직후 예수/총평 이상 · sanitize 초기 대응

- **무엇을:** 매수 후 예수 0·총평 반토막 현상 분석 → sanitize/스냅샷 경로 수정 착수 (이후 8/20 계획서로 체계화).
- **왜:** 서킷 오발동의 직접 원인(이중차감·표시=리스크 혼재).
- **주요 파일:** `services/ledger_valuation.py`, `run_bot.py`, `tests/test_kis_*`, `tests/test_ledger_label_coalesce.py`

---

## 2026-08-13 — VKOSPI KR 오토 블락 도입 → 매수 구간만 · 값 보정

- **무엇을 (초기):** VKOSPI ≥25 시 KR 매수 차단. 조회 실패 시 조용히 패스. 다중 소스.
- **수정:** (1) **매수 루프에서만** 적용 — `_build_market_context` 매 루프 스팸/오해 해소. (2) 잘못된 지수(~96) 오인 수정 → 정상 범위 `VKOSPI_MIN/MAX`. (3) 이후 8/21 동적 스파이크로 교체.
- **왜:** 잡주 무빙 극단 구간 KR 신규 매수 방어. US/COIN 미적용.
- **주요 파일:** `api/macro_data.py`, `run_bot.py`, `execution/market_cycles/kr_buy_cycle.py`

---

## 커밋된 이력 요약 (원격에 있는 것, 참고)

| 날짜 | 커밋 요약 |
|------|-----------|
| 2026-07-19 | 매수 직후 예수 재오염·Instant Out 방어 및 문서 반영 |
| 2026-07-14 | us_screener: NDX 위키 URL·ICB 섹터 열 파싱 복구 |
| 2026-06-12 | 스윙 진입 강화: RSI·정배열·BEAR 차단·타임스탑 연장 |
| 2026-06-09 | Phase4 환율 Z-Score·헷지·GUI 고점보정 등 운영 보강 |
| 2026-06-08 | V8·스윙 본절락 이원화 및 KIS API 한도(EGW00201) 완화 |
| 2026-06-07 | 모듈화·코인 헷지·TWAP·장부 표시 정합 및 Phase4/5 보강 |
| 2026-06-04 | 일봉 OHLCV 검증·교차검증 통일 및 시장 사이클·문서 정리 |
| 2026-06-01 | 멱등·장부 정합·스윙 러너 보강 |
| 2026-05-30 | 스윙·V8·코인 스캔 정책 갱신 및 운영 문서 |
| 2026-05-21 | V8 절대손절 -8%/-12% 캡·문서·GUI 로그·조건검색 |
| 2026-05-20 | 스윙 Pullback·매도선, BEAR 스윙 허용, HTS 조건검색 |
| 2026-05-15 | 스윙 매수 전략 리팩토링 및 조건검색식 |
| 2026-05-13 | Phase4 글로벌·RS/변동성·Hurst 0.45 반영 |
| 2026-05-12 | 조건검색 추가 / 바이낸스·코인 장부 동기화 |
| 2026-05-10~02 | 텔레그램·KIS 호가·코인 페그·GUI 표시·휴장·네트워크 등 운영 개선 |

시대별 성과 해석은 `analysis/STRATEGY_REVIEW.md`·`analysis/strategy_eras.json` 참고.

---

## 주제별 문서 바로가기

| 주제 | 문서 |
|------|------|
| 변경 기록 규칙 | `.cursor/rules/change-log-docs.mdc` |
| Phase5 서킷 | `docs/PHASE5_ACCOUNT_CIRCUIT.md` |
| 스냅샷·오발동 계획 | `docs/EQUITY_SNAPSHOT_CIRCUIT_PLAN.md` |
| KIS GUI 표시 | `docs/KIS_GUI_DISPLAY.md` |
| 전략 복기 | `analysis/STRATEGY_REVIEW.md` |
| 멱등·장부 | `docs/idempotency/` |
| 모듈화 | `docs/MODULARIZATION.md` |

---

## 미커밋 워킹트리 힌트 (2026-08-21 시점)

주요 수정: `strategy/rules.py`, `api/macro_data.py`, `services/ledger_valuation.py`, `execution/circuit_break.py`·`guard.py`·`phase5_ops.py`, `run_bot.py`, `run_gui.py`, `adjust_capital.py`, 시장 사이클, Phase5/스윙/VKOSPI/equity 테스트, `analysis/`, `docs/CHANGELOG.md` 등.
상태 백업 JSON·`bot_state.bak`·임시 `_tmp_*` / `_doc.txt` 등은 **커밋하지 말 것**.

