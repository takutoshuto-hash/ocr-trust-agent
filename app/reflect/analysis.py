"""振り返りの材料を決定的に集計する。LLM はこの集計だけを見て提案を書く（生の個人情報は渡さない）。

集計内容:
  - 項目種別ごとの件数・修正率（人が見た項目のみ）
  - 混同しやすい文字の対（抽出値→確定値の文字置換を数える。例 "1"→"7"）
  - 様式・送り主ごとの修正率（上位のみ、ID は匿名のまま）
  - 自動確定の見逃し（監査サンプルで人が直したもの）
  - 修復エージェントの行動別の採用数
  - ハルシネーション（空欄検知 FAIL）の件数
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.schemas import AuditEvent, TrainingRecord


def analyze(records: list[TrainingRecord], audits: list[AuditEvent], *, since: Optional[datetime] = None,
            top: int = 8) -> dict:
    if since:
        records = [r for r in records if r.created_at >= since]
        audits = [a for a in audits if a.created_at >= since]
    seen = [r for r in records if r.verified]

    by_ft: dict[str, dict] = defaultdict(lambda: {"n": 0, "corrected": 0})
    by_fmt: dict[str, dict] = defaultdict(lambda: {"n": 0, "corrected": 0})
    by_sender: dict[str, dict] = defaultdict(lambda: {"n": 0, "corrected": 0})
    processed: Counter = Counter(r.field_type for r in records)   # 人が見たかに関係なく処理した件数（効果測定の分母）
    confusions: Counter = Counter()
    variant_corr: Counter = Counter()
    length_diff: Counter = Counter()
    for r in seen:
        for bucket, key in ((by_ft, r.field_type), (by_fmt, r.format_id), (by_sender, r.sender_id)):
            bucket[key]["n"] += 1
            bucket[key]["corrected"] += int(r.corrected)
        if r.corrected and isinstance(r.extracted, str) and isinstance(r.final, str):
            for a, b in _char_substitutions(r.extracted, r.final):
                if _fold(a) == _fold(b):
                    variant_corr[r.field_type] += 1     # 異体字（髙↔高）はルールで直せない。根拠復元と顧客照合で扱う
                    continue
                confusions[(r.field_type, a, b)] += 1
            length_diff["missing" if len(r.final) > len(r.extracted) else "extra" if len(r.final) < len(r.extracted) else "same_len"] += 1

    auto_missed = [r for r in seen if r.was_auto and r.corrected]
    actions: Counter = Counter()
    accepted: Counter = Counter()
    for a in audits:
        if a.event == "resolved":
            for act in a.detail.get("actions", []):
                actions[act.get("action", "?")] += 1
                if act.get("accepted"):
                    accepted[act.get("action", "?")] += 1
    hallucination_flags = sum(1 for a in audits if a.event == "judged" for _ in [0])  # placeholder count of judged forms
    blank_fails = sum(1 for a in audits if a.event == "decided" and any("ハルシネーション" in str(r) for r in a.detail.get("review", [])))

    def rate_table(bucket: dict, min_n: int = 5, with_processed: bool = False) -> list[dict]:
        rows = []
        for k, v in bucket.items():
            if v["n"] < min_n:
                continue
            row = {"key": k, "n": v["n"], "corrected": v["corrected"], "rate": round(v["corrected"] / v["n"], 4)}
            if with_processed:
                # rate は「人が見た件数」あたり（現場の体感）。rate_processed は「処理した件数」あたりで、
                # 判定が慎重になって人に回る件数が増えても薄まらない（読み取りルールの効果測定はこちらを使う）
                row["n_processed"] = int(processed.get(k, v["n"]))
                row["rate_processed"] = round(v["corrected"] / max(1, row["n_processed"]), 4)
            rows.append(row)
        return sorted(rows, key=lambda x: -x["rate"])[:top]

    return {
        "since": since,
        "window_records": len(seen),
        "overall_correction_rate": round(sum(r.corrected for r in seen) / len(seen), 4) if seen else None,
        "by_field_type": rate_table(by_ft, 1, with_processed=True),
        "by_format": rate_table(by_fmt),
        "by_sender_top": rate_table(by_sender),
        "confusions_top": [{"field_type": ft, "from": a, "to": b, "count": c} for (ft, a, b), c in confusions.most_common(top)],
        "length_diff": dict(length_diff),
        "variant_kanji_corrections": dict(variant_corr),
        "auto_missed": {"count": len(auto_missed), "field_types": dict(Counter(r.field_type for r in auto_missed))},
        "resolver_actions": {k: {"tried": actions[k], "accepted": accepted[k]} for k in actions},
        "forms_judged": hallucination_flags,
        "blank_zone_fails_in_review": blank_fails,
    }


def _char_substitutions(a: str, b: str) -> list[tuple[str, str]]:
    """同じ長さなら位置ごとの置換、長さが違えば最初の差分1文字だけを返す（粗いが説明可能）。"""
    a, b = a.replace(" ", ""), b.replace(" ", "")
    if len(a) == len(b):
        return [(x, y) for x, y in zip(a, b) if x != y][:3]
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return [(x, y)]
    return []


def default_since(days: int = 1) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _fold(ch: str) -> str:
    from app.judge.tools import VARIANT_KANJI
    return VARIANT_KANJI.get(ch, ch)
