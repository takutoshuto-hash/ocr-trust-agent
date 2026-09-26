"""修正予測分類器の特徴量。

「この項目を人が直すか」を予測するための入力。送り主に依存しない特徴を中心に、
台帳の統計（3階層）を加える。学習時も推論時も同じ関数で作る（学習/推論のズレ防止）。
"""
from __future__ import annotations

from app.schemas import CheckStatus, FieldValue, FieldVerdict
from app.trust.ledger import LedgerStat

FIELD_TYPES = [
    "applicant.name", "applicant.name_kana", "applicant.zip", "applicant.address", "applicant.phone",
    "applicant.organization", "deliveries.name", "deliveries.name_kana", "deliveries.zip",
    "deliveries.address", "deliveries.phone", "deliveries.product_code", "deliveries.qty", "deliveries.noshi_name",
]

FEATURE_NAMES = [
    "self_confidence", "agreement", "agreement_known",
    "checks_fail", "checks_unknown", "checks_pass", "value_len", "value_empty", "has_digits",
    "history_match",     # 1=過去の確定値と一致 / 0=履歴なし / -1=履歴はあるが不一致
    "sender_n", "sender_rate", "format_n", "format_rate", "global_n", "global_rate",
] + [f"ft_{ft}" for ft in FIELD_TYPES]


def build_features(fv: FieldValue, verdict: FieldVerdict, stats: dict[str, LedgerStat]) -> dict[str, float]:
    """stats: {"sender": LedgerStat, "format": LedgerStat, "global": LedgerStat}"""
    val = str(fv.value if fv.value is not None else "")
    f: dict[str, float] = {
        "self_confidence": float(fv.confidence or 0.0),
        "agreement": 1.0 if verdict.agreement else 0.0,
        "agreement_known": 0.0 if verdict.agreement is None else 1.0,
        "checks_fail": float(sum(c.status == CheckStatus.FAIL for c in verdict.checks)),
        "checks_unknown": float(sum(c.status == CheckStatus.UNKNOWN for c in verdict.checks)),
        "checks_pass": float(sum(c.status == CheckStatus.PASS for c in verdict.checks)),
        "value_len": float(len(val)),
        "value_empty": 1.0 if not val.strip() else 0.0,
        "has_digits": 1.0 if any(ch.isdigit() for ch in val) else 0.0,
        "history_match": _history_feature(verdict),
    }
    for scope in ("sender", "format", "global"):
        s = stats.get(scope) or LedgerStat(key="")
        f[f"{scope}_n"] = float(min(s.n, 1000))
        f[f"{scope}_rate"] = float(s.correction_rate if s.n else 0.5)   # 未知は 0.5（中立）
    for ft in FIELD_TYPES:
        f[f"ft_{ft}"] = 1.0 if verdict.field_type == ft else 0.0
    return f


def _history_feature(verdict: FieldVerdict) -> float:
    for c in verdict.checks:
        if c.name == "history":
            if c.status == CheckStatus.PASS:
                return 1.0
            if c.status == CheckStatus.FAIL or "不一致" in (c.detail or ""):
                return -1.0                      # 不一致・1〜2 文字違い・異体字違いはすべて「履歴と合わない」
            return 0.0
    return 0.0


def to_vector(features: dict[str, float]) -> list[float]:
    return [float(features.get(n, 0.0)) for n in FEATURE_NAMES]
