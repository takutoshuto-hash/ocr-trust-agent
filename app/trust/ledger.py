"""トラスト台帳: 項目種別 × キー階層ごとの承認実績から自律レベルを決める。

キー階層（具体 → 汎用）:
  1. field_type|sender:<id>     送り主ごと
  2. field_type|format:<id>     帳票様式ごと
  3. field_type|global          全体
十分なサンプルがある最も具体的な層を採用し、なければ上位層へフォールバック。
新規記入者は自動的に format / global 層で判定される。
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from app.schemas import AutonomyLevel


@dataclass
class LedgerStat:
    key: str
    n: int = 0
    corrections: int = 0
    streak: int = 0          # 連続無修正承認
    level: int = 0

    @property
    def correction_rate(self) -> float:
        return self.corrections / self.n if self.n else 1.0

    def to_dict(self) -> dict:
        return {"key": self.key, "n": self.n, "corrections": self.corrections, "streak": self.streak, "level": self.level}

    @classmethod
    def from_dict(cls, d: dict) -> "LedgerStat":
        return cls(**{k: d[k] for k in ("key", "n", "corrections", "streak", "level") if k in d})


@dataclass
class Policy:
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Policy":
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})

    def matches(self, patterns_key: str, field_type: str) -> bool:
        return any(fnmatch.fnmatch(field_type, p) for p in self.raw.get(patterns_key, []) or [])

    @property
    def promotion(self) -> dict:
        return self.raw.get("promotion", {})

    @property
    def min_samples_for_sender(self) -> int:
        return int(self.raw.get("min_samples_for_sender", 5))

    @property
    def router(self) -> dict:
        return self.raw.get("router", {})

    @property
    def budget(self) -> dict:
        return self.raw.get("budget", {})

    @property
    def demote_on_correction(self) -> bool:
        return bool(self.raw.get("demote_on_correction", True))


class TrustLedger:
    def __init__(self, store, policy: Policy):
        self.store = store          # get_ledger(key) / put_ledger(stat)
        self.policy = policy

    # ---- キー ----
    @staticmethod
    def keys(field_type: str, sender_id: str, format_id: str) -> list[str]:
        return [f"{field_type}|sender:{sender_id}", f"{field_type}|format:{format_id}", f"{field_type}|global"]

    def stat(self, key: str) -> LedgerStat:
        return self.store.get_ledger(key) or LedgerStat(key=key)

    # ---- 判定 ----
    def must_review(self, field_type: str, sender_id: str) -> Optional[str]:
        """ポリシーで人の確認が必須なら理由を返す（学習ルーターより優先）。"""
        sender_stat = self.stat(f"{field_type}|sender:{sender_id}")
        if sender_stat.n < self.policy.min_samples_for_sender and self.policy.matches("new_sender_review", field_type):
            return f"新規送り主（実績 {sender_stat.n} 件）のため人が確認（ポリシー new_sender_review）"
        return None

    def resolve(self, field_type: str, sender_id: str, format_id: str) -> tuple[AutonomyLevel, LedgerStat, list[str]]:
        """採用した層のレベルと統計、理由を返す。統計は「検証合格だった項目」だけを数えたもの。"""
        reasons: list[str] = []
        min_n = int(self.policy.promotion.get("L1", {}).get("min_samples", 30))
        chosen: Optional[LedgerStat] = None
        for key in self.keys(field_type, sender_id, format_id):
            s = self.stat(key)
            if s.n >= min_n:
                chosen = s
                break
        if chosen is None:
            chosen = self.stat(f"{field_type}|global")
            reasons.append(f"実績不足（{chosen.key} n={chosen.n}）→ L0")
            return AutonomyLevel.L0, chosen, reasons

        level = AutonomyLevel(chosen.level)
        reasons.append(f"台帳 {chosen.key}: L{chosen.level} (n={chosen.n}, 修正率={chosen.correction_rate:.3f}, streak={chosen.streak})")
        if level == AutonomyLevel.L2 and self.policy.matches("never_l2", field_type):
            reasons.append("ポリシー never_l2 により L1 に制限")
            level = AutonomyLevel.L1
        return level, chosen, reasons

    # ---- 学習（人の確定を反映） ----
    def record(self, field_type: str, sender_id: str, format_id: str, corrected: bool, judge_ok: bool = True) -> list[dict]:
        """全階層を更新し、昇格/降格イベントを返す。

        L1/L2 は「検証に通った値をそのまま採用してよいか」の判断なので、
        検証 FAIL だった項目（どのみち人が見る）は統計に含めない。
        """
        events: list[dict] = []
        if not judge_ok:
            return events
        for key in self.keys(field_type, sender_id, format_id):
            s = self.stat(key)
            before = s.level
            s.n += 1
            if corrected:
                s.corrections += 1
                s.streak = 0
                if self.policy.demote_on_correction and s.level > 0:
                    s.level = 0
            else:
                s.streak += 1
                s.level = self._eligible_level(s, field_type)
            self.store.put_ledger(s)
            if s.level != before:
                events.append({"key": key, "from": before, "to": s.level, "n": s.n, "streak": s.streak})
        return events

    def _eligible_level(self, s: LedgerStat, field_type: str) -> int:
        level = 0
        for lv in (1, 2):
            cond = self.policy.promotion.get(f"L{lv}", {})
            if (s.n >= int(cond.get("min_samples", 10**9))
                    and s.correction_rate <= float(cond.get("max_correction_rate", 0))
                    and s.streak >= int(cond.get("min_streak", 10**9))):
                if lv == 2 and self.policy.matches("never_l2", field_type):
                    break
                level = lv
        return level
