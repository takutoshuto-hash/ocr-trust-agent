"""複数の日次シミュレーション曲線を並べ、「何が効いたか」の表を出す。

使い方: python eval/compare_runs.py
  --runs "改善前=eval/out/curve_gemini_200x14_vertex.csv,改善後 v2=eval/out/curve_gemini_200x14_v2.csv,..."
出力: 日別の要確認率の表（Markdown）と、run ごとの要約（最終日・後半平均・自動確定の真の誤り率の合算）。
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

DEFAULT_RUNS = ",".join([
    "改善前（基準値）=eval/out/curve_gemini_200x14_vertex.csv",
    "改善後 v2（全部入り）=eval/out/curve_gemini_200x14_v2.csv",
    "v2 − 振り返りなし=eval/out/curve_gemini_200x14_noreflect.csv",
    "v2 − 2回目を全面読みに戻す=eval/out/curve_gemini_200x14_pageread.csv",
])


def load(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=DEFAULT_RUNS)
    ap.add_argument("--out", default="eval/out/compare_runs.md")
    a = ap.parse_args()
    runs = []
    for item in a.runs.split(","):
        name, path = item.split("=", 1)
        runs.append((name.strip(), load(path.strip())))
    days = max((len(r) for _, r in runs), default=0)

    lines = ["| 日 | " + " | ".join(n for n, _ in runs) + " |", "|---|" + "---|" * len(runs)]
    for d in range(days):
        cells = []
        for _, rows in runs:
            cells.append(f"{fnum(rows[d]['review_rate']) * 100:.1f}%" if d < len(rows) and fnum(rows[d]["review_rate"]) is not None else "–")
        lines.append(f"| {d + 1} | " + " | ".join(cells) + " |")

    summary = ["", "| 実行 | 日数 | 最終日の要確認率 | 後半（8日目以降）平均 | 人が見た割合（最終日） | 自動確定の真の誤り率（合算） | 提案（承認/取消） | 費用（記録分） |", "|---|---|---|---|---|---|---|---|"]
    for name, rows in runs:
        if not rows:
            summary.append(f"| {name} | 0 | – | – | – | – | – | – |")
            continue
        last = rows[-1]
        late = [fnum(r["review_rate"]) for r in rows if int(r["day"]) >= 8 and fnum(r["review_rate"]) is not None]
        # 真の誤り率の合算: auto_error_rate × 自動確定数（fields − review）
        num = den = 0.0
        for r in rows:
            fields, rev = fnum(r["fields"]) or 0, fnum(r["review_rate"]) or 0
            auto = fields * (1 - rev)
            num += (fnum(r["auto_error_rate"]) or 0) * auto
            den += auto
        seen = fnum(last.get("human_sees_rate"))
        approved = sum(int(fnum(r.get("approved")) or 0) for r in rows)
        proposals = sum(int(fnum(r.get("proposals")) or 0) for r in rows)
        cost = sum(fnum(r.get("gemini_cost_jpy")) or 0 for r in rows)
        summary.append(f"| {name} | {len(rows)} | {fnum(last['review_rate']) * 100:.1f}% | "
                       f"{(sum(late) / len(late) * 100) if late else float('nan'):.1f}% | "
                       f"{(seen * 100) if seen is not None else float('nan'):.1f}% | {(num / den * 100) if den else 0:.2f}% | "
                       f"{approved}/{proposals} | {('¥%.0f' % cost) if cost else '–'} |")
    text = "\n".join(lines + summary)
    print(text)
    Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
