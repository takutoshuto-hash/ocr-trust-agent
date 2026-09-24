"""複数の日次シミュレーション曲線を並べ、「何が効いたか」の表を出す。

使い方: python eval/compare_runs.py
  --runs "改善前=eval/out/curve_gemini_200x14_vertex.csv,改善後 v2=eval/out/curve_gemini_200x14_v2.csv,..."
  --proposals eval/out/curve_gemini_200x14_v2.csv.proposals.jsonl   # 発表用実行の提案ログ（内訳を数える）
出力:
  eval/out/compare_runs.md   日別の要確認率の表（Markdown）と、run ごとの要約
  eval/out/summary_v2.json   ダッシュボードの「検証結果」が読む要約（数字をテンプレートに直書きしないため）
集計の式は app/verification.py（ダッシュボードと共通）。
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from app.verification import DEFAULT_RUNS, SUMMARY_PATH, count_proposals, fnum, load_curve, load_summary, summarize_run


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=",".join(f"{name}={path}" for _, name, path in DEFAULT_RUNS))
    ap.add_argument("--out", default="eval/out/compare_runs.md")
    ap.add_argument("--proposals", default="", help="発表用実行の <out>.proposals.jsonl。指定すると提案の内訳を summary に書く")
    ap.add_argument("--summary", default=str(SUMMARY_PATH), help="ダッシュボード用の要約 JSON の出力先（既存の値は保ち、計算できるものだけ更新）")
    ap.add_argument("--no-summary", action="store_true")
    a = ap.parse_args()
    runs = []
    for item in a.runs.split(","):
        name, path = item.split("=", 1)
        runs.append((name.strip(), load_curve(path.strip())))
    days = max((len(r) for _, r in runs), default=0)

    lines = ["| 日 | " + " | ".join(n for n, _ in runs) + " |", "|---|" + "---|" * len(runs)]
    for d in range(days):
        cells = []
        for _, rows in runs:
            cells.append(f"{fnum(rows[d]['review_rate']) * 100:.1f}%" if d < len(rows) and fnum(rows[d]["review_rate"]) is not None else "–")
        lines.append(f"| {d + 1} | " + " | ".join(cells) + " |")

    summary = ["", "| 実行 | 日数 | 最終日の要確認率 | 後半（8日目以降）平均 | 人が見た割合（最終日） | 自動確定の実際の誤り率（正解と照合・合算） | 承認数/提案数 | 費用（記録分） |",
               "|---|---|---|---|---|---|---|---|"]
    for name, rows in runs:
        s = summarize_run(rows)
        if not rows:
            summary.append(f"| {name} | 0 | – | – | – | – | – | – |")
            continue
        seen, late = s["human_sees_rate_last"], s["review_rate_late_avg"]
        summary.append(f"| {name} | {s['days']} | {s['review_rate_last'] * 100:.1f}% | "
                       f"{(late * 100) if late is not None else float('nan'):.1f}% | "
                       f"{(seen * 100) if seen is not None else float('nan'):.1f}% | {s['true_error_rate'] * 100:.2f}% | "
                       f"{s['approved']}/{s['proposals']} | {('¥%.0f' % s['cost_jpy']) if s['cost_jpy'] else '–'} |")
    text = "\n".join(lines + summary)
    print(text)
    Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(f"\nwrote {a.out}")

    if a.no_summary:
        return
    sp = Path(a.summary)
    doc = load_summary(sp)
    doc["generated"] = date.today().isoformat()
    doc["runs"] = {name: summarize_run(rows) for name, rows in runs}
    if a.proposals:
        doc["proposals"] = {**count_proposals(a.proposals), "verified": True,
                            "note": "発表用実行の提案ログから数えた内訳（承認＝取り消し以外の承認、取り消し＝効果を測って引っ込めた提案、自動却下＝ガバナンスで却下）"}
    sp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {sp}")


if __name__ == "__main__":
    main()
