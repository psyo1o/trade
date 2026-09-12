# docs/ 목차 (문서 지도)

AI·사람이 **어디를 열지** 빠르게 고르기 위한 지도.  
세션 시작: [`../AGENTS.md`](../AGENTS.md) → [`CURRENT.md`](CURRENT.md) → [`CHANGELOG.md`](CHANGELOG.md).

## 운영·핸드오프

| 문서 | 내용 |
|------|------|
| [`../AGENTS.md`](../AGENTS.md) | AI 작업 지침·체크리스트·자주 만지는 파일 |
| [`CURRENT.md`](CURRENT.md) | **지금/다음/주의** (짧게) |
| [`CHANGELOG.md`](CHANGELOG.md) | 날짜별 수정 내역 (최신 위) |
| `../backups/YYYY/MM/` · `execution/state_backup.py` | 장부·매매내역 일자 아카이브 |

## 리스크·잔고·서킷

| 문서 | 내용 |
|------|------|
| [`PHASE5_ACCOUNT_CIRCUIT.md`](PHASE5_ACCOUNT_CIRCUIT.md) | Phase5 시장별 MDD·쿨다운·config |
| [`EQUITY_SNAPSHOT_CIRCUIT_PLAN.md`](EQUITY_SNAPSHOT_CIRCUIT_PLAN.md) | 스냅샷 오발동 원인·risk vs display |
| [`KIS_GUI_DISPLAY.md`](KIS_GUI_DISPLAY.md) | GUI·KIS 표시·강제 새로고침 |

## 전략·헷지

| 문서 | 내용 |
|------|------|
| [`HEDGE_UNIVERSE.md`](HEDGE_UNIVERSE.md) | 헷지 티커·BEAR/Phase4 현금 관망 정책 |
| [`../analysis/STRATEGY_REVIEW.md`](../analysis/STRATEGY_REVIEW.md) | 거래 복기·고도화 제안 |
| [`../analysis/README.md`](../analysis/README.md) | analysis 툴 사용법 |
| 루트 [`../README.md`](../README.md) §전략 | V8/스윙 상수·타임스탑·매수 게이트 |

## 구조·멱등

| 문서 | 내용 |
|------|------|
| [`MODULARIZATION.md`](MODULARIZATION.md) | market_cycles·모듈 분리 |
| [`idempotency/README.md`](idempotency/README.md) | 주문 멱등·장부 정합 목차 |

## 문서 갱신 규칙 (요약)

1. 코드 바꾸면 → `CHANGELOG.md` + `CURRENT.md`
2. 주제 로직이면 → 위 표의 해당 docs
3. 상수·게이트·안내 문구면 → 루트 `README.md` + (필요 시) `run_gui.py` 전략 안내
4. 상세는 `.cursor/rules/change-log-docs.mdc` / `agent-handoff.mdc`
