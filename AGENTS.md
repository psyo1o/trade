# AI·에이전트 작업 지침 (이 저장소)

다른 PC·다른 AI(Cursor 등)에서 **이 파일을 먼저** 읽고 시작한다.

## 30초 시작 순서

1. [`docs/CURRENT.md`](docs/CURRENT.md) — **지금 상태·열린 이슈·다음에 할 일** (짧게 유지)
2. [`docs/CHANGELOG.md`](docs/CHANGELOG.md) — **날짜별 수정 내역** (최신이 위)
3. 주제 문서 — [`docs/README.md`](docs/README.md) 목차에서 해당 항목만 연다
4. 코드 — 아래 「자주 만지는 파일」만 건드린다

커밋/PR 문구는 한국어 (`.cursor/rules/git-commit-korean.mdc`).

## 문서 역할 분리

| 파일 | 역할 | 언제 고치나 |
|------|------|-------------|
| `docs/CURRENT.md` | 현재 포커스·미커밋·주의·다음 작업 | **매 작업 종료 시** 2~5줄 갱신 |
| `docs/CHANGELOG.md` | 날짜별 변경 이력 (상세) | **매 기능/픽스 후** 맨 위에 항목 |
| `docs/README.md` | 주제별 문서 지도 | 문서 추가·이동 시 |
| 주제 docs (`PHASE5_*.md` 등) | 설계·동작 상세 | 해당 로직 변경 시 |
| `README.md` | 운영 매뉴얼·상수표 | 상수·게이트·GUI 설명 변경 시 |
| `run_gui.py` `_build_strategy_guide_text` | GUI 매매·전략 안내 | 전략/게이트 정책이 바뀔 때 **코드와 같이** |

## 작업 종료 체크리스트 (필수)

- [ ] `docs/CHANGELOG.md` 맨 위에 `YYYY-MM-DD — 제목` 추가
- [ ] `docs/CURRENT.md`의 「지금」「다음」「주의」 갱신
- [ ] 관련 주제 docs / README 상수표 / GUI 안내 문구가 코드와 같은가
- [ ] 가능하면 `pytest` (건드린 테스트만)
- [ ] **LLM·Gemini/OpenAI 코드 변경 시** `python scripts/smoke_phase5_ai.py` 라이브 스모크 (`.cursor/rules/llm-live-smoke.mdc`)

## LLM·유료 API (배포 전 필수)

| 단계 | 명령 |
|------|------|
| 단위 | `pytest tests/test_phase5_ai_liquidation.py tests/test_gemini_models.py -q` |
| 라이브 | `python scripts/smoke_phase5_ai.py` — 매수·Phase5 `llm_success` 확인 |
| 프로브 | `python scripts/probe_gemini_models.py` (**`--quick` 기본**, `--full`은 드물게) |

운영 키로 모델 후보 전수 프로브·Phase5 루프 재시도는 **API 낭비** — 금지.

## CHANGELOG 한 줄 형식

```markdown
## YYYY-MM-DD — 짧은 제목

- **무엇을:** …
- **왜:** …
- **주요 파일:** `a.py`, `b.py`
- **이어서 할 일 / 주의:** …
- **테스트:** `pytest …`
```

## 자주 만지는 파일

| 목적 | 경로 |
|------|------|
| 시장 벤치마크(매수·Phase5) | `strategy/market_benchmark.py` |
| V8·스윙 시그널·청산·상수 | `strategy/rules.py` |
| 헷지 티커 | `strategy/hedge_universe.py` |
| 매수 루프 KR/US/COIN | `execution/market_cycles/*_buy_cycle.py` |
| Phase5·서킷 | `execution/phase5_ops.py`, `execution/circuit_break.py`, `execution/guard.py` |
| 거시·VKOSPI | `api/macro_data.py`, `run_bot.py` (`_apply_vkospi_*`) |
| 잔고·스냅샷 | `services/ledger_valuation.py` |
| 봇 진입·헬퍼 | `run_bot.py` |
| GUI | `run_gui.py` |
| 거래 복기(봇과 분리) | `analysis/` |

## 하지 말 것

- `config.json` / 토큰 / `bot_state*.json` / `*.bak` 을 문서·커밋에 넣지 말 것
- 사용자가 묻지 않은 대규모 리팩터·관련 없는 파일 수정
- BEAR/Phase4에서 헷지 예외 매수를 **다시 넣지 말 것** (정책: 현금 관망) — 상세는 `docs/HEDGE_UNIVERSE.md`

## Cursor 규칙

- `.cursor/rules/change-log-docs.mdc` — 문서 반영 필수 (alwaysApply)
- `.cursor/rules/git-commit-korean.mdc` — 커밋·PR 한국어
- `.cursor/rules/agent-handoff.mdc` — 세션 시작 시 CURRENT/CHANGELOG 우선
