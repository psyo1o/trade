# 현재 상태 (CURRENT)

**최종 갱신:** 2026-10-13

## 지금

- 섀도우 규칙 기본 `shadow`: 미장 실적 10거래일 진입 후보·국장 15:15 지수 -3% 전량 청산 후보를 **로그만** (`shadow_events.jsonl`). live 로 바꾸기 전에는 주문이 바뀌지 않음.
- 복기: `python tools/analyze_shadow.py`. 설명은 `docs/SHADOW_RULES.md`.
- 일봉 캐시 TTL 4h. 고점 보정은 시장 통화 입력.

## 다음

- [ ] 재시작 후 기동 로그의 `earnings_block_mode`·`kr_crash_exit_mode` 가 shadow 인지 확인
- [ ] 15:15 KST 회차 이후 `shadow_events.jsonl` 에 `[SHADOW-KRCRASH]` 가 조건에 맞을 때만 쌓이는지 확인

## 주의

- 모드 변경은 `config.json` 후 **봇/GUI 재시작**. 기본 shadow 는 실주문을 막거나 넣지 않음.
- 토큰 발급 1분 제한(EGW00133) — 연속 강제 재발급 자제.
- 스윙 매도선은 래칫(하향 없음). 러너 트레일은 10MA, 절대 손절 하한은 평단×0.96.

## CHANGELOG

- [2026-10-13 섀도우 규칙](CHANGELOG.md)
- [2026-10-09 고점 보정 시장 통화](CHANGELOG.md)
- [2026-10-08 일봉 캐시 TTL](CHANGELOG.md)
- [2026-09-18 토큰 전 경로 동기화](CHANGELOG.md)
