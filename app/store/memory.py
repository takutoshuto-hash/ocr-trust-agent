"""インメモリ実装（ローカル開発・テスト・シミュレーション用）。"""
from __future__ import annotations

from typing import Optional

from app.schemas import AuditEvent, FormDecision, OrderForm, TrainingRecord
from app.trust.ledger import LedgerStat


class MemoryStore:
    def __init__(self):
        self.forms: dict[str, FormDecision] = {}
        self.images: dict[str, bytes] = {}
        self.ledger: dict[str, LedgerStat] = {}
        self.training: list[TrainingRecord] = []
        self.audit: list[AuditEvent] = []
        self.proposals: dict[str, object] = {}
        self.rules: list[object] = []
        self.overrides: dict[str, object] = {}

    def put_form(self, fd, image):
        self.forms[fd.form_id] = fd
        if image:
            self.images[fd.form_id] = image

    def get_form(self, form_id):
        return self.forms.get(form_id)

    def get_image(self, form_id):
        return self.images.get(form_id)

    def list_pending(self, limit=50):
        return [f for f in reversed(self.forms.values()) if f.status == "pending"][:limit]

    def list_forms(self, limit=200):
        return list(reversed(self.forms.values()))[:limit]

    def recent_confirmed(self, sender_id, format_id, limit=3):
        out = []
        for f in reversed(self.forms.values()):
            if f.status == "confirmed" and f.final and (f.sender_id == sender_id or f.format_id == format_id):
                out.append((f"sender={f.sender_id} format={f.format_id}", f.final))
                if len(out) >= limit:
                    break
        return out

    def sender_history(self, sender_id, limit=10):
        out = []
        for f in reversed(self.forms.values()):
            if f.status == "confirmed" and f.final and f.sender_id == sender_id:
                out.append(f.final)
                if len(out) >= limit:
                    break
        return out

    def customer_history(self, phone_key, limit=10):
        out = []
        if not phone_key:
            return out
        for f in reversed(self.forms.values()):
            if f.status == "confirmed" and f.final and f.applicant_phone_key == phone_key:
                out.append(f.final)
                if len(out) >= limit:
                    break
        return out

    def get_ledger(self, key):
        return self.ledger.get(key)

    def put_ledger(self, stat):
        self.ledger[stat.key] = stat

    def list_ledger(self):
        return list(self.ledger.values())

    def add_training(self, recs):
        self.training.extend(recs)

    def list_training(self, limit=100_000):
        return self.training[-limit:]

    def count_training(self):
        return len(self.training)

    def add_audit(self, ev):
        self.audit.append(ev)

    def list_audit(self, form_id=None, limit=200):
        evs = [e for e in self.audit if form_id is None or e.form_id == form_id]
        return evs[-limit:]

    # ---- 振り返り ----
    def put_proposal(self, p):
        self.proposals[p.proposal_id] = p

    def get_proposal(self, proposal_id):
        return self.proposals.get(proposal_id)

    def list_proposals(self, status=None, limit=100):
        ps = [p for p in self.proposals.values() if status is None or p.status == status]
        return sorted(ps, key=lambda p: p.created_at, reverse=True)[:limit]

    def put_rule(self, rule):
        self.rules.append(rule)

    def list_rules(self, scope=None):
        return [r for r in self.rules if scope is None or r.scope == scope or r.scope == "global"]

    def put_policy_override(self, key, value):
        self.overrides[key] = value

    def get_policy_overrides(self):
        return dict(self.overrides)
