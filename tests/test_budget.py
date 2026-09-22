"""予算縮退: normal → reduced（二重読み取り省略）→ exhausted（自動確定停止）。"""
import tempfile
from pathlib import Path

from app.config import settings
from app.extract.mock import MockExtractor
from app.learn import CorrectionRouter
from app.pipeline import BudgetGuard, Pipeline, _CountingExtractor
from app.schemas import FieldStatus, OrderForm
from app.store import MemoryStore
from app.trust import Policy

TRUTH = OrderForm.model_validate({"applicant": {"name": "田中 太郎", "name_kana": "タナカ タロウ", "zip": "870-0001",
                                                "address": "大分県大分市王子北町1-2-3", "phone": "097-555-1234"},
                                  "deliveries": [{"name": "鈴木 花子", "name_kana": "スズキ ハナコ", "zip": "150-0001",
                                                  "address": "東京都渋谷区神宮前1-1-1", "phone": "03-3000-1000",
                                                  "product_code": "BMN-50", "qty": 2}]})


def test_budget_modes():
    b = BudgetGuard({"max_gemini_calls": 10, "max_auto_accepts": 5, "degrade_at": 0.8})
    assert b.mode()[0] == "normal"
    b.add_call(8); assert b.mode()[0] == "reduced"
    b.add_call(2); assert b.mode()[0] == "exhausted"
    b2 = BudgetGuard({"max_gemini_calls": 100, "max_auto_accepts": 3})
    b2.add_auto(3); assert b2.mode()[0] == "exhausted"


def test_pipeline_degrades_and_audits():
    policy = Policy.load(settings.policy_path)
    policy.raw["budget"] = {"max_gemini_calls": 3, "max_auto_accepts": 100, "degrade_at": 0.5}
    router = CorrectionRouter(Path(tempfile.mkdtemp()), min_samples=10**6)
    pipe = Pipeline(store=MemoryStore(), extractor=MockExtractor(error_scale=0.0), policy=policy, router=router, seed=1)
    # モックは既定では呼び出しを数えないので、テストでは数えるように差し替える
    pipe.budget = BudgetGuard(policy.raw["budget"], count_calls=True)
    pipe.extractor = _CountingExtractor(pipe.extractor, pipe.budget); pipe.resolver.extractor = pipe.extractor

    fd1 = pipe.process(b"a", sender_id="S1", hint=TRUTH)                 # 2 calls → normal（二重読み取りあり）
    assert all(v.agreement is not None for v in fd1.verdicts.values())
    fd2 = pipe.process(b"b", sender_id="S1", hint=TRUTH)                 # 2 calls ≥ 3*0.5 → reduced: 単読（計3 calls）
    assert all(v.agreement is None for v in fd2.verdicts.values())
    fd3 = pipe.process(b"c", sender_id="S1", hint=TRUTH)                 # 3 ≥ 3 → exhausted: 自動確定なし
    assert all(d.status == FieldStatus.REVIEW for d in fd3.decisions.values())
    assert any(e.event == "budget_state" and e.detail["mode"] == "exhausted" for e in pipe.store.list_audit())
    assert pipe.metrics()["budget"]["mode"] == "exhausted"
