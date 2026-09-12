# -*- coding: utf-8 -*-
"""집계·CSV·마크다운 리포트."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .load_trades import RoundTrip


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _summarize_group(rows: list[RoundTrip], key_fn) -> list[dict[str, Any]]:
    buckets: dict[str, list[RoundTrip]] = defaultdict(list)
    for r in rows:
        buckets[str(key_fn(r))].append(r)
    out: list[dict[str, Any]] = []
    for k, grp in sorted(buckets.items(), key=lambda x: (-len(x[1]), x[0])):
        prs = [float(r.profit_rate) for r in grp if r.profit_rate is not None]
        wins = sum(1 for p in prs if p > 0)
        early = sum(1 for r in grp if r.post_exit.get("post_exit_verdict") == "early_exit")
        good = sum(1 for r in grp if r.post_exit.get("post_exit_verdict") == "good_exit")
        pe_ok = sum(1 for r in grp if r.post_exit.get("post_exit_ok"))
        ret20 = [
            float(r.post_exit["ret_20d"])
            for r in grp
            if r.post_exit.get("ret_20d") is not None
        ]
        med20 = _median(ret20)
        out.append(
            {
                "group": k,
                "trades": len(grp),
                "win_rate_pct": round(wins / len(prs) * 100, 1) if prs else None,
                "avg_profit_pct": round(sum(prs) / len(prs), 2) if prs else None,
                "median_profit_pct": round(_median(prs), 2) if _median(prs) is not None else None,
                "avg_hold_hours": round(sum(r.hold_hours for r in grp) / len(grp), 1),
                "post_exit_n": pe_ok,
                "early_exit_pct": round(early / pe_ok * 100, 1) if pe_ok else None,
                "good_exit_pct": round(good / pe_ok * 100, 1) if pe_ok else None,
                "median_ret_20d_after_sell": round(med20, 2) if med20 is not None else None,
                "avg_ret_20d_after_sell": round(sum(ret20) / len(ret20), 2) if ret20 else None,
            }
        )
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for k in row:
            if k not in fields:
                fields.append(k)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def build_markdown_report(
    trips: list[RoundTrip],
    orphans: list[dict[str, Any]],
    *,
    eras_path: Path,
    generated_at: str | None = None,
) -> str:
    ts = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    closed = [t for t in trips if not t.open_position]
    prs = [float(t.profit_rate) for t in closed if t.profit_rate is not None]
    wins = sum(1 for p in prs if p > 0)
    losses = sum(1 for p in prs if p < 0)

    by_era = _summarize_group(closed, lambda r: r.buy_era)
    by_strat = _summarize_group(closed, lambda r: r.strategy)
    by_market = _summarize_group(closed, lambda r: r.market)
    by_sell = _summarize_group(closed, lambda r: r.sell_bucket)

    pe_ok = [t for t in closed if t.post_exit.get("post_exit_ok")]
    early = sum(1 for t in pe_ok if t.post_exit.get("post_exit_verdict") == "early_exit")
    good = sum(1 for t in pe_ok if t.post_exit.get("post_exit_verdict") == "good_exit")

    lines: list[str] = [
        "# 매매 기록 분석 리포트",
        "",
        f"생성: {ts}",
        "",
        "## 1. 전체 요약",
        "",
        f"| 항목 | 값 |",
        f"|------|-----|",
        f"| 완결 라운드트립 | {len(closed)}건 |",
        f"| 승 / 패 | {wins} / {losses} |",
        f"| 승률 | {wins/len(prs)*100:.1f}% |" if prs else "| 승률 | — |",
        f"| 평균 수익률 | {sum(prs)/len(prs):+.2f}% |" if prs else "| 평균 수익률 | — |",
        f"| 중앙 수익률 | {_median(prs):+.2f}% |" if _median(prs) is not None else "| 중앙 수익률 | — |",
        f"| 매도 후 분석 가능 | {len(pe_ok)}건 |",
        f"| 조기청산(20d+5%) | {early}건 ({early/len(pe_ok)*100:.1f}%) |" if pe_ok else "",
        f"| 적절청산(20d-5%) | {good}건 ({good/len(pe_ok)*100:.1f}%) |" if pe_ok else "",
        f"| 고아 매도(매수 미매칭) | {len(orphans)}건 |",
        "",
        "## 2. 시대(Era)별 — 코드 변경 시점 반영",
        "",
        f"기준: `{eras_path.name}`",
        "",
        _table(by_era),
        "",
        "## 3. 전략별",
        "",
        _table(by_strat),
        "",
        "## 4. 시장별",
        "",
        _table(by_market),
        "",
        "## 5. 매도 사유별",
        "",
        _table(by_sell),
        "",
        "## 6. 해석 · 고도화 제안",
        "",
    ]

    insights = _generate_insights(closed, by_era, by_strat, by_sell, pe_ok)
    lines.extend(insights)
    lines.append("")
    lines.append("## 7. 산출물")
    lines.append("")
    lines.append("- `round_trips.csv` — 건별 보유·수익·매도후 수익률")
    lines.append("- `summary_by_era.csv` / `summary_by_strategy.csv` / `summary_by_market.csv`")
    lines.append("- `summary_by_sell_reason.csv`")
    lines.append("")
    return "\n".join(line for line in lines if line is not None)


def _table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(데이터 없음)"
    cols = [
        "group",
        "trades",
        "win_rate_pct",
        "avg_profit_pct",
        "median_profit_pct",
        "avg_hold_hours",
        "early_exit_pct",
        "good_exit_pct",
        "median_ret_20d_after_sell",
        "avg_ret_20d_after_sell",
    ]
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join(["---"] * len(cols)) + " |"
    body = []
    for r in rows:
        body.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    return "\n".join([header, sep, *body])


def _generate_insights(
    closed: list[RoundTrip],
    by_era: list[dict],
    by_strat: list[dict],
    by_sell: list[dict],
    pe_ok: list[RoundTrip],
) -> list[str]:
    lines: list[str] = []

    # Era trend
    eras_with_trades = [e for e in by_era if e["trades"] >= 3]
    if eras_with_trades:
        best = max(eras_with_trades, key=lambda e: e.get("avg_profit_pct") or -999)
        worst = min(eras_with_trades, key=lambda e: e.get("avg_profit_pct") or 999)
        lines.append(
            f"- **시대별:** 평균 수익 최고 `{best['group']}` ({best.get('avg_profit_pct')}%), "
            f"최저 `{worst['group']}` ({worst.get('avg_profit_pct')}%). "
            f"6월 이후 스윙 BEAR 차단·market_cycles 분리 이후 성과 변화를 era 표와 대조하세요."
        )

    # Strategy
    swing = next((s for s in by_strat if "SWING" in s["group"]), None)
    v8 = next((s for s in by_strat if "V8" in s["group"] or "TREND" in s["group"]), None)
    if swing and v8:
        lines.append(
            f"- **스윙 vs V8:** SWING_FIB 평균 {swing.get('avg_profit_pct')}% (n={swing['trades']}), "
            f"V8 계열 {v8.get('avg_profit_pct')}% (n={v8['trades']}). "
            f"조기청산 비율 SWING {swing.get('early_exit_pct')}% vs V8 {v8.get('early_exit_pct')}%."
        )

    # Sell reason
    hard = next((s for s in by_sell if s["group"] == "hard_stop"), None)
    phase5 = next((s for s in by_sell if s["group"] == "phase5_circuit"), None)
    if hard:
        lines.append(
            f"- **하드스탑:** {hard['trades']}건, 평균 {hard.get('avg_profit_pct')}%. "
            f"매도 후 20d 평균 {hard.get('avg_ret_20d_after_sell')}% — "
            f"{'손절이 대체로 맞았음' if (hard.get('avg_ret_20d_after_sell') or 0) < 0 else '일부는 바닥에서 잘렸을 가능성'}."
        )
    if phase5:
        lines.append(
            f"- **Phase5 서킷:** {phase5['trades']}건 — 인프라 오류(8/20 미장)와 실제 MDD를 구분해 보세요."
        )

    # Post-exit overall
    if pe_ok:
        ret20_vals = [float(t.post_exit.get("ret_20d", 0) or 0) for t in pe_ok]
        med20 = _median(ret20_vals)
        lines.append(
            f"- **매도 타이밍:** 매도 후 20일 **중앙** 주가 변화 {med20:+.2f}% "
            f"(n={len(pe_ok)}). 양수면 '일찍 팔았다' 비중, 음수면 손절·청산이 대체로 맞았음."
        )

    lines.extend(
        [
            "",
            "### 고도화 방향 (데이터 기반 초안)",
            "",
            "1. **조기청산 비율이 높은 전략/시대** — 스윙 시간가중 하드·V8 매도선을 era별로 A/B (hold longer on winners).",
            "2. **하드스탑 후 반등 비율** — KR/US별 stop 폭(ATR 배수) 재조정; BEAR 구간 BEAR 차단(6/12~) 효과는 era 표로 검증.",
            "3. **Phase5·MDD** — 서킷 매도 건의 post_exit가 'good_exit'이면 방어 성공, 'early_exit'면 고점·입력 오류 의심.",
            "4. **코인 vs 주식** — 보유 시간·조기청산 패턴 분리; 코인은 V8_TIME_STOP 유예 효과 별도 집계.",
            "5. **Phase3 AI 필터** — 4/29~ era 매수 건만 필터링해 AI pass/fail과 수익 상관 (trade_history reason 확장 필요).",
            "",
            "> 이 리포트는 `analysis/run_analysis.py`로 재생성합니다. 봇 프로세스와 독립입니다.",
        ]
    )
    return lines


def export_all(
    output_dir: Path,
    trips: list[RoundTrip],
    orphans: list[dict[str, Any]],
    eras_path: Path,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    closed = [t for t in trips if not t.open_position]
    paths: dict[str, Path] = {}

    rt = [t.to_row() for t in trips]
    p = output_dir / "round_trips.csv"
    write_csv(p, rt)
    paths["round_trips"] = p

    for name, key_fn in (
        ("summary_by_era", lambda r: r.buy_era),
        ("summary_by_strategy", lambda r: r.strategy),
        ("summary_by_market", lambda r: r.market),
        ("summary_by_sell_reason", lambda r: r.sell_bucket),
    ):
        sp = output_dir / f"{name}.csv"
        write_csv(sp, _summarize_group(closed, key_fn))
        paths[name] = sp

    md = build_markdown_report(trips, orphans, eras_path=eras_path)
    mp = output_dir / "ANALYSIS_REPORT.md"
    mp.write_text(md, encoding="utf-8")
    paths["report"] = mp

    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "round_trips": len(trips),
        "orphan_sells": len(orphans),
    }
    jp = output_dir / "meta.json"
    jp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["meta"] = jp
    return paths
