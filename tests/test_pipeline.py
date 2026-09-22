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
    pipe = _pipe()
    rng = random.Random(3)
    zips, products = load_master()
    pool = [{"id": f"S{i}", "phone": phone(rng)} for i in range(20)]

    def run_batch(k):
        review = fields = 0
        for i in range(k):
            td, sender = make_truth(rng, zips, products, pool)
            truth = OrderForm.model_validate(td)
            fd = pipe.process(json.dumps(td).encode() + bytes([i % 250]), sender_id=sender, hint=truth)
            tflat = truth.flatten()
            fields += len(fd.decisions); review += len(fd.strict_review_paths)
            pipe.confirm(fd.form_id, {p: tflat[p] for p in fd.review_paths})
        return review / fields

    early = run_batch(60)
    summary = pipe.retrain()
    late = run_batch(60)
    assert summary["trained"], summary
    # 最初の60枚でも台帳（検証合格の実績）が zip/phone/qty を L1 に上げ始めるので 1.0 にはならない。
    # ルーターは Kish 有効標本数で保守的に閾値を決めるため、60枚後の低下は緩やか（10日規模の曲線は eval/simulate_days.py で見る）
    assert early > 0.4, early
    assert late < early - 0.05, (early, late)
    m = pipe.metrics()
    assert m["training_records"] > 0 and m["router"]["trained_on"] > 0
