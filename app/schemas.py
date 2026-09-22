"""データモデル。

帳票 → 項目（フィールド）単位で扱う。パスは "applicant.zip" や "deliveries[0].name" の形。
項目種別（field_type）はパスから添字を除いたもの（"deliveries.name"）。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------- 帳票の構造（Gemini の responseSchema にもなる） ----------

class Delivery(BaseModel):
    name: str = ""
    name_kana: str = ""
    zip: str = ""
    address: str = ""
    phone: str = ""
    product_code: str = ""
    qty: int = 1
    noshi_name: str = ""


class Applicant(BaseModel):
    name: str = ""
    name_kana: str = ""
    zip: str = ""
    address: str = ""
    phone: str = ""
    organization: str = ""


class OrderForm(BaseModel):
    applicant: Applicant = Field(default_factory=Applicant)
    deliveries: list[Delivery] = Field(default_factory=list)

    # --- フィールド単位のフラット化 ---
    def flatten(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in self.applicant.model_dump().items():
            out[f"applicant.{k}"] = v
        for i, d in enumerate(self.deliveries):
            for k, v in d.model_dump().items():
                out[f"deliveries[{i}].{k}"] = v
        return out

    @classmethod
    def from_flat(cls, flat: dict[str, Any]) -> "OrderForm":
        applicant: dict[str, Any] = {}
        deliveries: dict[int, dict[str, Any]] = {}
        for path, v in flat.items():
            m = re.fullmatch(r"applicant\.(\w+)", path)
            if m:
                applicant[m.group(1)] = v
                continue
            m = re.fullmatch(r"deliveries\[(\d+)\]\.(\w+)", path)
            if m:
                deliveries.setdefault(int(m.group(1)), {})[m.group(2)] = v
        return cls(
            applicant=Applicant(**applicant),
            deliveries=[Delivery(**deliveries[i]) for i in sorted(deliveries)],
        )


def field_type_of(path: str) -> str:
    """'deliveries[2].zip' -> 'deliveries.zip'"""
    return re.sub(r"\[\d+\]", "", path)


# ---------- 抽出 ----------

class FieldValue(BaseModel):
    path: str
    value: Any
    confidence: float = 0.0          # モデル自己申告（参考値。判定には直接使わない）
    evidence: str = ""               # 読んだ文字列そのもの


class Extraction(BaseModel):
    form: OrderForm
    fields: dict[str, FieldValue]
    raw_text: str = ""
    model: str = ""
    latency_ms: int = 0


# ---------- ジャッジ ----------

class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"   # 判定材料なし（マスタ未登録など）


class CheckResult(BaseModel):
    name: str
    status: CheckStatus
    detail: str = ""


class FieldVerdict(BaseModel):
    path: str
    field_type: str
    checks: list[CheckResult] = Field(default_factory=list)
    agreement: Optional[bool] = None   # 二重読み取りの一致（None=未実施）
    reason: str = ""                   # 人向けの説明

    @property
    def any_fail(self) -> bool:
        return any(c.status == CheckStatus.FAIL for c in self.checks)

    @property
    def all_pass(self) -> bool:
        return bool(self.checks) and all(c.status == CheckStatus.PASS for c in self.checks)


# ---------- 判定（自律レベル） ----------

class AutonomyLevel(int, Enum):
    L0 = 0   # すべて人が確認
    L1 = 1   # ジャッジ合格なら自動確定（事後報告）
    L2 = 2   # 抽出値をそのまま自動確定


class FieldStatus(str, Enum):
    AUTO = "auto"       # 自動確定
    REVIEW = "review"   # 要確認


class FieldDecision(BaseModel):
    path: str
    field_type: str
    value: Any
    status: FieldStatus
    level: AutonomyLevel
    audit: bool = False                    # 自動確定だが監査サンプリングで人にも見せる
    judge_ok: bool = False                 # 検証に FAIL が無かった
    resolved_from: Optional[Any] = None    # 行動するエージェントが修復した場合の元の値
    p_correction: Optional[float] = None   # 学習ルーターの予測（未学習なら None）
    reasons: list[str] = Field(default_factory=list)

    @property
    def human_sees(self) -> bool:
        return self.status == FieldStatus.REVIEW or self.audit


class FormDecision(BaseModel):
    form_id: str
    sender_id: str
    format_id: str
    created_at: datetime = Field(default_factory=now_utc)
    extraction: Extraction
    verdicts: dict[str, FieldVerdict]
    decisions: dict[str, FieldDecision]
    status: str = "pending"   # pending | confirmed
    final: Optional[OrderForm] = None
    expires_at: Optional[datetime] = None   # 保持期限（Firestore TTL で自動削除）
    review_opened_at: Optional[datetime] = None   # 人が確認画面を最初に開いた時刻（実測用）
    review_seconds: Optional[float] = None        # 開いてから確定までの秒数（要確認項目だけ見る場合の実測）

    @property
    def needs_review(self) -> bool:
        """人が開く必要があるか（要確認 or 監査サンプル）。"""
        return any(d.human_sees for d in self.decisions.values())

    @property
    def review_paths(self) -> list[str]:
        """人が見る項目（要確認＋監査サンプル）。"""
        return [p for p, d in self.decisions.items() if d.human_sees]

    @property
    def strict_review_paths(self) -> list[str]:
        """要確認のみ（監査サンプルを除く）。要確認率の分子。"""
        return [p for p, d in self.decisions.items() if d.status == FieldStatus.REVIEW]


# ---------- 学習用レコード（人の確定が教師データになる） ----------

class TrainingRecord(BaseModel):
    form_id: str
    path: str
    field_type: str
    sender_id: str
    format_id: str
    extracted: Any
    final: Any
    corrected: bool
    was_auto: bool
    verified: bool = True      # 人が実際に目視したラベルか（自動確定で未監査なら False → 学習に使わない）
    judge_ok: bool = True
    features: dict[str, float]
    created_at: datetime = Field(default_factory=now_utc)


# ---------- 振り返りエージェントの提案 ----------

class ProposalKind(str, Enum):
    RULE = "rule"        # 読み取りルール（抽出プロンプトに注入するヒント）
    POLICY = "policy"    # ポリシー値の変更（許可されたキー・範囲内のみ）


class Proposal(BaseModel):
    proposal_id: str
    kind: ProposalKind
    title: str
    rationale: str                          # 根拠（集計値を含む）
    evidence: dict[str, Any] = Field(default_factory=dict)
    rule_text: Optional[str] = None         # kind=rule
    rule_scope: str = "global"              # global | format:<id> | sender:<id>
    policy_key: Optional[str] = None        # kind=policy 例 "hallucination.blank_ink_ratio"
    policy_from: Optional[Any] = None
    policy_to: Optional[Any] = None
    status: str = "pending"                 # pending | approved | rejected
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=now_utc)
    proposer: str = "reflection_agent"      # reflection_agent(adk) | reflection_rules


class ApprovedRule(BaseModel):
    rule_id: str
    text: str
    scope: str = "global"
    source_proposal: str = ""
    created_at: datetime = Field(default_factory=now_utc)


# ---------- 監査ログ ----------

class AuditEvent(BaseModel):
    form_id: str
    event: str                     # extracted | judged | decided | confirmed | ledger_promoted | ledger_demoted | router_trained
    actor: str = "agent"           # agent | human:<name> | system
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)
