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
from app.reflect.agent import ReflectionAgent, _set_path
from app.resolve import Resolver
from app.schemas import (AuditEvent, AutonomyLevel, Extraction, FieldDecision, FieldStatus, FormDecision,
                         OrderForm, TrainingRecord, field_type_of, now_utc)
from app.store import Store, get_store
from app.trust import Policy, TrustLedger


class Pipeline:
    def __init__(self, store: Optional[Store] = None, extractor: Optional[Extractor] = None,
                 policy: Optional[Policy] = None, router: Optional[CorrectionRouter] = None,
                 seed: Optional[int] = None, explain: Optional[bool] = None):
        self.store = store or get_store()
        self.policy = policy or Policy.load(settings.policy_path)
        self.profile = str(self.policy.raw.get("profile", "lean"))
        self.extractor = extractor or get_extractor(self.profile)
        self.ledger = TrustLedger(self.store, self.policy)
        hc = self.policy.raw.get("hallucination", {}) or {}
        self.judge = Judge(blank_ink_ratio=float(hc.get("blank_ink_ratio", 0.004)),
                           evidence_max_distance=float(hc.get("evidence_max_distance", 0.5)))
        ac = self.policy.raw.get("actions", {}) or {}
        # ADK プランナーは Gemini 抽出器のときだけ（モック・ローカル Gemma では決定的なルールプランナー）
        planner = str(ac.get("planner", "rules")) if self.extractor.name == "gemini" else "rules"
        self.resolver = Resolver(self.extractor, self.judge, ac, planner=planner)
        # 振り返りで承認済みのポリシー上書きを反映
        for key, val in (self.store.get_policy_overrides() or {}).items():
            _set_path(self.policy.raw, key, val)
        rf = self.policy.raw.get("reflection", {}) or {}
        self.reflection = ReflectionAgent(self.store, self.policy.raw, planner=str(rf.get("planner", "rules")) if self.extractor.name == "gemini" else "rules")
        rc = self.policy.router
        self.router = router or CorrectionRouter(
            settings.model_dir,
            min_samples=int(rc.get("min_training_samples", 200)),
            target_error_rate=float(rc.get("target_error_rate", 0.005)),
        )
        self._since_train = 0
        self._rng = random.Random(seed)
        # 受付時の説明文生成（ADK）: Gemini 抽出器の本番運用でのみ既定 ON。モック・シミュレーション・テストでは OFF
        self.explain = (self.extractor.name == "gemini") if explain is None else bool(explain)
        # 予算縮退: 1日の Gemini 呼び出し数・自動確定数を数え、上限に応じて段階的に縮退する
        self.budget = BudgetGuard(self.policy.budget, count_calls=(self.extractor.name == "gemini"))
        if self.budget.count_calls:
            self.extractor = _CountingExtractor(self.extractor, self.budget)
            self.resolver.extractor = self.extractor

    # ---------------- 受付 → 判定 ----------------
    def process(self, image: bytes, *, sender_id: str, format_id: str = "fax_v1",
                hint: Optional[OrderForm] = None, double_read: bool = True) -> FormDecision:
        form_id = uuid.uuid4().hex[:12]
        examples = self.store.recent_confirmed(sender_id, format_id)

        # 予算縮退の段階を判定（normal → reduced: 二重読み取り・再読み取り省略 → exhausted: 自動確定停止）
        mode, changed = self.budget.mode()
        if changed:
            self._audit("-", "budget_state", {"mode": mode, **self.budget.snapshot()}, actor="system")
        if mode != "normal":
            double_read = False
        self.resolver.enabled = bool(self.policy.raw.get("actions", {}).get("enabled", True)) and mode == "normal"

        # データ最小化: クラウドへ送る場合は機密欄をマスクし、送信内容を監査ログに残す
        to_send = image
        masked: list[str] = []
        if sends_to_cloud(self.extractor):
            to_send, masked = self._minimize(image, format_id)
            self._audit(form_id, "external_transmission", {
                "destination": "gemini_api", "model": getattr(self.extractor, "model", ""), "bytes": len(to_send),
                "masked_fields": masked, "profile": self.profile, "purpose": "extraction",
            })

        rules = [r.text for r in self.store.list_rules(scope=f"format:{format_id}")]   # 承認済みルール（global + 様式）
        ex1 = self.extractor.extract(to_send, examples=examples, variant=0, hint=hint, rules=rules)
        ex2 = self.extractor.extract(to_send, examples=examples, variant=1, hint=hint, rules=rules) if double_read else None
        self._audit(form_id, "extracted", {"model": ex1.model, "double_read": double_read, "few_shot": len(examples), "rules": len(rules)})

        verdicts = self.judge.judge(ex1, ex2, image=image, format_id=format_id)
        self._audit(form_id, "judged", {"fail": [p for p, v in verdicts.items() if v.any_fail],
                                        "disagree": [p for p, v in verdicts.items() if v.agreement is False]})

        # 行動するエージェント: 失敗・不一致の項目を人に回す前に修復を試みる（行動はすべて監査へ）
        resolved: dict[str, object] = {}
        if self.resolver.enabled:
            updates, actions_log = self.resolver.resolve(ex1, ex2, verdicts, image=to_send, format_id=format_id, hint=hint)
            if actions_log:
                self._audit(form_id, "resolved", {"actions": actions_log, "updated": list(updates)})
            if updates:
                flat = ex1.form.flatten()
                for path, new in updates.items():
                    resolved[path] = flat[path]
                    flat[path] = new
                    ex1.fields[path].value = new
                ex1.form = OrderForm.from_flat(flat)
                # 修復後の値で再検証。修復値は「独立した読みと一致」が採用条件なので二重読み取り一致とみなす
                verdicts = self.judge.judge(ex1, ex2, image=image, format_id=format_id)
                for path in updates:
                    verdicts[path].agreement = True
                    verdicts[path].reason = "エージェントが修復（" + verdicts[path].reason + "）"

        decisions: dict[str, FieldDecision] = {}
        for path, fv in ex1.fields.items():
            decisions[path] = self._decide(fv, verdicts[path], sender_id, format_id)
            if path in resolved:
                decisions[path].resolved_from = resolved[path]
                decisions[path].reasons.insert(0, f"エージェントが {resolved[path]!r} → {fv.value!r} に修復")
            if mode == "exhausted" and decisions[path].status == FieldStatus.AUTO:
                decisions[path].status = FieldStatus.REVIEW
                decisions[path].audit = False
                decisions[path].reasons.append("予算縮退（exhausted）: 上限超過のため自動確定を停止し人が確認")
            elif mode == "reduced" and decisions[path].status == FieldStatus.AUTO:
                decisions[path].reasons.append("予算縮退（reduced）: 二重読み取りを省略して判定")
        self.budget.add_auto(sum(1 for d in decisions.values() if d.status == FieldStatus.AUTO))

        retention = int(self.policy.raw.get("retention_days", 30))
        fd = FormDecision(form_id=form_id, sender_id=sender_id, format_id=format_id,
                          extraction=ex1, verdicts=verdicts, decisions=decisions,
                          expires_at=now_utc() + timedelta(days=retention))
        if fd.needs_review and self.explain:
            # 要確認理由の説明文は受付時に作って保存（確認画面を開くときに LLM を待たせない）
            try:
                from app.judge.agent import explain_review
                fd.explanation = explain_review(fd)
            except Exception as e:   # 説明が作れなくても受付は止めない
                fd.explanation = f"（説明の生成に失敗: {str(e)[:120]}）"
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
        if fd.review_opened_at is not None:
            fd.review_seconds = round((now_utc() - fd.review_opened_at).total_seconds(), 1)
        self.store.put_form(fd, b"")
        self.store.add_training(recs)
        self._audit(form_id, "confirmed", {"corrected": [r.path for r in recs if r.corrected],
                                           "auto_errors": [r.path for r in recs if r.corrected and r.was_auto],
                                           "review_seconds": fd.review_seconds, "reviewed_fields": len(fd.review_paths)}, actor=actor)
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

    # ---------------- 振り返り（夜間） ----------------
    def reflect(self, days: int = 1) -> dict:
        """修正ログを集計し、振り返りエージェントが提案を作る。提案は承認されるまで何も変えない。"""
        from app.reflect import analyze
        from app.reflect.analysis import default_since
        analysis = analyze(self.store.list_training(), self.store.list_audit(limit=20_000), since=default_since(days))
        proposals = self.reflection.propose(analysis)
        self._audit("-", "reflected", {"window_days": days, "records": analysis.get("window_records"),
                                       "proposals": [p.model_dump(mode="json", include={"proposal_id", "kind", "title", "status"}) for p in proposals]},
                    actor="agent")
        return {"analysis": analysis, "proposals": [p.model_dump(mode="json") for p in proposals]}

    def decide_proposal(self, proposal_id: str, approve: bool, actor: str):
        p = self.reflection.decide(proposal_id, approve, actor)
        self._audit("-", "proposal_approved" if approve else "proposal_rejected",
                    {"proposal_id": p.proposal_id, "kind": p.kind.value, "title": p.title,
                     "policy_key": p.policy_key, "policy_to": p.policy_to, "rule_text": p.rule_text}, actor=actor)
        if approve and p.kind.value == "policy" and p.policy_key:
            self._apply_policy_live(p.policy_key, p.policy_to)
        return p

    def _apply_policy_live(self, key: str, value) -> None:
        """承認されたポリシー値を実行中の部品へ反映する。"""
        if key == "hallucination.blank_ink_ratio":
            self.judge.blank_ink_ratio = float(value)
        elif key == "router.target_error_rate":
            self.router.target_error_rate = float(value)

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
            "budget": {"mode": self.budget.mode()[0], **self.budget.snapshot()},
            "router": {"trained_on": self.router.trained_on, "threshold": self.router.threshold},
            "ledger_levels": {s.key: s.level for s in self.store.list_ledger() if s.level > 0},
        }

    def mark_review_opened(self, form_id: str) -> None:
        fd = self.store.get_form(form_id)
        if fd is not None and fd.status == "pending" and fd.review_opened_at is None:
            fd.review_opened_at = now_utc()
            self.store.put_form(fd, b"")

    def dashboard_data(self) -> dict:
        """ダッシュボード用: 日次系列（要確認率・自動確定の誤り率）、項目種別ごとの台帳レベル、確認時間の実測。"""
        from collections import defaultdict
        from datetime import timezone, timedelta
        from statistics import median
        JST = timezone(timedelta(hours=9))
        forms = self.store.list_forms(limit=10_000)
        recs = self.store.list_training()
        by_day: dict = defaultdict(lambda: {"fields": 0, "review": 0, "auto": 0, "forms": 0})
        for f in forms:
            d = f.created_at.astimezone(JST).strftime("%m/%d")
            b = by_day[d]; b["forms"] += 1; b["fields"] += len(f.decisions); b["review"] += len(f.strict_review_paths)
            b["auto"] += len(f.decisions) - len(f.strict_review_paths)
        audited: dict = defaultdict(lambda: {"n": 0, "err": 0})
        for r in recs:
            if r.was_auto and r.verified:
                d = r.created_at.astimezone(JST).strftime("%m/%d")
                audited[d]["n"] += 1; audited[d]["err"] += int(r.corrected)
        days = sorted(by_day)
        series = [{"day": d, "forms": by_day[d]["forms"],
                   "review_rate": round(by_day[d]["review"] / by_day[d]["fields"], 4) if by_day[d]["fields"] else None,
                   "auto_error_audited": (round(audited[d]["err"] / audited[d]["n"], 4) if audited[d]["n"] else None),
                   "audited_n": audited[d]["n"]} for d in days]
        levels: dict = defaultdict(lambda: {"L0": 0, "L1": 0, "L2": 0, "n": 0})
        for s in self.store.list_ledger():
            ft = s.key.split("|")[0]
            levels[ft][f"L{s.level}"] += 1; levels[ft]["n"] = max(levels[ft]["n"], s.n)
        secs = [f.review_seconds for f in forms if f.review_seconds is not None]
        per_field = [f.review_seconds / max(1, len(f.review_paths)) for f in forms if f.review_seconds is not None and f.review_paths]
        m = self.metrics()
        return {
            "kpi": {**m, "target_error_rate": self.router.target_error_rate,
                    "pending_proposals": len(self.store.list_proposals(status="pending")),
                    "review_seconds_median": round(median(secs), 1) if secs else None,
                    "review_seconds_per_field_median": round(median(per_field), 1) if per_field else None,
                    "review_timed_forms": len(secs)},
            "daily": series,
            "ledger_levels": [{"field_type": ft, **v} for ft, v in sorted(levels.items())],
        }

    def _audit(self, form_id: str, event: str, detail: dict, actor: str = "agent") -> None:
        self.store.add_audit(AuditEvent(form_id=form_id, event=event, actor=actor, detail=detail))


class BudgetGuard:
    """1日の Gemini 呼び出し数と自動確定数を数え、段階的に縮退する（policy.yaml の budget）。

    normal    : 通常
    reduced   : 呼び出しが上限の degrade_at（既定 80%）以上 → 二重読み取り・再読み取りを省略（判定は台帳・ルーターのまま）
    exhausted : 呼び出しが上限以上、または自動確定数が上限以上 → 自動確定を停止し全件を人へ（読み取りは単読で継続）
    カウンタはプロセス内（Cloud Run の複数インスタンスでは近似）。日付（JST）が変わるとリセット。
    """

    def __init__(self, cfg: dict, count_calls: bool = True):
        from datetime import timedelta, timezone
        self.max_calls = int((cfg or {}).get("max_gemini_calls", 0) or 0)
        self.max_auto = int((cfg or {}).get("max_auto_accepts", 0) or 0)
        self.degrade_at = float((cfg or {}).get("degrade_at", 0.8))
        self.count_calls = count_calls
        self._jst = timezone(timedelta(hours=9))
        self._day = self._today()
        self.calls = 0
        self.auto = 0
        self._last_mode = "normal"

    def _today(self):
        from datetime import datetime
        return datetime.now(self._jst).date()

    def _roll(self):
        if self._today() != self._day:
            self._day, self.calls, self.auto = self._today(), 0, 0

    def add_call(self, n: int = 1):
        self._roll(); self.calls += n

    def add_auto(self, n: int):
        self._roll(); self.auto += n

    def mode(self) -> tuple[str, bool]:
        self._roll()
        m = "normal"
        if self.max_calls and self.calls >= self.max_calls:
            m = "exhausted"
        elif self.max_auto and self.auto >= self.max_auto:
            m = "exhausted"
        elif self.max_calls and self.calls >= self.max_calls * self.degrade_at:
            m = "reduced"
        changed = m != self._last_mode
        self._last_mode = m
        return m, changed

    def snapshot(self) -> dict:
        self._roll()
        return {"date": self._day.isoformat(), "gemini_calls": self.calls, "max_gemini_calls": self.max_calls,
                "auto_accepts": self.auto, "max_auto_accepts": self.max_auto, "degrade_at": self.degrade_at}


class _CountingExtractor:
    """抽出器をラップして Gemini 呼び出し回数を数える（extract / extract_field）。"""

    def __init__(self, inner, budget: BudgetGuard):
        self._inner, self._budget = inner, budget
        self.name = inner.name
        self.model = getattr(inner, "model", "")

    def extract(self, *args, **kwargs):
        self._budget.add_call(); return self._inner.extract(*args, **kwargs)

    def extract_field(self, *args, **kwargs):
        self._budget.add_call(); return self._inner.extract_field(*args, **kwargs)

    def __getattr__(self, item):
        return getattr(self._inner, item)


def _norm(x) -> str:
    return str(x if x is not None else "").replace(" ", "").replace("　", "").replace("-", "").upper()
