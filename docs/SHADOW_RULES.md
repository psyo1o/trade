# 섀도우 규칙

기본은 **shadow** 입니다. `off` 와 `shadow` 는 기존 매수·매도를 바꾸지 않습니다. `live` 만 실제로 막거나 팝니다. 키를 `config.json` 에 넣지 않아도 shadow 로 동작합니다. 바꾼 뒤에는 **봇/GUI를 완전히 재시작**합니다.

| 키 | 값 |
|----|----|
| `earnings_block_mode` | `off` \| `shadow` \| `live` (기본 `shadow`) |
| `kr_crash_exit_mode` | `off` \| `shadow` \| `live` (기본 `shadow`) |

기동 로그: `[섀도우 규칙] earnings_block_mode=... kr_crash_exit_mode=...`

## 1) 미장 실적 진입 차단

- 대상: **미국 주식 신규 매수**만. 국장·코인에는 적용하지 않습니다.
- 다음 실적 발표일이 매수일 포함 **10거래일(day 0~9)** 안이면 차단 후보입니다. day 10 은 통과입니다. 거래일은 NYSE 달력(실패 시 월~금)입니다.
- 실적일을 못 구하면 **fail-open**: 매수는 진행하고, 조회 실패를 로그에 남깁니다.
- `shadow`: 매수 주문은 그대로 두고 `[SHADOW-EARNINGS]` (티커, 의도 가격/수량, 실적일, 거래일 수).
- `live`: 그 매수를 넣지 않습니다.
- `off`: 조회도, 로그도, 차단도 없습니다.

## 2) 국장 지수 급락 청산

- 창: KST **15:15:00 이상 ~ 15:20:00 미만**. 매매는 `:00/:15/:30/:45` 라서 **15:15 회차만** 들어옵니다.
- 판정 시각은 그 회차의 **사이클 시작 시각**입니다. 국장 루프가 창을 조금 넘겨도 같은 회차로 평가합니다.
- **날짜당 한 번**. `bot_state.json` → `shadow_rules.kr_crash_evaluated_dates`. 같은 날 재시작이 창 안이어도 다시 평가하지 않습니다. 보유가 없거나 지수 조회가 예외로 끝나면 날짜를 소모하지 않습니다.
- 조건: **KODEX200(069500)** 또는 그 종목의 상장시장 지수(KOSPI 종목은 KOSPI `^KS11`, KOSDAQ 종목은 KOSDAQ `^KQ11`)가 전일 종가 대비 **-3% 이하** (`INDEX_CRASH_KR`, 매수 급락 필터와 같은 등락률 공식).
- 해당 국장 보유는 수익·손실 구분 없이 **전량** 후보입니다.
- 상장시장을 모르면 KODEX200만 봅니다. 지수 조회 실패(None)는 급락으로 치지 않습니다.
- `shadow`: 매도하지 않고 `[SHADOW-KRCRASH]` (티커, 수량, 현재가, 평단, 미실현%, 발동 지수와 등락률).
- `live`: 기존 KIS 시장가 매도 (`create_market_sell_order_kis`, lane `kr_crash`).
- `off`: 평가하지 않습니다.

## 3) 로그와 복기

파일: 저장소 루트의 **`shadow_events.jsonl`** (`trade_history.json` 과 같은 폴더). 한 줄에 JSON 하나, append-only.

```json
{"timestamp":"2026-10-13T15:16:00+09:00","rule":"kr_crash_exit","mode":"shadow","market":"KR","ticker":"005930","details":{}}
```

`timestamp` 는 KST ISO. `rule` 은 `earnings_block` 또는 `kr_crash_exit`.

```bat
python tools/analyze_shadow.py
python tools/analyze_shadow.py --csv shadow_report.csv
```

표준출력(한국어): 규칙별 건수, 실적은 섀도우로 통과한 매매가 나중에 청산된 `profit_rate`, 국장 급락은 섀도우 시각의 가격과 이후 실제 청산가. `--events` · `--history` 로 경로를 바꿀 수 있습니다.

코드: `strategy/shadow_rules.py`, `strategy/shadow_report.py`, 미장 매수 `execution/market_cycles/us_buy_cycle.py`, 국장 `execution/market_cycles/kr_cycle.py`.
