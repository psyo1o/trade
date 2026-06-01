# 멱등 분석 요약 (2026-05)

## 위험 구간

1. **TWAP 매수 슬라이스 재시도** — 타임아웃 후 재주문 → 이중 체결
2. **매도 3회 재시도** — rt_cd 실패인데 체결됨 → 이중 매도
3. **15분 사이클 반복** — 장부 저장 실패 후 동일 신호 재매수
4. **스윙 HALF** — `scale_out_done` 전 API 재시도

## 이미 있던 앱 레벨 방어

- 보유 중 매수 스킵 · `cooldown` / `ticker_cooldowns`
- `scale_out_done` (V8·스윙 HALF 후)
- TWAP 매수 후 장부 1회 등록

## 적용 방향

- **order_idempotency** 장부에 filled/submitted/failed
- **buy_inflight / sell_inflight** 동일 사이클 중복 시도 차단
- **브로커 clientOrderId** (바이낸스)
- **잔고 증감**으로 KIS/업비트 응답 불일치 보정
