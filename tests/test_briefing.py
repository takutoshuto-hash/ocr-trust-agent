"""朝のブリーフィング: 直近の運用まとめと、提案を質問の形にする。"""
import tempfile
from pathlib import Path

from app.config import settings
from app.extract.mock import MockExtractor
from app.learn import CorrectionRouter
from app.pipeline import Pipeline
from app.reflect import analyze
from app.schemas import OrderForm, TrainingRecord
from app.store import MemoryStore
from app.trust import Policy


def _rec(ft, extracted, final):
    return TrainingRecord(form_id="f", path=ft, field_type=ft, sender_id="S1", format_id="fax_v1", extracted=extracted,
                          final=final, corrected=extracted != final, was_auto=False, verified=True, features={})


def test_briefing_summarizes_and_asks():
    store = MemoryStore()
    router = CorrectionRouter(Path(tempfile.mkdtemp()), min_samples=10_000, target_error_rate=0.005)
    pipe = Pipeline(store=store, extractor=MockExtractor(), policy=Policy.load(settings.policy_path), router=router, seed=1, budget_enabled=False)
    truth = OrderForm.model_validate({"applicant": {"name": "田中 太郎", "name_kana": "タナカ タロウ", "zip": "870-0001",
                                                    "address": "大分県大分市王子北町1-2-3", "phone": "097-555-1234"},
                                      "deliveries": [{"name": "鈴木 花子", "name_kana": "スズキ ハナコ", "zip": "150-0001",
                                                      "address": "東京都渋谷区神宮前1-1-1", "phone": "03-3000-1000",
                                                      "product_code": "BMN-50", "qty": 2}]})
    fd = pipe.process(b"img-1", sender_id="S1", hint=truth)
    pipe.confirm(fd.form_id, {p: truth.flatten()[p] for p in fd.review_paths})
    b = pipe.briefing()
    assert b["forms"] == 1 and b["fields"] == len(fd.decisions) and b["review_rate"] == 1.0
    assert "1 枚" in b["headline"] and b["questions"] == [] and b["active_rules"] == 0
    # 提案があれば質問になる
    recs = [_rec("deliveries.zip", "870-0007", "870-0001")] * 4 + [_rec("deliveries.name", "x", "x")] * 10
    pipe.reflection.propose(analyze(recs, []))
    b = pipe.briefing()
    assert b["questions"] and all("？" in q["ask"] for q in b["questions"])
    assert "判断をお願いしたいこと" in b["headline"]
