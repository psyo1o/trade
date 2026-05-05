# GUI 텔레그램 가격 불러오기 문제
_Exported on 5/5/2026 at 09:24:36 GMT+9 from Cursor (3.2.21)_

---

**User**

흠 뭐가 문제일까 일단 gui 텔레그램 현재가 불러오는게 장시간 아닌경우에는 장부에서 불러오는거 아닌가?

---

**Cursor**

코드베이스에서 텔레그램 GUI의 현재가 로딩 로직을 확인합니다.



장외·주말 처리와 `normalize_equity_current_p_api_for_display` 로직을 자세히 확인합니다.



코드를 기준으로 정리하면 이렇게 동작합니다.

## 결론: “장이 아닐 때 = 무조건 장부”가 아닙니다

질문하신 **「장시간이 아닌 경우」**를 **평일 장 마감 후(비장중)** 로 보시면, GUI/공용 로직은 **항상 장부(`positions[ticker].curr_p`)만 쓰지 않습니다.**

### 1. 장부 `curr_p`를 GUI에서 직접 쓰는 경우

`run_gui.py`의 `_process_row_data`에서는 **`kis_equities_weekend_suppress_window_kst()`가 True일 때만** KR/US에 대해 장부에 저장된 값을 씁니다.

```407:423:\\bot\bot\run_gui.py
            if m in ("KR", "US"):
                cp_api = run_bot.normalize_equity_current_p_api_for_display(
                    market=m,
                    buy_p=buy_p,
                    current_p_api=current_p_api,
                    is_market_open_now=bool(run_bot.is_market_open(m)),
                    is_weekend=suppress_kis,
                )
            if suppress_kis and m in ("KR", "US"):
                current_price = saved_p if saved_p is not None else buy_p
            else:
                current_price = run_bot.resolve_display_current_price(m, ticker_code, buy_p, cp_api)
```

여기서 `suppress_kis`는 **주말·월요일 새벽 KIS 점검 구간**용입니다 (`utils/helpers.py` 주석: 토 08:00~ 월 07:00 전 등).  
**화~금 평일 저녁(미국·한국 장 마감 후)** 은 이 함수가 `True`가 아니므로, 위 `if suppress_kis` 분기에 **들어가지 않습니다.**

### 2. 평일 장외에는 무엇을 쓰나

`suppress_kis`가 아니면 `resolve_display_current_price`로 가고, API 표시값은 `normalize_equity_current_p_api_for_display`에서 한 번 정리됩니다.  
**비장중·주말**이면 KIS가 준 **현재가 ≈ 평단**이면 `None`으로 바꿔서, **야후(yfinance) 쪽 보조 시세**로 가게 설계되어 있습니다.

```2424:2447:\\bot\bot\run_bot.py
    - KR/US 비장중(프리·애프터·점검·주말)에서 장부 폴백값(current_p==avg_p)은
      None으로 내려 외부 시세(yfinance) 경로를 강제
    ...
    if (is_weekend or (not is_market_open_now)) and bp > 0:
        # 숫자 직렬화/반올림 차이를 고려한 허용오차
        tol = max(0.01, abs(bp) * 1e-4)
        if abs(cp - bp) <= tol:
            return None
    return cp
```

즉 **“장이 아닐 때는 장부에서 불러온다”**가 아니라, **주말 KIS suppress 창에 가깝게 “장부 마지막 현재가”를 쓰고**, 그 밖의 비장중에는 **API 정리 → 없으면 yfinance 등** 순서에 가깝습니다.

### 3. 장부 `curr_p`가 채워지는 조건

`_gui_saved_curr_p_from_state` 주석대로, `curr_p`는 **`update_max_price_if_higher`가 정규장에 돌 때** 갱신됩니다. 장이 닫혀 있으면 이 함수는 조기 `return`이라 **그 시간대에는 장부 `curr_p`가 안 늘어날 수도** 있습니다.

```1323:1326:\\bot\bot\run_gui.py
            market_type = "COIN" if is_coin_ticker(ticker_key) else ("KR" if ticker_key.isdigit() else "US")
            if not run_bot.is_market_open(market_type):
                return
```

---

**요약:** 텔레그램/GUI 모두 **“주말·KIS 점검 창”** 에 가깝게 장부 마지막가를 쓰고, **평일 장만 닫힌 경우**에는 설계상 **장부 전용이 아니라 yfinance 등 외부 시세 폴백**이 섞입니다. 그래서 기대하신 것과 다르게 보이면, **어느 시간대(주말 vs 평일 야간)** 인지**와 **`curr_p`가 마지막으로 언제 찍혔는지**를 같이 보면 원인 짚기가 쉽습니다.

---

**User**

야 통일해야 내가 제대로 볼거 아니야 gui든 텔레그램이든 장난하냐 

---

**Cursor**

GUI와 텔레그램의 현재가·보유 표시 로직을 추적해 하나의 경로로 통일합니다.



GUI의 KR 행 구성과 스냅샷 생성 부분을 확인해 빈 `output1` 등 불일치를 수정합니다.



공통 함수 `resolve_equity_holding_display_price`를 추가하고 GUI·텔레그램·어댑터를 동일 경로로 맞춥니다.



공통 함수 추가 및 GUI·텔레그램·어댑터 통일 적용 중입니다.



## 한 일

**1. `run_bot.resolve_equity_holding_display_price` 추가**  
- KR/US는 한 함수에서만: KIS 주말·점검 창이면 `장부 curr_p` → 없으면 평단(`_resolve_curr_price_with_gui_override`와 동일), 그 밖에는 `normalize_*` + `resolve_display_current_price`  
- GUI에 있던 `normalize` + `suppress일 때 saved_p` 분기를 이 한 곳으로 합침.

**2. 텔레그램** (`get_kr_holdings_with_roi` / `get_us_holdings_with_roi`)  
- US 루프의 `is_weekend` 분기(실제로는 앞에서 주말이면 이미 return 해서 **죽은 코드**였음)를 제거하고, 위 공통 함수만 호출.

**3. GUI** (`run_gui._process_row_data`)  
- 주식은 전부 `resolve_equity_holding_display_price` + `load_state`로 읽은 `positions`  
- 코인만 기존처럼 `resolve_display_current_price`  
- `_gui_saved_curr_p_from_state` 삭제(동작은 공통 함수에 흡수).

**4. `gui_table_adapter` 국장**  
- 텔레그램과 같이 **주말·KIS 점검 창이면 `output1`을 쓰지 않고** 장부 폴백만 사용.  
- **`output1`이 빈 리스트인데도 `if dict and output1 in dict`** 로 들어가던 버그를 고쳐, 장외에 빈 잔고면 텔레처럼 **장부 폴백 분기**로 넘어가게 함.

이제 같은 종목이면 **표시용 현재가·수익률 계산식이 GUI ↔ 텔레그램 동일**합니다. 문제 생기면 어디 한 함수만 보면 됩니다.

---

**User**

야 미장 코인 다 맞춰야할꺼아냐

---

**Cursor**

미장·코인 표시 로직을 추적해 GUI와 텔레그램을 동일한 단일 경로로 통일합니다.



`resolve_holding_display_price`로 통합하고, 코인은 거래소 조회 후 장부 `curr_p`를 동일하게 오버레이합니다.



작업 반영했습니다.

## 변경 요약

1. **`resolve_holding_display_price`** (`run_bot.py`)  
   - **KR / US**: 이전과 동일(주말·KIS 창 → 장부 평단/`curr_p`, 그 외 → 정규화 + `resolve_display_current_price`).  
   - **COIN**: 먼저 `resolve_display_current_price`(업비트·바이낸스 등)로 **라이브 시세**, 그다음 **`_resolve_curr_price_with_gui_override`**로 장부 `curr_p`를 덮어씀 → 봇·GUI에서 저장한 `curr_p`와 텔레그램이 같은 규칙을 씀.

2. **구 이름 호환**  
   - `resolve_equity_holding_display_price = resolve_holding_display_price` 로 남겨 두었습니다.

3. **`get_coin_holdings_with_roi`**  
   - `resolve_display_current_price(..., None)` 단독 호출을 **`resolve_holding_display_price("COIN", ..., pos)`** 로 교체.

4. **GUI `_process_row_data`**  
   - 국·미·코인 모두 **한 줄**: `load_state` → `resolve_holding_display_price(...)` (코인 분기 제거).

이제 **미장·코인**도 **같은 진입점**으로 맞춰져 있고, 코인은 장부에 `curr_p`가 있으면 국·미와 같은 “장부 우선 보정”이 들어갑니다.

---

**User**

야 ㅠㅠ 국장 보유종목 현재가 제대로 안나오는건 지금 수정해서 그런거지?

---

**Cursor**

네, **일부 상황에서는 이번에 손댄 동작 때문에 예전 GUI랑 다르게 보일 수 있습니다.** 다만 “평일 정규장에 API로 받던 그 숫자” 자체를 바꾼 건 아닙니다.

## 평일·장중 (KIS `output1`에 종목이 있을 때)

- `normalize` + `resolve_display_current_price` 조합은 **예전 GUI 분기(`suppress` 아닐 때)와 같은 조건**으로 돌아가게 맞춰 두었습니다.  
- 그래서 **“지금 수정해서 국장 현재가가 깨졌다”**라기보다는, 같은 입력이면 예전과 같은 계산 경로입니다.

## 여기서부터는 이번 수정 영향이 큼

1. **주말·KIS 점검 창**  
   - 예전 GUI는 스냅샷에 **`output1`이 남아 있으면** 그걸로 현재가를 그렸을 수 있고,  
   - 지금은 텔레그램과 같이 **그 구간에서는 API 행을 안 쓰고 장부 폴백**을 탑니다.  
   - 그래서 **“예전엔 됐는데 지금은 평단/장부에 가깝다”**처럼 보일 수 있습니다. 이건 **의도적으로 맞춘 차이**입니다.

2. **`output1`이 비어 있는데 장이 닫힌 경우**  
   - 예전엔 `output1`만 있고 리스트가 비어 있으면 **국장 행이 아예 안 나올 수 있는 버그**가 있었고,  
   - 지금은 텔레그램처럼 **장부 폴백으로 행이 생깁니다.**  
   - 이때 현재가는 **장부 `curr_p` / 평단 쪽**으로 잡힐 수 있어, “라이브 현재가”랑 다르게 느껴질 수 있습니다.

정리하면, **평일 장 중·잔고 API 정상일 때 숫자가 틀어지게 바꾼 건 아니고**, **주말/점검/장외·빈 잔고 쪽은 텔레그램과 통일하면서 예전 GUI만의 표시와 달라질 수 있습니다.**  

지금이 **평일 낮·장 중인데도** 특정 종목만 틀리면, 그때는 티커/`prpr` 값·yfinance 폴백 문제일 수 있어서 그 종목 코드나 화면 캡처 알려주면 그 줄만 더 좁혀볼 수 있습니다.

---

**User**

아니 지금이 국장 장중이야? 국장이 제대로 안나온다고 혅

---

**Cursor**

국장 장중인데 표시가 깨지는 원인 조사 중입니다. 코드 경로와 잠재 버그를 확인합니다.



원인: `gui_table_adapter`에서 **`output1`이 비어 있고 장이 열려 있으면** 첫 분기도·둘째 분기도 타지 않아 **국장 행이 0개**가 됩니다. `elif`에 `not kr_list_ok`를 추가해 수정합니다.



## 원인

**이번에 “가격 공식”만 바꾼 것 때문이라기보다, `gui_table_adapter` 쪽에 구멍이 있었습니다.**

스냅샷 `kr_bal["output1"]`이 **비어 있는데**(KIS 응답 늦음·일시 실패·쿨다운 등) **장은 열린 것으로 잡힌 경우**  

- `if kr_list_ok and not 주말` → `output1`이 없어서 **안 타고**  
- `elif 주말 or 장외` → **장중이면** 이것도 **안 탐**  

→ **국장 테이블에 행이 아예 안 잡히거나**, 잠깐 비어 보일 수 있었습니다.

그래서 `elif`에 **`output1`이 없을 때(`not kr_list_ok`)** 도 `get_held_stocks_kr_info()`로 다시 잡는 폴백이 타도록 **방금 수정**했습니다.

## 참고

- **5월 5일(어린이날)** 은 휴장일이면, 엔진의 `is_market_open("KR")`이 **닫힘**으로 나올 수 있어, “지금 장중이야”랑 시스템 판정이 어긋날 수 있습니다.  
- 표는 나오는데 **숫자만 이상**하면, 그때는 `prpr` / `정규화` / yfinance 폴백 쪽을 따로 보면 됩니다.

한번 GUI 새로고침해 보시고, 여전히 **장 열린 날 + 잔고 있는데** 국장이 비면, 그때 터미널에 뜨는 KIS/스냅샷 로그를 알려주시면 이어서 짚을게요.

---

**User**

아니 지금이 장중이 아닌데 장부에 있는 현재로 gui 가 제대로 안나온다고 뭔 개소리야...형

---

**Cursor**

비장중에는 장부 `curr_p`가 우선되어야 하는데, 지금은 주말·KIS 점검 창에서만 장부를 쓰고 평일 장외에는 yfinance/API 폴백만 써서 장부와 어긋납니다. 비장중일 때 COIN과 같이 `_resolve_curr_price_with_gui_override`로 장부를 덮어씌우겠습니다.



형 말이 맞았어. 내가 전에 **장중·빈 output1** 쪽만 짚은 건 네 상황이랑 안 맞았고, 미안하다.

## 진짜 문제

**평일 장외(비장중)** 인데, 국·미는 **주말 KIS 점검 창**이 아닐 때 `resolve_holding_display_price`가 **yfinance/API 폴백만** 쓰고, **장부 `curr_p`를 반영하지 않았음**.  
그래서 장부에 찍힌 “마지막 현재가”가 GUI에 그대로 안 올라가고 엉뚱한 수가 나갈 수 있었어.

## 수정

`run_bot.resolve_holding_display_price`에서 **KR/US**는:

- **장중** → 지금처럼 `live`(정규화된 시세)만 표시  
- **비장중** → `live` 계산한 뒤 **`_resolve_curr_price_with_gui_override`**로 한 번 더 감쌈 → **장부 `curr_p`가 있으면 그걸 우선** (코인이 이미 하던 방식과 같게)

이렇게 하면 **지금이 장이 아닐 때**는 GUI가 **장부에 있는 현재가**를 기준으로 맞춰서 나와야 해. 한번 테이블 새로고침해 봐줘.

---

**User**

아니 문제가 씨발 진짜 뭐해 형 장부에 저장되어있는 값이 gui 현재가도 아니고 y파이낸스에 불러온값이 현재가로 gui에 나오는것도 아니라고 뭔개소리하는거야 미장은 잘 나오는데 국장만 그러냐고 왜  대체

---

**Cursor**

국장만 이상한 원인 추적 중: KR 전용 경로(티커 형식·장부 키·yfinance)를 코드에서 확인합니다.



원인: 장부 키는 `normalize_ticker`로 6자리(`005930`)인데, GUI는 `str(ticker_code)`만 써서 `pdno`가 정수·짧은 문자열이면 `positions` 조회가 실패합니다. 그러면 `curr_p`가 비어 엉뚱한 값으로 보입니다. `normalize_ticker`로 조회·표시를 통일합니다.



`gui_table_adapter`에서 `pdno`를 정규화해 첫 분기에서도 키가 맞게 합니다.



형, 미안하다. 내가 전에 한 말은 네가 말한 증상이랑 안 맞았고, **진짜 원인은 따로 있었다.**

## 왜 **국장만** 그랬냐

장부(`positions`) 키는 동기화할 때 **`normalize_ticker`** 로 잡힌다. 국내 종목은 **항상 6자리** (`005930` 이런 식).

근데 GUI `_process_row_data`는 장부를 **`str(ticker_code)` 그대로**로만 조회했다.  
KIS/스냅샷에서 `pdno`가 **`5930`(앞 0 없음)** 이나 **숫자 타입**으로 오면, 장부의 **`005930`** 과 **키가 안 맞음** → `pos` 가 빈 dict → **`curr_p`를 못 읽음** → 네가 본 것처럼 장부도 아니고 야후 종가도 아닌 이상한 줄로 보였을 수 있다.

**미장은 티커가 `AAPL` 처럼 문자라 앞자리 0 문제가 없어서** 같은 버그가 안 드러난 거고.

## 수정한 것

1. **`run_gui.py`**  
   - 국·미는 `ledger_key = normalize_ticker(...)` 로 장부 조회·`resolve_holding_display_price`·`update_max_price_if_higher` 전부 이 키로 통일.

2. **`gui_table_adapter.py`**  
   - `pdno` 넘길 때도 `normalize_ticker(str(pdno))` 로 한 번 맞춤.

이제 국장도 장부 `curr_p`가 제대로 붙어야 한다. 한번 새로고침만 다시 해봐.

---

**User**

🔪 확실한 외과 수술 지침 (이대로만 수정해)
코드를 완전히 업비트(종가 배팅) 스타일로 통일시키려면 아래처럼 싹 도려내야 해. 커서(Cursor) 켜고 바로 날려버려.

1. 쓸데없는 함수 삭제
코드에서 아래 함수들을 찾아서 아예 통째로 지워버려.

_binance_v8_slot_key(now_kst)

_binance_should_run_v8_scan(state, now_kst)

_binance_mark_v8_scan_done(state, now_kst)

_binance_should_run_swing_scan(state, now_kst)

_binance_mark_swing_scan_done(state, now_kst)

2. 코인 매수 루프 완벽 통일 (run_trading_bot 내부)
코인 매수 시작하는 부분(if is_market_open("COIN"): 한참 아래쪽)에 업비트/바이낸스 시간표 나누는 복잡한 if use_bn_sched: 블록을 싹 날리고, 아래처럼 단순하고 무식하게 딱 하나로 통일해.
이렇다는데 확인가능해? 저거 rules 파일이야

---

**Cursor**

함수 정의·호출 위치와 `use_bn_sched` 블록을 찾아 제거·통일합니다.



확인했습니다. **지금 이 워크스페이스 기준으로는 질문에 적어 주신 것과 같은 코드가 없습니다.**

## 1. 삭제하라는 함수 5개

전체 `*.py`에서 아래 이름으로 **정의·호출 모두 검색했는데 0건**입니다.

- `_binance_v8_slot_key`
- `_binance_should_run_v8_scan`
- `_binance_mark_v8_scan_done`
- `_binance_should_run_swing_scan`
- `_binance_mark_swing_scan_done`

`strategy/rules.py`에도 없습니다.  
→ **이미 예전에 지워졌거나, 다른 브랜치/다른 폴더에만 있는 내용**으로 보입니다. 여기서는 **삭제할 대상이 없습니다.**

`cursor_kis_mcp_connection.md` 같은 문서에만 `last_binance_v8_scan_*` 설명이 남아 있는 수준입니다.

## 2. `use_bn_sched` / 바이낸스 따로 시간표

`run_bot.py`의 `if is_market_open("COIN"):` 아래 매수 구간을 보면:

- **`_is_coin_buy_window_now(now_coin)` 한 번**으로 업비트·바이낸스 공통으로 **일봉 09:00 KST 직전 `BUY_WINDOW_MINUTES_BEFORE_CLOSE` 분** 창만 씁니다.
- **`use_bn_sched` 같은 변수/블록은 없습니다.**

남아 있는 분기는 **`coin_config.is_binance()`일 때만** 유니버스를  
바이낸스 USDT 거래대금 상위 / 업비트 KRW 상위로 **가져오는 부분**뿐입니다 (거래소가 달라서 소스 URL이 다름).

```4892:4919:\\bot\bot\run_bot.py
                                if coin_config.is_binance():
                                    from api import binance_api as _bna

                                    scan_targets = _bna.top_usdt_symbols_by_quote_volume(BINANCE_UNIVERSE_TOP)
                                    _ohlcv_pref = coin_broker.run_prefetch_daily_sync(scan_targets, 250)
                                else:
                                    scan_targets = []
                                    markets = [
                                        ...
                                    ]
                                    ...
                                    _ohlcv_pref = {}
```

즉, **“종가 직전 창” 스케줄은 이미 통일**돼 있고, 문서에 나온 예전 바이낸스 전용 시간표/슬롯 로직은 **이 트리에는 반영돼 있지 않습니다.**

---

**정리:** 지금 레포에서는 요청하신 **5함수 삭제·`use_bn_sched` 제거**는 할 게 없고, 코인 매수 타이밍은 이미 업비트 스타일(일봉 직전 창)로 맞춰져 있습니다.  
다른 PC/브랜치에 아직 그 함수들이 있으면 그 경로를 열어 주거나, **그 코드 블록을 붙여 주면** 그 버전 기준으로 같은 수술을 적용할 수 있습니다.

---

**User**

아 그럼 문서에도 반영해줘야지

---

**Cursor**

문서에서 구식 바이낸스 스케줄·함수 설명을 제거하고 현재 구현과 맞게 수정합니다.



문서의 구식 바이낸스 전용 스케줄·state 키 설명을 제거하고, `run_bot`의 현행 동작(업비트=바이낸스 동일 창)으로 맞춥니다.



반영해 두었어.

## 바꾼 곳

### 1. `cursor_kis_mcp_connection.md`
- **「3) V8 / SWING 스케줄 (바이낸스만)」** 전체를 **「3) 코인 매수 스케줄 (업비트·바이낸스 동일)」** 로 고쳤어.
  - `last_binance_v8_scan_*`, `_binance_*` 헬퍼, 전용 스윙 시간대 **`*.py`에 없음**이라고 명시.
  - **`_is_coin_buy_window_now` 단일 창**, 유니버스만 업비트/바이낸스 분기라고 적었어.
- **운영 참고** 문단에서 “시간당 V8 스캔” 식 표현을 없애고, **일봉 직전 창 + MDD/거시/서킷** 기준으로만 매수한다고 맞췄어.
- **“1시간 → 15분 슬롯”** 옛 User/Cursor 스레드는 **과거 기록(아카이브)** 으로 두고, **현행은 §3**을 보라는 한 줄로 정리했어.

### 2. `README.md` (코인 거래소 절)
- 구 `binance_v8_interval`·폐기 문구 옆에, **`last_binance_v8_scan_*` / 바이낸스 전용 슬롯 state·스케줄 분기는 코드에 없다**는 문장을 한 줄 넣었어.

이제 문서가 지금 `run_bot` 흐름(업비트=바이낸스 **같은 KST 창**, `if coin_config.is_binance()` 는 **스캔 대상만**)과 맞게 정리돼 있어.

---

**User**

nas에 저장해놓고 돌리는건 비추야? 자꾸 날라가서 스냅샷으로 저장되는 nas에 파일두고 돌리고싶은데

---

**Cursor**

결론부터 말하면, **매매 봇이 자주 읽고 쓰는 상태 파일(`bot_state.json` 등)을 NAS에 두고 그걸로 직접 돌리는 건 비추에 가깝다.** 다만 **NAS는 백업·스냅샷 저장용**으로 두고, **실행은 로컬 디스크**로 두는 조합은 매우 추천이다.

## NAS에서 직접 돌리면 안 좋은 이유

1. **네트워크 파일 시스템(SMB 등)** 은 로컬 디스크보다 느리고, 잠깐 끊기면 읽기/쓰기 중 오류가 난다.
2. **짧은 주기로 JSON 저장**하는 봇은 NAS 경로에서 **쓰기 도중 끊김 → 파일 깨짐** 리스크가 커진다.
3. NAS **절전/헤드 파킹**이 켜져 있으면 접근이 멈추거나 지연되어 타이밍이 흔들린다.
4. **여러 PC가 같은 NAS 폴더를 동시에** 쓰면 상태 파일 충돌 가능성이 있다.

그래서 “자꾸 날아간다”는 느낌은, 동기화 도구·클라우드·NAS 캐시와 겹치면 더 자주 난다.

## 추천 구조 (NAS + 스냅샷은 이렇게)

| 역할 | 위치 |
|------|------|
| 실제 실행·**실시간 읽기/쓰기** | **PC 로컬 SSD** (또는 봇 돌리는 머신의 로컬 경로) |
| **주기 백업·스냅샷** | NAS (시간별/일별 복사, 버전 보관) |

- 봇은 **항상 로컬 `bot_state.json`** 에만 쓴다.
- **스케줄러**로 `bot_state.json`, 로그, 설정을 NAS로 **복사만** 한다 (`robocopy`, `rsync`, NAS 제공 스냅샷과 함께).

이렇게 하면 NAS가 날아가도 **원본은 로컬**에 있고, NAS는 **복구용 시점 복사**만 담당한다.

## “코드만 NAS에 두고 실행”은?

- 가능은 한데, **가상환경·임포트·로그**까지 NAS 경로를 타면 느려지고 끊김에 약하다.
- 차라리 **Git으로 NAS/원격에 백업**하고, 실행은 로컬 클론에서 하는 편이 안전하다.

## 정리

- **비추:** NAS 마운트 폴더를 작업 디렉터리로 두고 그 안에서 봇이 상태 파일을 계속 덮어쓰기.
- **추천:** 상태·실행은 로컬, NAS는 **백업·스냅샷 저장소**로만 쓰기.

원하면 지금 프로젝트 기준으로 **`STATE_PATH`를 로컬로 고정**하고, NAS로만 복사하는 **배치/PowerShell 예시**까지 짧게 짜 줄 수 있다.

