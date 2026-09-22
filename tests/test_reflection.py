"""振り返りエージェント: 集計 → 提案 → 承認でルール注入／ポリシー上書き、範囲外は自動却下。"""
import tempfile
from pathlib import Path

from app.config import settings
from app.extract.mock import MockExtractor
from app.learn import CorrectionRouter
from app.pipeline import Pipeline
from app.reflect import analyze
from app.reflect.agent import ReflectionAgent
from app.schemas import AuditEvent, ProposalKind, TrainingRecord
from app.store import MemoryStore
from app.trust import Policy


def _rec(ft, extracted, final, sender="S1", was_auto=False):
    return TrainingRecord(form_id="f", path=ft, field_type=ft, sender_id=sender, format_id="fax_v1", extracted=extracted,
                          final=final, corrected=extracted != final, was_auto=was_auto, verified=True, features={})


def test_analysis_finds_confusions_and_auto_misses():
    recs = [_rec("deliveries.zip", "870-0007", "870-0001")] * 4 + [_rec("deliveries.zip", "150-0001", "150-0001")] * 10
    recs += [_rec("deliveries.name", "田中 大郎", "田中 太郎", was_auto=True)] * 2
    a = analyze(recs, [AuditEvent(form_id="f", event="resolved", detail={"actions": [{"action": "reread_zone", "accepted": True}]})])
    top = a["confusions_top"][0]
    assert (top["from"], top["to"], top["count"]) == ("7", "1", 4)
    assert a["auto_missed"]["count"] == 2
    assert a["resolver_actions"]["reread_zone"]["accepted"] == 1


def test_rules_planner_proposes_and_approval_applies():
    store = MemoryStore()
    policy = Policy.load(settings.policy_path)
    agent = ReflectionAgent(store, policy.raw, planner="rules")
    recs = [_rec("deliveries.zip", "870-0007", "870-0001")] * 4 + [_rec("deliveries.name", "x", "y", was_auto=True)] * 2
    props = agent.propose(analyze(recs, []))
    kinds = {p.kind for p in props}
    assert ProposalKind.RULE in kinds and ProposalKind.POLICY in kinds
    assert all(p.status == "pending" for p in props)
    assert not store.list_rules() and not store.get_policy_overrides()      # 承認前は何も変わらない
    rule = next(p for p in props if p.kind == ProposalKind.RULE)
    pol = next(p for p in props if p.kind == ProposalKind.POLICY)
    agent.decide(rule.proposal_id, True, "human:test")
    agent.decide(pol.proposal_id, True, "human:test")
    assert store.list_rules()[0].text == rule.rule_text
    assert store.get_policy_overrides()["audit_sampling_rate"] == pol.policy_to
    assert policy.raw["audit_sampling_rate"] == pol.policy_to
    # 同じ提案は重複しない
    assert agent.propose(analyze(recs, [])) == [] or all(p.title != rule.title for p in agent.propose(analyze(recs, [])))


def test_governance_rejects_out_of_range_and_unknown_keys():
    store = MemoryStore()
    agent = ReflectionAgent(store, {"audit_sampling_rate": 0.02}, planner="rules")
    p1 = agent._to_proposal({"kind": "policy", "policy_key": "audit_sampling_rate", "policy_to": 0.9, "title": "t"}, "x")
    p2 = agent._to_proposal({"kind": "policy", "policy_key": "budget.max_gemini_calls", "policy_to": 10, "title": "t"}, "x")
    assert p1.status == "rejected" and p2.status == "rejected"


def test_pipeline_reflect_and_rules_injected():
    router = CorrectionRouter(Path(tempfile.mkdtemp()), min_samples=10**6)
    pipe = Pipeline(store=MemoryStore(), extractor=MockExtractor(), policy=Policy.load(settings.policy_path), router=router, seed=1)
    pipe.store.add_training([_rec("deliveries.zip", "870-0007", "870-0001")] * 5)
    out = pipe.reflect(days=1)
    assert out["analysis"]["window_records"] == 5 and out["proposals"]
    pid = out["proposals"][0]["proposal_id"]
    pipe.decide_proposal(pid, True, "human:test")
    assert any(e.event == "proposal_approved" for e in pipe.store.list_audit())
    assert len(pipe.store.list_rules(scope="format:fax_v1")) == 1
