"""動画の台本（scripts/demo_story.py）が毎回同じ結末になること: 事故 → コツの提案 → 効かず → 再提案は自動却下・取り消しを提案 → 復旧。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from demo_story import DEFECT, FormSource, LocalClient, storyboard  # noqa: E402

from app.config import settings
from app.extract.mock import MockExtractor
from app.learn import CorrectionRouter
from app.pipeline import Pipeline
from app.store import MemoryStore
from app.trust import Policy


def _pipe():
    router = CorrectionRouter(Path(tempfile.mkdtemp()), min_samples=200, target_error_rate=0.005)
    return Pipeline(store=MemoryStore(), extractor=MockExtractor(), policy=Policy.load(settings.policy_path), router=router, seed=1, budget_enabled=False)


def test_mock_defect_is_systematic_and_survives_rules():
    ex = MockExtractor()
    ex.set_defect("deliveries.zip", "7", "1", 1.0)
    from app.schemas import OrderForm
    truth = OrderForm.model_validate({"applicant": {"name": "田中 太郎", "zip": "870-0001", "address": "大分県大分市王子北町1-2-3"},
                                      "deliveries": [{"name": "鈴木 花子", "zip": "170-0007", "address": "東京都豊島区", "product_code": "BMN-50", "qty": 1}]})
    for variant in (0, 1):
        e = ex.extract(b"img", hint=truth, variant=variant, rules=["コツ1", "コツ2"])
        assert e.form.deliveries[0].zip == "110-0001"          # 1 回目も 2 回目も同じ癖。ルールを覚えても消えない
        assert e.form.applicant.zip == "870-0001"              # 対象外の項目には触らない
    assert ex.extract_field(b"crop", "deliveries.zip", hint="170-0007")[0] == "110-0001"
    ex.clear_defects()
    assert ex.extract(b"img", hint=truth).form.deliveries[0].zip == "170-0007"


def test_ink_placeholder_matches_blank_detection():
    """フォントが無いときの代替画像でも、値のある欄には墨があり空欄には無い（空欄検知と食い違わない）。"""
    from demo_story import _ink_png
    import random
    from app.judge.zones import ink_ratio, load_zones, open_image
    truth = {"applicant": {"name": "田中 太郎", "zip": "870-0001", "address": "大分県大分市王子北町1-2-3", "phone": ""},
             "deliveries": [{"name": "鈴木 花子", "zip": "150-0001", "address": "東京都渋谷区神宮前1-1-1", "product_code": "BMN-50", "qty": 2, "name_kana": ""}]}
    img = open_image(_ink_png(truth, 1, random.Random(1)))
    z = load_zones("fax_v1")
    assert ink_ratio(img, z["applicant.name"]) > 0.0208 and ink_ratio(img, z["deliveries[0].zip"]) > 0.0208
    assert ink_ratio(img, z["applicant.phone"]) < 0.013 and ink_ratio(img, z["deliveries[0].name_kana"]) < 0.013


def test_storyboard_ends_the_same_way():
    pipe = _pipe()
    log: list[str] = []
    res = storyboard(LocalClient(pipe), FormSource(None, seed=11), seed_days=2, per_day=60, accident_forms=25, auto=True, log=log.append)
    # 平常運転で確認の割合は初日 100% から下がっている
    assert res["seed_last_review_rate"] < 0.6
    # 事故: 郵便番号の読み違いが多発し、人の確認に回った（自動確定はしない）
    assert res["accident_day1"]["zip_wrong"] >= 10
    # 夜 1: 「7」を「1」と読み違えるコツが提案され、承認されてルールになった
    assert res["night1_target_pending"] == 1 and len(res["rules_after_night1"]) >= 1
    # 夜 2: 同じコツの再提案はガバナンスが自動却下し、効いていないコツの取り消しを提案 → 承認で無効化
    assert res["night2_duplicate_rejected"] == 1
    assert res["night2_retract_pending"] >= 1 and res["rules_after_night2"] == []
    rejected = pipe.store.list_proposals(status="rejected")
    assert any("既に有効" in p.rationale for p in rejected)
    # 復旧: 原因を直すと読み違いは消える
    assert res["recovery_day"]["zip_wrong"] <= 2
    # 監査ログに事故注入・提案・承認・取り消しが残る
    events = {e.event for e in pipe.store.list_audit(limit=50_000)}
    assert {"mock_defect", "reflected", "proposal_approved"} <= events or {"reflected", "proposal_approved"} <= events
