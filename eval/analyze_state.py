"""シミュレーションの書き出し状態（training.jsonl）から、日別・項目種別の修正内訳を集計する。

training.jsonl には「人が見た項目」（要確認＋監査サンプル）の確定記録が入る。
自動確定で監査に当たらなかった項目の真偽は含まれない（--field-log を使った実行なら fieldlog CSV で補う）。

使い方: python eval/analyze_state.py --state eval/out/state_gemini14d [--field-log eval/out/fieldlog.csv]
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--field-log", default=None)
    ap.add_argument("--days", type=int, default=14)
    a = ap.parse_args()
    d = Path(a.state)
    meta = json.loads((d / "META.json").read_text(encoding="utf-8"))
    print("META:", {k: meta[k] for k in ("days", "per_day", "final_review_rate", "final_auto_error_rate")})

    recs = [json.loads(l) for l in (d / "training.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    # 書き出し時に created_at を「今 - (days - day + 1)」に付け替えている → 日を復元
    dates = sorted({datetime.fromisoformat(r["created_at"]).date() for r in recs})
    day_of = {dt: i + 1 for i, dt in enumerate(dates)}
    by_day = defaultdict(lambda: {"seen": 0, "corrected": 0, "audit": 0, "audit_corrected": 0})
    by_day_ft = defaultdict(Counter)
    audit_err_ft = Counter()
    for r in recs:
        day = day_of[datetime.fromisoformat(r["created_at"]).date()]
        b = by_day[day]; b["seen"] += 1; b["corrected"] += int(r["corrected"])
        if r.get("was_auto"):
            b["audit"] += 1; b["audit_corrected"] += int(r["corrected"])
            if r["corrected"]:
                audit_err_ft[r["field_type"]] += 1
        if r["corrected"]:
            by_day_ft[day][r["field_type"]] += 1

    print("\n日別: 人が見た項目 / 修正 / 監査サンプル / 監査で見つかった自動確定の誤り")
    for day in sorted(by_day):
        b = by_day[day]
        print(f"  day {day:2d}: seen {b['seen']:5d}  corrected {b['corrected']:4d} ({b['corrected']/b['seen']:.3f})  "
              f"audit {b['audit']:4d}  audit_err {b['audit_corrected']:3d} ({(b['audit_corrected']/b['audit'] if b['audit'] else 0):.4f})")
    print("\n監査サンプルで見つかった自動確定の誤り（項目種別、全期間）:", dict(audit_err_ft.most_common()))
    for day in (8, 9, 10):
        if day in by_day_ft:
            print(f"\nday {day} の修正（項目種別 上位）:", dict(by_day_ft[day].most_common(8)))

    if a.field_log and Path(a.field_log).exists():
        rows = list(csv.DictReader(open(a.field_log, encoding="utf-8")))
        print("\n[field log] 日別: 自動確定の真の誤り（項目種別）")
        for day in sorted({int(r["day"]) for r in rows}):
            auto = [r for r in rows if int(r["day"]) == day and r["status"] == "auto"]
            wrong = [r for r in auto if r["correct"] == "0"]
            ft = Counter(r["field_type"] for r in wrong)
            thr = next((r["router_threshold"] for r in auto if r["router_threshold"]), "")
            print(f"  day {day:2d}: auto {len(auto):5d} wrong {len(wrong):3d} ({(len(wrong)/len(auto) if auto else 0):.4f}) thr={thr}  {dict(ft.most_common(5))}")


if __name__ == "__main__":
    main()
