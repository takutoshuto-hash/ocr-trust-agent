"""Firestore 実装（Cloud Run 本番用）。画像は GCS_BUCKET があれば Cloud Storage、なければ Firestore に base64。"""
from __future__ import annotations

import base64
from typing import Optional

from google.cloud import firestore

from app.config import settings
from app.schemas import AuditEvent, FormDecision, OrderForm, TrainingRecord
from app.trust.ledger import LedgerStat


class FirestoreStore:
    def __init__(self, project: Optional[str], prefix: str):
        self.db = firestore.Client(project=project)
        self.p = prefix
        self.bucket = None
        if settings.gcs_bucket:
            from google.cloud import storage
            self.bucket = storage.Client(project=project).bucket(settings.gcs_bucket)

    def _c(self, name: str):
        return self.db.collection(f"{self.p}_{name}")

    # ---- forms ----
    def put_form(self, fd, image):
        self._c("forms").document(fd.form_id).set(fd.model_dump(mode="json"))
        if image:
            if self.bucket:
                self.bucket.blob(f"forms/{fd.form_id}.png").upload_from_string(image, content_type="image/png")
            else:
                self._c("images").document(fd.form_id).set({"b64": base64.b64encode(image).decode()})

    def get_form(self, form_id):
        d = self._c("forms").document(form_id).get()
        return FormDecision.model_validate(d.to_dict()) if d.exists else None

    def get_image(self, form_id):
        if self.bucket:
            b = self.bucket.blob(f"forms/{form_id}.png")
            return b.download_as_bytes() if b.exists() else None
        d = self._c("images").document(form_id).get()
        return base64.b64decode(d.to_dict()["b64"]) if d.exists else None

    # 複合インデックス（等価 + 並び替え）を要求しないよう、絞り込みは単一フィールドで行い並び替えはクライアント側で行う

    def list_pending(self, limit=50):
        q = self._c("forms").where(filter=firestore.FieldFilter("status", "==", "pending")).limit(500)
        forms = [FormDecision.model_validate(d.to_dict()) for d in q.stream()]
        forms.sort(key=lambda f: f.created_at, reverse=True)
        return forms[:limit]

    def list_forms(self, limit=200):
        q = self._c("forms").order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit)
        return [FormDecision.model_validate(d.to_dict()) for d in q.stream()]

    def recent_confirmed(self, sender_id, format_id, limit=3):
        q = self._c("forms").where(filter=firestore.FieldFilter("sender_id", "==", sender_id)).limit(50)
        forms = [FormDecision.model_validate(d.to_dict()) for d in q.stream()]
        forms.sort(key=lambda f: f.created_at, reverse=True)
        return [(f"sender={f.sender_id}", f.final) for f in forms if f.status == "confirmed" and f.final][:limit]

    # ---- ledger ----
    def get_ledger(self, key):
        d = self._c("ledger").document(key.replace("/", "_")).get()
        return LedgerStat.from_dict(d.to_dict()) if d.exists else None

    def put_ledger(self, stat):
        self._c("ledger").document(stat.key.replace("/", "_")).set(stat.to_dict())

    def list_ledger(self):
        return [LedgerStat.from_dict(d.to_dict()) for d in self._c("ledger").stream()]

    # ---- training ----
    def add_training(self, recs):
        batch = self.db.batch()
        for r in recs:
            batch.set(self._c("training").document(), r.model_dump(mode="json"))
        batch.commit()

    def list_training(self, limit=100_000):
        q = self._c("training").order_by("created_at").limit(limit)
        return [TrainingRecord.model_validate(d.to_dict()) for d in q.stream()]

    def count_training(self):
        return self._c("training").count().get()[0][0].value

    # ---- 振り返り ----
    def put_proposal(self, p):
        self._c("proposals").document(p.proposal_id).set(p.model_dump(mode="json"))

    def get_proposal(self, proposal_id):
        from app.schemas import Proposal
        d = self._c("proposals").document(proposal_id).get()
        return Proposal.model_validate(d.to_dict()) if d.exists else None

    def list_proposals(self, status=None, limit=100):
        from app.schemas import Proposal
        q = self._c("proposals")
        if status:
            q = q.where(filter=firestore.FieldFilter("status", "==", status))
        ps = [Proposal.model_validate(d.to_dict()) for d in q.limit(500).stream()]
        return sorted(ps, key=lambda p: p.created_at, reverse=True)[:limit]

    def put_rule(self, rule):
        self._c("rules").document(rule.rule_id).set(rule.model_dump(mode="json"))

    def list_rules(self, scope=None):
        from app.schemas import ApprovedRule
        rs = [ApprovedRule.model_validate(d.to_dict()) for d in self._c("rules").limit(200).stream()]
        return [r for r in rs if scope is None or r.scope == scope or r.scope == "global"]

    def put_policy_override(self, key, value):
        self._c("policy").document("overrides").set({key.replace(".", "__"): value}, merge=True)

    def get_policy_overrides(self):
        d = self._c("policy").document("overrides").get()
        return {k.replace("__", "."): v for k, v in (d.to_dict() or {}).items()} if d.exists else {}

    # ---- audit ----
    def add_audit(self, ev):
        self._c("audit").document().set(ev.model_dump(mode="json"))

    def list_audit(self, form_id=None, limit=200):
        if form_id:
            q = self._c("audit").where(filter=firestore.FieldFilter("form_id", "==", form_id)).limit(limit)
            evs = [AuditEvent.model_validate(d.to_dict()) for d in q.stream()]
            return sorted(evs, key=lambda e: e.created_at)
        q = self._c("audit").order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit)
        return sorted((AuditEvent.model_validate(d.to_dict()) for d in q.stream()), key=lambda e: e.created_at)
