"""行動するエージェント（Resolver）のテスト。ルールプランナー＋決定的な採否。"""
import tempfile
from pathlib import Path

from app.config import settings
from app.extract.mock import MockExtractor
from app.judge import Judge
from app.learn import CorrectionRouter
from app.pipeline import Pipeline
from app.resolve import Resolver
from app.resolve.actions import FieldContext, act_complete_address_from_zip, act_nearest_product_code
from app.schemas import Extraction, FieldValue, OrderForm
from app.store import MemoryStore
from app.trust import Policy


def _ex(flat: dict) -> Extraction:
    form = OrderForm.from_flat(flat)
    return Extraction(form=form, fields={p: FieldValue(path=p, value=v, evidence=str(v)) for p, v in form.flatten().items()}, model="mock")


BASE = {"applicant.name": "田中 太郎", "applicant.name_kana": "タナカ タロウ", "applicant.zip": "870-0001",
        "applicant.address": "大分県大分市王子北町1-2-3", "applicant.phone": "097-555-1234", "applicant.organization": "",
        "deliveries[0].name": "鈴木 花子", "deliveries[0].name_kana": "スズキ ハナコ", "deliveries[0].zip": "150-0001",
        "deliveries[0].address": "東京都渋谷区神宮前1-1-1", "deliveries[0].phone": "03-3000-1000",
        "deliveries[0].product_code": "BMN-50", "deliveries[0].qty": 2, "deliveries[0].noshi_name": "御中元"}


def test_nearest_product_code_unique():
    ctx = FieldContext(path="deliveries[0].product_code", field_type="deliveries.product_code", value="BMN-58", evidence="",
                       secondary_value=None, reasons=[], sibling={}, image=None, format_id="fax_v1")
    c = act_nearest_product_code(ctx)
    assert c and c.value == "BMN-50"


def test_complete_address_from_zip_fixes_prefix():
    ctx = FieldContext(path="deliveries[0].address", field_type="deliveries.address", value="東京都渋合区神宮前1-1-1", evidence="",
                       secondary_value=None, reasons=[], sibling={"zip": "150-0001"}, image=None, format_id="fax_v1")
    c = act_complete_address_from_zip(ctx)
    assert c and c.value.startswith("東京都渋谷区神宮前") and c.value.endswith("1-1-1")


def test_complete_address_refuses_when_zip_disagrees_with_the_whole_address():
    """読み違えた郵便番号（870-0001 → 810-0001）で住所を書き換えない。住所側に同じ町域・市区町村が無ければ補完しない。"""
    ctx = FieldContext(path="deliveries[0].address", field_type="deliveries.address", value="大分県大分市王子北町2-19-17", evidence="",
                       secondary_value=None, reasons=[], sibling={"zip": "810-0001"}, image=None, format_id="fax_v1")
    assert act_complete_address_from_zip(ctx) is None


def test_resolver_repairs_product_code_and_address():
    bad = dict(BASE, **{"deliveries[0].product_code": "BMN-58", "deliveries[0].address": "東京都渋合区神宮前1-1-1"})
    ex1, ex2 = _ex(bad), _ex(bad)
    judge = Judge()
    verdicts = judge.judge(ex1, ex2)
    assert verdicts["deliveries[0].product_code"].any_fail and verdicts["deliveries[0].address"].any_fail
    r = Resolver(MockExtractor(), judge, {"enabled": True, "max_per_form": 6}, planner="rules")
    updates, log = r.resolve(ex1, ex2, verdicts, image=None, format_id="fax_v1")
    assert updates["deliveries[0].product_code"] == "BMN-50"
    assert updates["deliveries[0].address"].startswith("東京都渋谷区")
    assert any(e.get("accepted") for e in log)


def test_resolver_respects_budget_and_premium_policy():
    bad = dict(BASE, **{"deliveries[0].product_code": "XXX-99"})   # 近似一致なし → 再読み取りも hint なしで候補なし
    ex1 = _ex(bad)
    judge = Judge()
    verdicts = judge.judge(ex1, None)
    r = Resolver(MockExtractor(), judge, {"enabled": True, "max_per_form": 1, "allow_premium": False}, planner="rules")
    updates, log = r.resolve(ex1, None, verdicts, image=None, format_id="fax_v1")
    assert not updates
    assert log[-1]["action"] == "escalate_to_human"
    assert not any(e.get("action") == "reread_premium" and "candidate" in e for e in log)


def test_pipeline_audits_resolution():
    router = CorrectionRouter(Path(tempfile.mkdtemp()), min_samples=10**6)
    pipe = Pipeline(store=MemoryStore(), extractor=MockExtractor(error_scale=0.0), policy=Policy.load(settings.policy_path), router=router, seed=1)
    truth = OrderForm.from_flat(BASE)
    fd = pipe.process(b"img", sender_id="S1", hint=truth)
    events = [e.event for e in pipe.store.list_audit(fd.form_id)]
    assert "judged" in events and "decided" in events


def test_zone_secondary_requires_primary_agreement():
    """2回目が欄切り出しのとき、欄再読み取りが2回目と一致しても（相関あり）採用せず、1回目と一致したときだけ採用する。"""
    from app.resolve.actions import Candidate
    judge = Judge()
    r = Resolver(MockExtractor(), judge, {"enabled": True, "max_per_form": 6}, planner="rules")
    ctx = FieldContext(path="deliveries[0].name", field_type="deliveries.name", value="田中 太郎", evidence="", secondary_value="田中 大郎",
                       reasons=["二重読み取り不一致"], sibling={}, image=None, format_id="fax_v1", secondary_source="zones")
    flat = {"deliveries[0].name": "田中 太郎", "deliveries[0].name_kana": ""}
    ok, why = r._accept(ctx, Candidate(value="田中 大郎", source="reread_zone"), flat)
    assert not ok and "独立でない" in why
    ok, why = r._accept(ctx, Candidate(value="田中 太郎", source="reread_zone"), flat)
    assert ok and "1回目" in why
    ctx.secondary_source = "page"                       # 従来の全面×全面なら 2 回目との一致で採用
    ok, _ = r._accept(ctx, Candidate(value="田中 大郎", source="reread_zone"), flat)
    assert ok


def test_restore_variant_from_history():
    from app.resolve.actions import act_restore_from_history
    ctx = FieldContext(path="applicant.name", field_type="applicant.name", value="高田 花子", evidence="", secondary_value="高田 花子",
                       reasons=[], sibling={}, image=None, format_id="fax_v1", history_values=["髙田 花子"])
    c = act_restore_from_history(ctx)
    assert c is not None and c.value == "髙田 花子" and c.source == "restore_from_history"
    ctx.history_values = ["山田 花子"]
    assert act_restore_from_history(ctx) is None


def test_pipeline_restores_variant_kanji_from_confirmed_history():
    """常連の依頼主「髙田」を「高田」と読んでも、履歴照合が異体字差を検出し、行動するエージェントが確定値の字体に戻す。"""
    import tempfile
    from pathlib import Path
    from app.config import settings
    from app.learn import CorrectionRouter
    from app.pipeline import Pipeline
    from app.schemas import OrderForm, FieldStatus
    from app.store import MemoryStore
    from app.trust import Policy
    store = MemoryStore()
    router = CorrectionRouter(Path(tempfile.mkdtemp()), min_samples=10_000, target_error_rate=0.005)
    pipe = Pipeline(store=store, extractor=MockExtractor(error_scale=0.0), policy=Policy.load(settings.policy_path), router=router, seed=1,
                    budget_enabled=False)
    base = {"applicant": {"name": "髙田 花子", "name_kana": "タカダ ハナコ", "zip": "870-0001", "address": "大分県大分市王子北町1-2-3",
                          "phone": "097-555-1234", "organization": ""},
            "deliveries": [{"name": "鈴木 花子", "name_kana": "スズキ ハナコ", "zip": "150-0001", "address": "東京都渋谷区神宮前1-1-1",
                            "phone": "03-3000-1000", "product_code": "BMN-50", "qty": 2, "noshi_name": ""}]}
    truth = OrderForm.model_validate(base)
    fd = pipe.process(b"img-1", sender_id="S1", hint=truth)
    pipe.confirm(fd.form_id, {p: truth.flatten()[p] for p in fd.review_paths})
    wrong = OrderForm.model_validate({**base, "applicant": {**base["applicant"], "name": "高田 花子"}})   # 異体字が常用漢字に化けた読み
    fd2 = pipe.process(b"img-2", sender_id="S1", hint=wrong)
    d = fd2.decisions["applicant.name"]
    assert d.value == "髙田 花子" and d.resolved_from == "高田 花子", (d.value, d.reasons)
