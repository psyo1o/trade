# 현재 상태 (CURRENT)

**최종 갱신:** 2026-10-13

## 지금

- 미장 실적 차단·국장 급락 청산은 **기본 shadow** (`earnings_block_mode`, `kr_crash_exit_mode`). 주문·포지션은 그대로, 로그만 `logs/shadow_events.jsonl`.
- `live` 로 바꾸면 실적 10거래일(당일 장후 포함) 미장 신규 매수 차단, KST 15:15~15:20 에 KODEX200 또는 상장시장 지수 ≤ -3% 이면 국장 전량 매도.
- V8 타임스탑은 72h(3거래일), 스윙 러너는 10MA (2026-09-12~). README 상수표를 코드와 맞춤.

## 다음

- [ ] 재시작 후 시작 로그 `[RULES] ...=shadow` 확인
- [ ] 며칠 shadow 로그를 `py -3.11 analysis/run_shadow_review.py` 로 보고 `live` 전환 여부 결정
- [ ] 봇 재시작 후 보유 종목 로그에 `[OHLCV]` 재조회가 4h 간격으로 찍히는지 확인

## 주의

- 키를 빼면 shadow 다. `off` 가 아니면 15:15 국장 보유가 있을 때 지수 조회(야후)가 한 번 나간다. 실패하면 매도하지 않는다.
- 실적일 조회 실패는 매수를 막지 않는다(fail-open).
- 토큰 발급 1분 제한(EGW00133) — 연속 강제 재발급 자제.
- 스윙 매도선은 래칫(하향 없음) — SHOP $161.37은 정상값.

## CHANGELOG

- [2026-10-13 실적 차단·국장 급락 청산 shadow](CHANGELOG.md)
- [2026-10-09 고점 보정 시장 통화](CHANGELOG.md)
- [2026-10-08 일봉 캐시 TTL](CHANGELOG.md)
