"""パイプライン: 抽出（二重読み取り）→ ジャッジ → 台帳＋ルーターで判定 → 保存・監査。
人の確定（confirm）で、台帳更新・教師データ追加・必要なら再学習。
"""
from __future__ import annotations

import random
import uuid
from typing import Optional

from datetime import timedelta

from app.config import settings
from app.extract import Extractor, get_extractor, sends_to_cloud
from app.judge import Judge
from app.judge.zones import load_zones, mask_zones
from app.learn import CorrectionRouter, build_features
from app.schemas import (AuditEvent, AutonomyLevel, Extraction, FieldDecision, FieldStatus, FormDecision,
                         OrderForm, TrainingRecord, field_type_of, now_utc)
from app.store import Store, get_store
from app.trust import Policy, TrustLedger


class Pipeline:
    def __init__(self, store: Optional[Store] = None, extractor: Optional[Extractor] = None,
                 policy: Optional[Policy] = None, router: Optional[CorrectionRouter] = None,
                 seed: Optional[int] = None):
        self.store = store or get_store()
        self.policy = policy or Policy.load(settings.policy_path)
        self.profile = str(self.policy.raw.get("profile", "lean"))
        self.extractor = extractor or get_extractor(self.profile)
        self.ledger = TrustLedger(self.store, self.policy)
        hc = self.policy.raw.get("hallucination", {}) or {}
        self.judge = Judge(blank_ink_ratio=float(hc.get("blank_ink_ratio", 0.004)),
                           evidence_max_distance=float(hc.get("evidence_max_distance", 0.5)))
        rc = self.policy.router
        self.router = router or CorrectionRouter(
            settings.model_dir,
            min_samples=int(rc.get("min_training_samples", 200)),
            target_error_rate=float(rc.get("target_error_rate", 0.005)),
        )
        self._since_train = 0
        self._rng = random.Random(seed)

    # ---------------- 受付 → 判定 ----------------
    def process(self, image: bytes, *, sender_id: str, format_id: str = "fax_v1",
                hint: Optional[OrderForm] = None, double_read: bool = True) -> FormDecision:
        form_id = uuid.uuid4().hex[:12]
        examples = self.store.recent_confirmed(sender_id, format_id)

        # データ最小化: クラウドへ送る場合は機密欄をマスクし、送信内容を監査ログに残す
        to_send = image
        masked: list[str] = []
        if sends_to_cloud(self.extractor):
            to_send, masked = self._minimize(image, format_id)
            self._audit(form_id, "external_transmission", {
                "destination": "gemini_api", "model": getattr(self.extractor, "model", ""), "bytes": len(to_send),
                "masked_fields": masked, "profile": self.profile, "purpose": "extraction",
            })

        ex1 = self.extractor.extract(to_send, examples=examples, variant=0, hint=hint)
        ex2 = self.extractor.extract(to_send, examples=examples, variant=1, hint=hint) if double_read else None
        self._audit(form_id, "extracted", {"model": ex1.model, "double_read": double_read, "few_shot": len(examples)})

        verdicts = self.judge.judge(ex1, ex2, image=image, format_id=format_id)
        self._audit(form_id, "judged", {"fail": [p for p, v in verdicts.items() if v.any_fail],
                                        "disagree": [p for p, v in verdicts.items() if v.agreement is False]})

        decisions: dict[str, FieldDecision] = {}
        for path, fv in ex1.fields.items():
            decisions[path] = self._decide(fv, verdicts[path], sender_id, format_id)

        retention = int(self.policy.raw.get("retention_days", 30))
        fd = FormDecision(form_id=form_id, sender_id=sender_id, format_id=format_id,
                          extraction=ex1, verdicts=verdicts, decisions=decisions,
                          expires_at=now_utc() + timedelta(days=retention))
        self.store.put_form(fd, image)
        self._audit(form_id, "decided", {"review": fd.review_paths, "auto": len(decisions) - len(fd.review_paths)})
        return fd

    def _minimize(self, image: bytes, format_id: str) -> tuple[bytes, list[str]]:
        """ポリシー mask_before_cloud に該当する欄を白塗りしてから送る。ゾーン未定義の項目はマスクできない（監査に残る）。"""
        import fnmatch
        patterns = self.policy.raw.get("mask_before_cloud", []) or []
        if not patterns:
            return image, []
        zones = load_zones(format_id)
        targets = [p for p in zones if any(fnmatch.fnmatch(field_type_of(p), pat) or fnmatch.fnmatch(p, pat) for pat in patterns)]
        if not targets:
            return image, []
        return mask_zones(image, [zones[p] for p in targets]), targets

    def _decide(self, fv, verdict, sender_id: str, format_id: str) -> FieldDecision:
        """優先順位:
        1. 検証 FAIL → 要確認（安全側）
        2. ポリシーの必須確認（新規送り主の氏名・住所など）→ 要確認
        3. 学習ルーターが学習済みなら、その判定（目標誤り率から逆算した閾値）
        4. 未学習なら台帳: L2 = 検証合格で自動 / L1 = 検証合格＋二重読み取り一致で自動 / L0 = 人
        自動確定の一部は監査サンプリングで人にも見せる（学習ラベルの偏り防止・見逃し誤りの計測）。
        """
        ft = verdict.field_type
        level, stat, reasons = self.ledger.resolve(ft, sender_id, format_id)
        stats = {"sender": self.ledger.stat(f"{ft}|sender:{sender_id}"),
                 "format": self.ledger.stat(f"{ft}|format:{format_id}"),
                 "global": self.ledger.stat(f"{ft}|global")}
        feats = build_features(fv, verdict, stats)
        p = self.router.predict(feats)
        router_auto = self.router.is_auto(p)
        judge_ok = not verdict.any_fail
        forced = self.ledger.must_review(ft, sender_id)

        status = FieldStatus.REVIEW
        if not judge_ok:
            reasons.append("検証に失敗 → 要確認")
        elif forced:
            reasons.append(forced)
        elif router_auto is True:
            status = FieldStatus.AUTO
            reasons.append(f"学習ルーター p={p:.4f} < 閾値 {self.router.threshold:.4f} → 自動確定")
        elif router_auto is False:
            reasons.append(f"学習ルーター p={p:.4f} ≥ 閾値 {self.router.threshold:.4f} → 要確認")
        elif level == AutonomyLevel.L2:
            status = FieldStatus.AUTO
            reasons.append("L2: 検証合格 → 自動確定（二重読み取り不要）")
        elif level == AutonomyLevel.L1 and verdict.agreement:
            status = FieldStatus.AUTO
            reasons.append("L1: 検証合格＋二重読み取り一致 → 自動確定")
        else:
            reasons.append("L0（ルーター未学習・台帳実績不足）→ 人が確認")

        audit = False
        if status == FieldStatus.AUTO and self._rng.random() < float(self.policy.raw.get("audit_sampling_rate", 0.0)):
            audit = True
            reasons.append("監査サンプリング対象（自動確定だが人も確認）")

        d = FieldDecision(path=fv.path, field_type=ft, value=fv.value, status=status, level=level,
                          audit=audit, judge_ok=judge_ok, p_correction=p, reasons=reasons)
        d._features = feats   # 確定時に教師データへ（pydantic の private 属性）
        return d

    # ---------------- 人の確定 → 学習 ----------------
    def confirm(self, form_id: str, corrections: dict[str, object], actor: str = "human") -> FormDecision:
        """corrections: {path: 確定値}。渡されなかった要確認項目は抽出値のまま承認とみなす。"""
        fd = self.store.get_form(form_id)
        if fd is None:
            raise KeyError(form_id)
        if fd.status == "confirmed":
            return fd

        final_flat = {}
        recs: list[TrainingRecord] = []
        ledger_events = []
        for path, d in fd.decisions.items():
            final = corrections.get(path, d.value)
            if isinstance(d.value, int):
                try:
                    final = int(final)
                except (TypeError, ValueError):
                    final = d.value
            corrected = _norm(final) != _norm(d.value)
            final_flat[path] = final
            feats = getattr(d, "_features", None) or build_features(
                fd.extraction.fields[path], fd.verdicts[path],
                {"sender": self.ledger.stat(f"{d.field_type}|sender:{fd.sender_id}"),
                 "format": self.ledger.stat(f"{d.field_type}|format:{fd.format_id}"),
                 "global": self.ledger.stat(f"{d.field_type}|global")})
            recs.append(TrainingRecord(form_id=form_id, path=path, field_type=d.field_type, sender_id=fd.sender_id,
                                       format_id=fd.format_id, extracted=d.value, final=final, corrected=corrected,
                                       was_auto=d.status == FieldStatus.AUTO, verified=d.human_sees,
                                       judge_ok=d.judge_ok, features=feats))
            if d.human_sees:   # 台帳も人が見た実績だけで更新
                ledger_events += self.ledger.record(d.field_type, fd.sender_id, fd.format_id, corrected, judge_ok=d.judge_ok)

        fd.final = OrderForm.from_flat(final_flat)
        fd.status = "confirmed"
        self.store.put_form(fd, b"")
        self.store.add_training(recs)
        self._audit(form_id, "confirmed", {"corrected": [r.path for r in recs if r.corrected],
                                           "auto_errors": [r.path for r in recs if r.corrected and r.was_auto]}, actor=actor)
        for ev in ledger_events:
            self._audit(form_id, "ledger_promoted" if ev["to"] > ev["from"] else "ledger_demoted", ev, actor="system")

        self._since_train += len(recs)
        if self._since_train >= int(self.policy.router.get("retrain_every_records", 500)):
            self.retrain()
        return fd

    def retrain(self) -> dict:
        summary = self.router.train(self.store.list_training())
        self._since_train = 0
        self._audit("-", "router_trained", summary, actor="system")
        return summary

    # ---------------- 指標 ----------------
    def metrics(self) -> dict:
        forms = self.store.list_forms(limit=10_000)
        n_fields = sum(len(f.decisions) for f in forms)
        n_review = sum(len(f.strict_review_paths) for f in forms)
        n_audit = sum(1 for f in forms for d in f.decisions.values() if d.audit)
        recs = self.store.list_training()
        audited = [r for r in recs if r.was_auto and r.verified]   # 監査サンプルで実測した自動確定の誤り
        return {
            "forms": len(forms),
            "fields": n_fields,
            "review_rate": round(n_review / n_fields, 4) if n_fields else None,
            "audit_rate": round(n_audit / n_fields, 4) if n_fields else None,
            "auto_error_rate_audited": round(sum(r.corrected for r in audited) / len(audited), 4) if audited else None,
            "training_records": len(recs),
            "hallucination_flags": sum(1 for f in forms for v in f.verdicts.values()
                                       for c in v.checks if c.name == "blank_zone" and c.status.value == "fail"),
            "profile": self.profile,
            "router": {"trained_on": self.router.trained_on, "threshold": self.router.threshold},
            "ledger_levels": {s.key: s.level for s in self.store.list_ledger() if s.level > 0},
        }

    def _audit(self, form_id: str, event: str, detail: dict, actor: str = "agent") -> None:
        self.store.add_audit(AuditEvent(form_id=form_id, event=event, actor=actor, detail=detail))


def _norm(x) -> str:
    return str(x if x is not None else "").replace(" ", "").replace("　", "").replace("-", "").upper()
