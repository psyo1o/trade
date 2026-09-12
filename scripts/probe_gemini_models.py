# -*- coding: utf-8 -*-
"""Gemini 모델 응답 프로브 — ``python scripts/probe_gemini_models.py [--full]``"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy.ai_filter import GEMINI_MODEL_DEFAULT, _get_secret

parser = argparse.ArgumentParser(description="Gemini generateContent 프로브")
parser.add_argument(
    "--full",
    action="store_true",
    help="모든 후보 모델 시도 (비용·한도 주의). 기본은 quick 2개만.",
)
parser.add_argument(
    "--no-list",
    action="store_true",
    help="ListModels API 생략",
)
args = parser.parse_args()

config = (
    json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    if (ROOT / "config.json").is_file()
    else {}
)
key = _get_secret("GOOGLE_API_KEY", config)
if not key:
    print("GOOGLE_API_KEY 없음")
    sys.exit(1)

if not args.no_list:
    list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
    try:
        lr = requests.get(list_url, timeout=30)
        if lr.status_code == 200:
            names = [
                m.get("name", "").replace("models/", "")
                for m in lr.json().get("models", [])
                if "generateContent" in (m.get("supportedGenerationMethods") or [])
            ]
            flash = [n for n in names if "flash" in n.lower()]
            print(f"ListModels OK - flash models: {len(flash)}")
            for n in flash[:10]:
                print(f"  - {n}")
        else:
            print(f"ListModels {lr.status_code}: {lr.text[:200]}")
    except Exception as e:
        print(f"ListModels EXC: {e}")

all_candidates = [
    GEMINI_MODEL_DEFAULT,
    "gemini-3.5-flash-lite",
    "gemini-flash-latest",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3-flash-preview",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]
candidates = all_candidates if args.full else all_candidates[:2]
if args.full:
    print("\n⚠️ --full: 후보 전수 프로브 — 운영 키로 자주 돌리지 마세요.")
else:
    print(f"\nquick 모드: {len(candidates)}개만 시도 (--full 로 전체)")

prompt = 'Respond JSON only: {"liquidation_score": 50, "rationale": "test"}'
print("\n=== generateContent probe ===")
working: list[str] = []
for mdl in candidates:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key={key}"
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1}}
    try:
        r = requests.post(url, json=body, timeout=30)
        if r.status_code == 200:
            data = r.json()
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])
            text = str(parts[0].get("text", "") if parts else "")[:100]
            print(f"OK  {mdl}: {text.replace(chr(10), ' ')}")
            working.append(mdl)
        else:
            err = (
                r.json().get("error", {}).get("message", r.text[:120])
                if r.headers.get("content-type", "").startswith("application/json")
                else r.text[:120]
            )
            print(f"FAIL {mdl}: {r.status_code} {err}")
            if r.status_code == 429:
                print("429 — 나머지 모델 프로브 중단")
                break
    except Exception as e:
        print(f"EXC  {mdl}: {type(e).__name__}: {e}")

print("\nworking:", working)
if working:
    print("recommended_default:", working[0])
    sys.exit(0)
sys.exit(2)
