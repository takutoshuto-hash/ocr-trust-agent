"""ハルシネーション対策（空欄検知・根拠整合）とデータ最小化のテスト。"""
import io
import json
import random
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data" / "synthetic"))
from generate_forms import fonts, load_master, make_truth, phone, render  # noqa: E402

from app.extract.mock import MockExtractor
from app.judge import Judge
from app.judge import tools as T
from app.judge.zones import ink_ratio, load_zones, mask_zones, open_image
from app.schemas import CheckStatus, OrderForm


def _synthetic(seed=5, blank_rate=1.0):
    rng = random.Random(seed)
    zips, products = load_master()
    truth, sender = make_truth(rng, zips, products, [{"id": "S1", "phone": phone(rng)}], blank_rate=blank_rate)
    buf = io.BytesIO(); render(truth, fonts(), rng).save(buf, format="PNG")
    return OrderForm.model_validate(truth), buf.getvalue()


def test_ink_ratio_distinguishes_blank_and_filled():
    truth, png = _synthetic()
    img, zones = open_image(png), load_zones("fax_v1")
    filled = ink_ratio(img, zones["applicant.name"])
    blank = ink_ratio(img, zones["applicant.name_kana"])   # blank_rate=1.0 → フリガナは必ず空欄
    assert filled > 0.008 > blank, (filled, blank)


def test_blank_zone_flags_invented_value():
    r = T.check_blank_zone(0.0005, "御中元", 0.004)
    assert r.status == CheckStatus.FAIL and "ハルシネーション" in r.detail
    assert T.check_blank_zone(0.0005, "", 0.004).status == CheckStatus.PASS
    assert T.check_blank_zone(0.02, "", 0.004).status == CheckStatus.UNKNOWN
    assert T.check_blank_zone(None, "x", 0.004).status == CheckStatus.UNKNOWN


def test_evidence_mismatch():
    assert T.check_evidence("870-0001", "８７０−０００１", 0.5).status == CheckStatus.PASS
    assert T.check_evidence("田中 太郎", "山田花子", 0.5).status == CheckStatus.FAIL


def test_judge_catches_mock_hallucination_on_real_image():
    """空欄だらけの帳票をモックで読むと創作値が出る → 空欄検知で必ず FAIL になる。"""
    truth, png = _synthetic(seed=11)
    ex = MockExtractor(error_scale=1.0).extract(png, hint=truth, variant=0)
    verdicts = Judge().judge(ex, image=png, format_id="fax_v1")
    tflat = truth.flatten()
    invented = [p for p, v in ex.form.flatten().items() if isinstance(v, str) and v and not tflat[p]]
    assert invented, "モックが創作値を出す前提のテスト（乱数シード依存）"
    for p in invented:
        assert any(c.name == "blank_zone" and c.status == CheckStatus.FAIL for c in verdicts[p].checks), p


def test_mask_zones_whitens_area():
    truth, png = _synthetic(seed=3, blank_rate=0.0)
    zones = load_zones("fax_v1")
    masked = mask_zones(png, [zones["applicant.phone"]])
    assert ink_ratio(open_image(masked), zones["applicant.phone"]) < 0.001
    assert ink_ratio(open_image(masked), zones["applicant.name"]) > 0.004
