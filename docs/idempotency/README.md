# 주문 멱등성(Idempotency) 작업 공간

이 폴더는 **API 재시도·15분 사이클·TWAP** 때문에 생기는 **중복 주문**을 막기 위한 설계·진행 기록입니다.

- **구현 코드:** `execution/idempotency.py`
- **장부 키:** `bot_state.json` → `order_idempotency`, `buy_inflight`, `sell_inflight`
- **엔진 연동:** `run_bot.py` (매수·매도 TWAP, 스윙 HALF/FULL, Scale-Out, 전량 청산)

## 문서

| 파일 | 내용 |
|------|------|
| [PROGRESS.md](./PROGRESS.md) | 단계별 체크리스트·상태 |
| [SELL_LANES.md](./SELL_LANES.md) | 매도 경로(lane)별 멱등 키·기존 방어와 비교 |
| [ANALYSIS.md](./ANALYSIS.md) | 최초 멱등 분석 요약(매수·매도 위험 구간) |
| [BALANCE_READS.md](./BALANCE_READS.md) | 잔고 조회 TTL·스냅샷 (주문 멱등과 분리) |
| [../KIS_GUI_DISPLAY.md](../KIS_GUI_DISPLAY.md) | GUI 국·미 라벨 · KIS 강제 새로고침 · 로그 |
| [SMOKE_TEST.md](./SMOKE_TEST.md) | 실계좌 스모크 체크리스트 |
| [LEDGER_RECONCILE.md](./LEDGER_RECONCILE.md) | positions ↔ 멱등 정합·저장 재시도 |

## lane(매도 의도) 규칙

동일 **15분 `cycle_tag`** · **티커** · **lane** · **슬라이스 인덱스** 조합은 **한 번만** 실주문합니다.

- `swing_half` / `swing_full` — 스윙 분할·전량 (`sell_inflight` + order_key)
- `scale_out` — V8 50% 익절 TWAP (슬라이스별 order_key만)
- `exit` — 타임스탑·하드스탑·샹들리에·스윙 트레일 전량 (`sell_inflight` + order_key)
- `manual` / `phase5` — GUI·서킷 수동 청산

## 검증

```bash
py -3.11 -m unittest discover -s tests -p "test_idempotency.py" -v
py -3.11 -m unittest discover -s tests -p "test_balance_read.py" -v
```
