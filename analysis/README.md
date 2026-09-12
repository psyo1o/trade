# 매매 기록 사후 분석 (Trade Review)

운영 중인 `run_bot.py` / `bot_state.json` 과 **완전 분리**된 읽기 전용 분석 도구입니다.

## 실행

```bash
py -3.11 analysis/run_analysis.py
```

옵션:

| 옵션 | 설명 |
|------|------|
| `--no-fetch` | OHLCV 재조회 없이 캐시만 사용 |
| `--refresh` | `analysis/cache/` 삭제 후 가격 재다운로드 |
| `--history PATH` | trade_history 경로 (기본: 루트 `trade_history.json`) |

## 산출물 (`analysis/output/`)

| 파일 | 내용 |
|------|------|
| `ANALYSIS_REPORT.md` | 요약·시대별·전략별·고도화 제안 |
| `round_trips.csv` | 매수→매도 쌍, 보유시간, 매도 후 5/10/20/60일 수익률 |
| `summary_by_era.csv` | 코드 변경 시대(`strategy_eras.json`)별 집계 |
| `summary_by_strategy.csv` | SWING / V8 / V6 등 |
| `summary_by_market.csv` | KR / US / COIN |
| `summary_by_sell_reason.csv` | 하드스탑 / Phase5 / 수동 등 |

## 시대(Era) 구분

`strategy_eras.json` — git 커밋 타임라인 기준 (V8, Phase3/4/5, market_cycles, 8/20 equity 서킷 등).

수정 후 분석을 다시 돌리면 era 집계가 갱신됩니다.

## 데이터 소스

- **입력:** `trade_history.json` (읽기만, 락/쓰기 없음)
- **가격:** pykrx(KR) → yfinance(KR/US) → pyupbit(COIN), `analysis/cache/` 에만 저장

KIS 토큰·봇 state 파일은 사용하지 않습니다.

## 해석 포인트

- **조기청산(early_exit):** 매도 후 20거래일 +5% 이상 추가 상승 → 익절/트레일이 빠를 수 있음
- **적절청산(good_exit):** 매도 후 20일 -5% 이하 → 손절·매도 타이밍 양호
- **Phase5 / 하드스탑** 건은 `sell_bucket`으로 따로 집계

## 주의

- 코인 OHLCV는 Upbit 마켓 코드에 따라 실패할 수 있음 → `post_exit_note=OHLCV 부족`
- 부분 매도·복수 매수는 FIFO로 lot 매칭
- 고아 매도(매수 기록 없음)는 `orphan`으로 집계
