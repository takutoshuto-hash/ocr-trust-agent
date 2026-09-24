"""検証結果（運用シミュレーション）の集計。ダッシュボードと eval/compare_runs.py が同じ式を使う。

曲線 CSV（eval/simulate_days.py の出力）から、発表に使う数字を計算する:
  - 最終日の要確認率、後半（8日目以降）の平均
  - 自動確定の「実際の誤り率」= 正解と照合した auto_error_rate を自動確定数で重み付けして合算
  - 抜き取り確認で見つかった誤り率（最終日）
  - 提案数・承認数の合計
CSV に無い数字（提案の内訳、現場の実測時間）は eval/out/summary_v2.json から読む。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional

from app.config import ROOT

# 発表に使う実行。名前はダッシュボードと compare_runs.md の両方に出る
DEFAULT_RUNS: list[tuple[str, str, str]] = [
    ("baseline", "改善前（基準値）", "eval/out/curve_gemini_200x14_vertex.csv"),
    ("improved", "改善後 v2（全部入り）", "eval/out/curve_gemini_200x14_v2.csv"),
    ("noreflect", "v2 − 振り返りなし", "eval/out/curve_gemini_200x14_noreflect.csv"),
    ("pageread", "v2 − 2回目を全面読みに戻す", "eval/out/curve_gemini_200x14_pageread.csv"),
]
SUMMARY_PATH = ROOT / "eval/out/summary_v2.json"


def fnum(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_curve(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def curve_rows(rows: list[dict]) -> list[dict]:
    """ダッシュボードの折れ線用に数値化（day は整数、空欄・None は null）。"""
    out = []
    for r in rows:
        out.append({
            "day": int(fnum(r.get("day")) or 0),
            "forms": int(fnum(r.get("forms")) or 0),
            "fields": int(fnum(r.get("fields")) or 0),
            "review_rate": fnum(r.get("review_rate")),
            "human_sees_rate": fnum(r.get("human_sees_rate")),
            "auto_error_rate": fnum(r.get("auto_error_rate")),
            "auto_error_rate_audited": fnum(r.get("auto_error_rate_audited")),
            "proposals": int(fnum(r.get("proposals")) or 0),
            "approved": int(fnum(r.get("approved")) or 0),
        })
    return out


def summarize_run(rows: list[dict]) -> dict:
    """1 本の実行の要約。式は compare_runs.md と同じ。"""
    if not rows:
        return {"days": 0}
    last = rows[-1]
    late = [fnum(r["review_rate"]) for r in rows if int(fnum(r["day"]) or 0) >= 8 and fnum(r["review_rate"]) is not None]
    num = den = 0.0
    for r in rows:
        fields, rev = fnum(r.get("fields")) or 0, fnum(r.get("review_rate")) or 0
        auto = fields * (1 - rev)
        num += (fnum(r.get("auto_error_rate")) or 0) * auto
        den += auto
    audited_last = None
    for r in reversed(rows):
        if fnum(r.get("auto_error_rate_audited")) is not None:
            audited_last = fnum(r.get("auto_error_rate_audited"))
            break
    cost = sum(fnum(r.get("gemini_cost_jpy")) or 0 for r in rows)
    return {
        "days": len(rows),
        "forms": sum(int(fnum(r.get("forms")) or 0) for r in rows),
        "fields": sum(int(fnum(r.get("fields")) or 0) for r in rows),
        "review_rate_first": fnum(rows[0].get("review_rate")),
        "review_rate_last": fnum(last.get("review_rate")),
        "review_rate_late_avg": (sum(late) / len(late)) if late else None,
        "human_sees_rate_last": fnum(last.get("human_sees_rate")),
        "true_error_rate": (num / den) if den else 0.0,
        "audited_error_rate_last": audited_last,
        "auto_accepted": round(den),
        "proposals": sum(int(fnum(r.get("proposals")) or 0) for r in rows),
        "approved": sum(int(fnum(r.get("approved")) or 0) for r in rows),
        "cost_jpy": cost or None,
    }


def count_proposals(jsonl_path: str | Path) -> dict:
    """simulate_days の <out>.proposals.jsonl から提案の内訳を数える。

    approved      : 承認された提案（取り消し以外）
    retracted     : 効果を測って取り消した提案（kind=retract, 承認済み）
    auto_rejected : ガバナンスで自動却下（題名が「（自動却下）」で始まる）
    rejected      : それ以外の却下
    pending       : 未決
    by_kind_status: kind × status の全件数（突き合わせ用）
    """
    p = Path(jsonl_path)
    counts = {"total": 0, "approved": 0, "retracted": 0, "auto_rejected": 0, "rejected": 0, "pending": 0}
    by: dict[str, int] = {}
    if not p.exists():
        return {**counts, "by_kind_status": by, "source": None}
    with p.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            pr = json.loads(line)
            kind, status, title = str(pr.get("kind") or ""), str(pr.get("status") or ""), str(pr.get("title") or "")
            counts["total"] += 1
            by[f"{kind}/{status}"] = by.get(f"{kind}/{status}", 0) + 1
            if status == "approved":
                counts["retracted" if kind == "retract" else "approved"] += 1
            elif status == "rejected":
                counts["auto_rejected" if title.startswith("（自動却下）") else "rejected"] += 1
            else:
                counts["pending"] += 1
    return {**counts, "by_kind_status": by, "source": str(p)}


def measurement_after(path: Path = ROOT / "data/measurement/results_cond2_after.csv") -> dict:
    """本作での確認時間の実測（合成 20 枚を本番の確認画面で処理）。要確認があった帳票だけ時間が付く。"""
    from statistics import median
    rows = load_curve(path)
    if not rows:
        return {}
    secs = [fnum(r.get("review_seconds")) for r in rows if fnum(r.get("review_seconds")) is not None]
    return {
        "forms": len(rows),
        "timed_forms": len(secs),
        "no_review_forms": sum(1 for r in rows if int(fnum(r.get("review_fields")) or 0) == 0),
        "deliveries": sum(int(fnum(r.get("deliveries")) or 0) for r in rows),
        "seconds_per_form_median": round(median(secs), 1) if secs else None,
    }


def load_summary(path: Path = SUMMARY_PATH) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def verification_data(runs: list[tuple[str, str, str]] = DEFAULT_RUNS, summary_path: Path = SUMMARY_PATH) -> dict:
    """ダッシュボードの「検証結果」表示に必要なものを一括で返す。"""
    out_runs = []
    curves = {}
    for key, name, path in runs:
        rows = load_curve(path)
        out_runs.append({"key": key, "name": name, **summarize_run(rows)})
        if rows:
            curves[key] = curve_rows(rows)
    return {"runs": out_runs, "curves": curves, "summary": load_summary(summary_path), "measurement_after": measurement_after()}
