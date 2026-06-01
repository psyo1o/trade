# 실계좌 멱등 스모크 (`test_mode=false`)

운영 전 **소액·1종목**으로 확인. `config.json` → `"test_mode": false` 후 **봇 완전 재시작**.

## 공통

- [ ] `bot_state.json` 에 `order_idempotency`, `buy_inflight`, `sell_inflight` 키 존재
- [ ] 사이클 로그에 `멱등` prune 메시지(삭제 건수) 또는 무삭제 — 오류 없음
- [ ] 동일 15분 슬롯에 같은 종목 **두 번 매수 시도 없음** (♻️ 또는 in-flight 패스)

## 매수 TWAP

| # | 시장 | 확인 | 로그 키워드 |
|---|------|------|-------------|
| M1 | KR | 소액 1종 TWAP 또는 1슬라이스 | `[KR BUY TWAP]`, `ok=True` |
| M2 | US | (선택) 1주 미만 소액 | `[US BUY TWAP]` |
| M3 | COIN | 최소주문 이상 1종 | `[COIN BUY TWAP]`, 바이낸스 시 clientOrderId |

- [ ] 실패 응답 후 **잔고만 늘었을 때** `잔고검증` note (의도적 네트워크 끊기는 위험 — 생략 가능)
- [ ] `order_idempotency` 에 `KR:…:buy:…:0` filled 기록

## 매도

| # | lane | 확인 |
|---|------|------|
| S1 | `swing_half` | (스윙 보유 시) HALF 1회만, ♻️ 재호출 없음 |
| S2 | `exit` | 타임/하드/트레일 전량 1회 |
| S3 | `scale_out` | V8 익절 TWAP 시 슬라이스별 로그 |
| S4 | `manual` | GUI 수동 매도 1회 |
| S5 | `phase5` | (테스트 환경만) 서킷 청산 시 동일 티커 중복 없음 |

- [ ] `[KR EXIT]` / `[US EXIT]` / `[COIN EXIT]` `ok=True`
- [ ] 실패 시 `bot_state.json` 에 failed 기록 유지 (`persist_idempotency`)

## 잔고 조회

- [ ] 한 슬라이스 동안 KIS 잔고 API **과다 호출 없음** (로그·브로커 rate limit)
- [ ] 체결 직후 다음 슬라이스에서 수량 baseline 갱신 (`refresh=True` 경로)

## 롤백

문제 시: `test_mode=true` 로 되돌리고 재시작. `order_idempotency` 는 7일 prune 또는 수동 삭제 가능.
