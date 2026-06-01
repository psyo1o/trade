# 모듈화 작업북 (AI 컨텍스트·유지보수)

> **목적:** Cursor 등 AI와 작업할 때 **불필요하게 큰 파일을 통째로 읽지 않게** 하고,  
> 사람이 **한 번에 한 단계**만 안전하게 리팩터링할 수 있게 한다.  
> 상세 배경·리스크는 `MODULARIZATION_ROADMAP_2026-04-22.md` 를 참고.

**최종 갱신:** 2026-05-24

---

## 1. 모듈화가 꼭 필요한가? (토큰 관점)

### 결론

| 목적 | 모듈화 필수? | 더 싼 방법 |
|------|-------------|------------|
| **AI 대화 토큰 줄이기** | **필수는 아님** | 질문을 파일·함수 단위로 좁히기, 아래 「AI에게 시킬 때」 참고 |
| **코드 유지보수·버그 격리** | **장기적으로 권장** | 이미 일부 완료(`services/`, `execution/`, `api/kis_parsers.py`) |
| **실행 속도·API 토큰(KIS 등)** | **무관** | 모듈 나눠도 런타임 비용은 거의 동일 |

지금 **가장 큰 파일** (대략):

| 파일 | 줄 수(2026-05) | AI가 통째로 읽으면 |
|------|----------------|-------------------|
| `run_gui.py` | ~4,000 | 컨텍스트 많이 소모 |
| `run_bot.py` | ~3,800 | 동일 |
| `strategy/rules.py` | ~1,200 | 스윙·V8 섞여 있으면 부분만 읽어도 됨 |

모듈화를 **끝까지** 하면 에이전트가 `execution/coin_cycle.py` 만 열 수 있어 **평균 토큰은 줄어듭니다.**  
다만 **당장**은 문서·규칙·질문 방식만으로도 상당 부분 절약 가능합니다.

---

## 2. 모듈화 없이 토큰 아끼는 습관 (먼저 이것부터)

AI에게 시킬 때 예시:

- ❌ 「`run_bot.py` 전체 보고 코인 매도 고쳐줘」
- ✅ 「`run_bot.py` 의 코인 매도 루프( `check_swing_exit` 호출 근처)만 보고, 15분 보호는 ` _new_buy_sell_protection_blocks` 유지」

**자주 쓰는 앵커 (검색 키워드)**

| 주제 | 먼저 볼 파일·심볼 |
|------|-------------------|
| 코인 매도·보호 | `run_bot.py` → `check_swing_exit`, `_new_buy_sell_protection_blocks` |
| 스윙 손절·매도선 | `strategy/rules.py` → `get_swing_hard_stop_floor`, `get_swing_exit_display_price` |
| V8 매도선 | `strategy/rules.py` → `get_final_exit_price` |
| Phase5 서킷 | `execution/guard.py` |
| GUI 멈춤·갱신 | `run_gui.py` → `BalanceUpdaterThread`, `refresh_balance` |
| GUI 무창 실행 | `launch_gui.py`, `start_gui_once.vbs` |
| 장부 동기화 | `execution/sync_positions.py` |

**입문용 목차:** `PROJECT_STRUCTURE.txt`  
**전략 설명:** `README.md` §8

---

## 3. 이미 끝난 것 (다시 하지 않음)

- [x] `services/account_read_facade.py` — KIS/코인 조회 통합
- [x] `services/account_snapshot.py` — 생존신고·스냅샷
- [x] `services/gui_table_adapter.py` — GUI 표
- [x] `api/kis_parsers.py` + `tests/test_kis_parsers.py`
- [x] `run_trading_bot` 2차 — 저위험 helper 추출 (본문 축소)
- [x] V8 + SWING_FIB 공존 (`strategy_type` 분기)
- [x] (2026-05) 스윙 평단 위 손절 -3% 클램프, 15분 매수 보호, Phase5 스파이크 필터
- [x] (2026-05) `launch_gui.py` / `start_gui*.vbs` — bat 대체·무창 실행

---

## 4. 단계별 체크리스트 (여기부터 차근차근)

원칙: **한 PR(또는 한 대화) = 한 단계.** 동작 변경 없이 **이동만**.  
각 단계 후:

```powershell
py -3.11 -m py_compile run_bot.py run_gui.py strategy/rules.py execution/guard.py
py -3.11 -m unittest discover -s tests -p "test_*.py" -v
```

(환경에 맞게 일부만 돌려도 됨 — 해당 모듈 테스트 우선)

---

### Step 0 — AI 규칙만 (코드 0줄, 즉시 효과)

- [ ] `.cursor/rules/` 에 짧은 규칙 추가 예:
  - 「`run_bot.py` / `run_gui.py` 전체 읽기 금지, 사용자가 준 함수명·줄 근처만」
  - 「전략 변경은 `strategy/rules.py`, 장부는 `execution/guard.py` + `sync_positions.py`」
- [ ] 이 작업북 링크를 규칙에 한 줄 넣기

**완료 기준:** 다음 AI 세션에서 불필요한 대용량 read 감소 (체감).

---

### Step 1 — P0-A: 코인 사이클만 분리 (가장 효과 큼)

**목표:** `run_bot.py` 에서 코인 매도·매수 블록만 새 모듈로 **복사 이동** (로직 동일).

| 항목 | 내용 |
|------|------|
| 새 파일 | `execution/market_cycles/coin_cycle.py` (또는 `run_bot/coin_cycle.py`) |
| 옮길 것 | `run_trading_bot` 안 코인 엔진: 매도 루프, 매수 스캔, TWAP 호출 **진입점** |
| `run_bot.py` | `from execution.market_cycles.coin_cycle import run_coin_cycle` 한 줄 호출 |
| 테스트 | `tests/test_swing_buy_protection.py`, `tests/test_phase5_peak_spike_filter.py` |

**완료 기준 (DoD):**

- [ ] 1사이클 로그 태그·순서 동일 (`[코인 매도 루프]`, `[V8-BUY]`, `[SWING-BUY]` …)
- [ ] `run_bot.py` 줄 수 **300줄 이상** 감소
- [ ] AI에게 「코인 매도만」이라고 하면 **새 파일 하나**만 열면 됨

**예상 AI 토큰:** 코인 이슈 시 `run_bot.py` 3,800줄 → ~800줄 파일만 읽으면 됨.

---

### Step 2 — P0-B: KR / US 사이클 분리

Step 1과 동일 패턴:

- [ ] `execution/market_cycles/kr_cycle.py`
- [ ] `execution/market_cycles/us_cycle.py`
- [ ] `run_trading_bot` = 동기화 → 리스크 → `run_kr_cycle` → `run_us_cycle` → `run_coin_cycle`

**완료 기준:** 국·미·코인 한 시장 장애 시 다른 시장 로그는 기존과 동일하게 이어짐.

---

### Step 3 — P0-C: 전략 라우터 (진입·청산 결정만)

**목표:** `strategy_type` / `check_swing_exit` / `calculate_pro_signals` **호출 위치**를 라우터로 모음. 규칙 수식은 `strategy/rules.py` 유지.

| 새 파일 | 역할 |
|---------|------|
| `strategy/entry_router.py` | V8 시도 → 스윙 fallback → `(strategy_type, sl_p, meta)` |
| `strategy/exit_router.py` | `SWING_FIB` → swing exit / else V8 exit |

- [ ] `tests/test_strategy_router.py` — V8 우선·스윙 fallback 최소 케이스
- [ ] `tests/test_exit_routing.py` — 스윙 포지션에 V8 `check_pro_exit` 미호출

**완료 기준:** ZBT류 버그 수정 시 `rules.py` + `exit_router.py` 만 보면 됨.

---

### Step 4 — P1: `rules.py` 스윙 / V8 파일 분리 (선택, 토큰↓)

| 새 파일 | 옮길 함수 예 |
|---------|-------------|
| `strategy/swing_rules.py` | `check_swing_entry/exit`, `get_swing_*` |
| `strategy/v8_rules.py` | `calculate_pro_signals`, `get_final_exit_price`, `check_pro_exit` |
| `strategy/rules.py` | re-export만 (import 호환) |

- [ ] 기존 `from strategy.rules import …` 깨지지 않게 re-export

**완료 기준:** 스윙만 손볼 때 `swing_rules.py` (~400줄)만 읽으면 됨.

---

### Step 5 — P1: GUI 얇게 만들기

| 새 파일 | 옮길 것 |
|---------|--------|
| `gui/balance_worker.py` | `BalanceUpdaterThread` |
| `gui/launcher.py` | (선택) `launch_gui.py` 는 루트 유지 가능 |

- [ ] `run_gui.py` 는 탭·타이머·시그널 연결만 (~1,500줄 이하 목표)

**완료 기준:** 「GUI 응답 없음」 수정 시 `balance_worker.py` + `update_max_price_if_higher` 만 검토.

---

### Step 6 — P2: 나머지 (여유 있을 때)

- [ ] `execution/state_schema.py` — `positions` 키 정규화
- [ ] `services/report_formatter.py` — 텔레·heartbeat 문자열
- [ ] `execution/order_executor.py` — KR/US/COIN 주문 공통 껍데기

로드맵 상세: `MODULARIZATION_ROADMAP_2026-04-22.md`  
이전 메모: `MODULARIZATION_NEXT_STEPS_2026-04-21.md`

---

## 5. 한 세션에서 이렇게 진행하기 (추천 대화 템플릿)

**Step 1 시작할 때 AI에게:**

```text
MODULARIZATION_WORKBOOK.md Step 1만 진행해줘.
- execution/market_cycles/coin_cycle.py 로 코인 사이클만 이동
- run_bot.py 는 run_coin_cycle() 호출만
- 로직·로그·save_state 위치 변경 금지
- 끝나면 py_compile + test_swing_buy_protection 실행
```

**끝난 뒤 이 문서에서 `[ ]` → `[x]` 로 직접 체크.**

---

## 6. 우선순위 한 줄

1. **Step 0** (규칙) — 오늘, 5분  
2. **Step 1** (코인 사이클) — AI·유지보수 효과 최대  
3. **Step 2** (KR/US)  
4. **Step 3** (라우터)  
5. Step 4~6 — 시간 날 때  

**모듈화를 “토큰만” 위해 전부 할 필요는 없습니다.**  
Step 0 + 질문을 좁히는 것만으로도 충분한 경우가 많고, **Step 1~3** 을 하면 체감이 크게 납니다.

---

## 7. 관련 파일 맵 (모듈화 후 목표)

```
run_bot.py              … 얇은 오케스트레이션 (~800줄 목표)
execution/
  market_cycles/
    kr_cycle.py
    us_cycle.py
    coin_cycle.py
  guard.py, sync_positions.py, …
strategy/
  rules.py              … re-export (호환)
  swing_rules.py
  v8_rules.py
  entry_router.py
  exit_router.py
run_gui.py              … UI + 타이머
gui/                    … (Step 5) balance_worker 등
launch_gui.py           … 프로세스 런처 (유지)
```

---

## 8. 다음에 같이 할 작업

대화에서 **「Step 1 시작」** 이라고만 하면, 이 문서 기준으로 `coin_cycle.py` 분리부터 진행하면 됩니다.
