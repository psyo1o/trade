# 멱등 적용 진행 상황

**최종 갱신:** 2026-05-28

## 범례

- ✅ 완료
- 🔄 진행 중
- ⬜ 예정

---

## 0. 기반

| # | 항목 | 상태 |
|---|------|------|
| 0.1 | `docs/idempotency/` | ✅ |
| 0.2 | `execution/idempotency.py` | ✅ |
| 0.3 | `execution/balance_read.py` | ✅ |
| 0.4 | `guard.py` sell_inflight | ✅ |
| 0.5 | 사이클 prune + `bal_read.invalidate()` | ✅ |

---

## 1. 매수

| # | 항목 | 상태 |
|---|------|------|
| 1.1 | KR / US / COIN TWAP 멱등 | ✅ |
| 1.2 | 스캔 `buy_inflight` | ✅ |
| 1.3 | 장부 BUY 120초 멱등 | ✅ |
| 1.4 | TWAP 잔고 `balance_read` | ✅ |
| 1.5 | 슬라이스 실패 `persist_idempotency` | ✅ |

---

## 2. 매도

| # | 항목 | 상태 |
|---|------|------|
| 2.1 | sell lane + inflight | ✅ |
| 2.2 | KR / US / COIN 루프 | ✅ |
| 2.3 | manual / phase5 | ✅ |
| 2.4 | 잔고 refresh + persist 실패 | ✅ |
| 2.5 | `SMOKE_TEST.md` | ✅ |
| 2.6 | 실계좌 스모크 실행 | ⬜ 운영자 |
| 2.7 | `LEDGER_RECONCILE.md` + `ledger_apply` | ✅ |
| 2.8 | 사이클 `reconcile_positions_for_cycle` | ✅ |
| 2.9 | GUI `merge_disk_if_newer` + `state_gen` | ✅ |

---

## 3. 잔고 조회 (별도 설계)

| # | 항목 | 상태 |
|---|------|------|
| 3.1 | `BALANCE_READS.md` | ✅ |
| 3.2 | TTL 캐시 12초 | ✅ |
| 3.3 | `tests/test_balance_read.py` | ✅ |

**요약:** 조회 API는 원래 멱등. 복잡함은 **체결 검증용 before/after** — `refresh=False` 시작, 주문 후 `refresh=True` 1회.

---

## 4. 선택 후보 (적용 완료)

| # | 항목 | 상태 |
|---|------|------|
| 4.1 | GUI·heartbeat 잔고 → `balance_read` | ✅ |
| 4.2 | `manual_sell` 장부 → `ledger_apply` | ✅ |

- GUI `refresh_balance`: `fresh_balances=True` (기존과 동일하게 API 재조회)
- `heartbeat_report`: TTL 공유 (`fresh_balances=False`, 12초 이내 봇과 스냅샷 공유)
