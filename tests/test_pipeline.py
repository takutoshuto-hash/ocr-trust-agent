import json
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data" / "synthetic"))
from generate_forms import load_master, make_truth, phone  # noqa: E402

from app.config import settings
from app.extract.mock import MockExtractor
from app.learn import CorrectionRouter
from app.pipeline import Pipeline
from app.schemas import FieldStatus, OrderForm
from app.store import MemoryStore
from app.trust import Policy


def _pipe():
    router = CorrectionRouter(Path(tempfile.mkdtemp()), min_samples=200, target_error_rate=0.01)
    return Pipeline(store=MemoryStore(), extractor=MockExtractor(), policy=Policy.load(settings.policy_path), router=router, seed=1, budget_enabled=False)


def test_first_form_is_all_review_and_confirm_learns():
    pipe = _pipe()
    truth = OrderForm.model_validate({"applicant": {"name": "田中 太郎", "name_kana": "タナカ タロウ", "zip": "870-0001",
                                                    "address": "大分県大分市王子北町1-2-3", "phone": "097-555-1234"},
                                      "deliveries": [{"name": "鈴木 花子", "name_kana": "スズキ ハナコ", "zip": "150-0001",
                                                      "address": "東京都渋谷区神宮前1-1-1", "phone": "03-3000-1000",
                                                      "product_code": "BMN-50", "qty": 2}]})
    fd = pipe.process(b"img-1", sender_id="S1", hint=truth)
    assert fd.needs_review and all(d.status == FieldStatus.REVIEW for d in fd.decisions.values())
    tflat = truth.flatten()
    fd2 = pipe.confirm(fd.form_id, {p: tflat[p] for p in fd.review_paths})
    assert fd2.status == "confirmed" and fd2.final == truth
    assert pipe.store.count_training() == len(fd.decisions)
    assert any(e.event == "confirmed" for e in pipe.store.list_audit(fd.form_id))


def test_review_rate_drops_with_volume():
    """量をさばくと、誤りの少ない項目種別（郵便番号・商品番号・数量）から自動確定が広がり、自動確定の誤りは目標内に収まる。
    氏名・住所など誤り 3% 前後の項目は、相互検証が誤りを捕まえるまでは要確認のままなのが正しい挙動
    （10 日規模の曲線は eval/simulate_days.py で見る）。"""
    pipe = _pipe()
    rng = random.Random(3)
    zips, products = load_master()
    pool = [{"id": f"S{i}", "phone": phone(rng)} for i in range(20)]
    reliable = {"deliveries.zip", "deliveries.product_code", "deliveries.qty", "applicant.zip"}
    auto_n = auto_wrong = 0

    def run_batch(k):
        nonlocal auto_n, auto_wrong
        review = fields = 0
        for i in range(k):
            td, sender = make_truth(rng, zips, products, pool)
            truth = OrderForm.model_validate(td)
            fd = pipe.process(json.dumps(td).encode() + bytes([i % 250]), sender_id=sender, hint=truth)
            tflat = truth.flatten()
            for p, d in fd.decisions.items():
                if d.field_type in reliable:
                    fields += 1; review += d.status == FieldStatus.REVIEW
                if d.status == FieldStatus.AUTO:
                    auto_n += 1; auto_wrong += str(d.value).replace(" ", "") != str(tflat[p]).replace(" ", "")
            pipe.confirm(fd.form_id, {p: tflat[p] for p in fd.review_paths})
        return review / fields

    first = run_batch(60)
    pipe.retrain()
    run_batch(60)
    summary = pipe.retrain()
    last = run_batch(60)
    assert summary["trained"], summary
    assert last < 0.5 and last < first, (first, last)
    assert auto_n > 100 and auto_wrong / auto_n < 0.01, (auto_n, auto_wrong)
    m = pipe.metrics()
    assert m["training_records"] > 0 and m["router"]["trained_on"] > 0
