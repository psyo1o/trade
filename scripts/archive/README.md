# scripts/archive

B-1 시장 사이클 분리(2026-04~06) 때 쓴 **1회성 마이그레이션·점검 스크립트** 보관함.

운영·`run_bot`·`run_gui` 경로에서 **import 하지 않음**. 재실행하지 마세요.

| 파일 | 용도 |
|------|------|
| `_extract_market_cycles.py` | run_bot 블록 → cycle 파일 추출 |
| `_integrate_b1_run_bot.py` | 추출본 run_bot 병합 (완료) |
| `_prefix_cycle_rb.py` | cycle 파일 `rb.` 접두 |
| `_patch_cycle_imports.py` | import 정리 |
| `_fix_cycle_bare_refs.py` | bare 심볼 수정 |
| `_wire_run_trading_bot.py` | run_trading_bot 위임 배선 |
| `_smoke_market_cycles.py` | 수동 스모크 (대체: `tests/test_market_cycles_smoke.py`) |
| `_verify_market_cycles.py` | cycle ↔ run_bot 심볼 정적 스캔 |
| `_calc_kr_swing_exit.py` | 스윙 매도선 감사 |
| `_swing_holdings_timeline.py` | 보유 타임라인 감사 |
| `_swing_coin_audit.py` | 코인 스윙 감사 |

현재 사이클 코드: `execution/market_cycles/{kr,us,coin}_cycle.py`
