"""項目間の相互検証（市外局番↔都道府県、姓↔読み）と、欄切り出しの二重読み取り入力のテスト。"""
import io
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data" / "synthetic"))
from generate_forms import fonts, load_master, make_truth, phone, render  # noqa: E402

from app.judge import tools as T
from app.judge.core import Judge
from app.schemas import CheckStatus, OrderForm
from app.extract.mock import MockExtractor


def test_phone_area_matches_prefecture():
    assert T.check_phone_area("097-555-1234", "870-0001").status == CheckStatus.PASS        # 大分
    assert T.check_phone_area("03-3000-1000", "870-0001").status == CheckStatus.FAIL         # 東京の局番 × 大分の郵便番号
    assert T.check_phone_area("090-1234-5678", "870-0001").status == CheckStatus.UNKNOWN     # 携帯
    assert T.check_phone_area("", "870-0001").status == CheckStatus.UNKNOWN
    assert T.check_phone_area("097-555-1234", "000-0000").status == CheckStatus.UNKNOWN      # 郵便番号がマスタに無い


def test_name_reading_consistency():
    assert T.check_name_reading("田中 太郎", "タナカ タロウ").status == CheckStatus.PASS
    assert T.check_name_reading("田中 太郎", "タナ力 タロウ").status == CheckStatus.FAIL      # 力（漢字）に誤読
    assert T.check_name_reading("髙田 花子", "タカダ ハナコ").status == CheckStatus.PASS      # 異体字も辞書に
    assert T.check_name_reading("山崎 隆", "ヤマサキ タカシ").status == CheckStatus.PASS      # 複数読み
    assert T.check_name_reading("珍名 太郎", "チンメイ タロウ").status == CheckStatus.UNKNOWN  # 辞書に無い姓
    assert T.check_name_reading("田中 太郎", "").status == CheckStatus.UNKNOWN


def test_judge_wires_cross_checks():
    truth = OrderForm.model_validate({"applicant": {"name": "田中 太郎", "name_kana": "タナカ タロウ", "zip": "870-0001",
                                                    "address": "大分県大分市王子北町1-2-3", "phone": "03-3000-1000"},
                                      "deliveries": [{"name": "鈴木 花子", "name_kana": "サトウ ハナコ", "zip": "150-0001",
                                                      "address": "東京都渋谷区神宮前1-1-1", "phone": "03-3000-1000",
                                                      "product_code": "BMN-50", "qty": 2}]})
    ex = MockExtractor(error_scale=0.0).extract(b"x", hint=truth)
    v = Judge().judge(ex)
    assert any(c.name == "phone_area" and c.status == CheckStatus.FAIL for c in v["applicant.phone"].checks)
    assert any(c.name == "phone_area" and c.status == CheckStatus.PASS for c in v["deliveries[0].phone"].checks)
    assert any(c.name == "name_reading" and c.status == CheckStatus.FAIL for c in v["deliveries[0].name_kana"].checks)
    assert any(c.name == "name_reading" and c.status == CheckStatus.FAIL for c in v["deliveries[0].name"].checks)
    assert any(c.name == "name_reading" and c.status == CheckStatus.PASS for c in v["applicant.name"].checks)


def test_synthetic_phones_are_consistent_with_prefecture():
    rng = random.Random(7)
    zips, products = load_master()
    pool = [{"id": f"S{i}", "phone": phone(rng)} for i in range(5)]
    fails = landline = 0
    for _ in range(60):
        td, _ = make_truth(rng, zips, products, pool)
        for blk in [td["applicant"], *td["deliveries"]]:
            if not blk["phone"]:
                continue
            r = T.check_phone_area(blk["phone"], blk["zip"])
            fails += r.status == CheckStatus.FAIL
            landline += r.status == CheckStatus.PASS
    assert fails == 0 and landline > 20, (fails, landline)


def test_zone_parts_and_empty_delivery_dropping():
    from app.extract.gemini import _to_extraction, zone_parts
    rng = random.Random(3)
    zips, products = load_master()
    td, _ = make_truth(rng, zips, products, [{"id": "S1", "phone": phone(rng)}])
    buf = io.BytesIO(); render(td, fonts(), rng).save(buf, format="PNG")
    parts = zone_parts(buf.getvalue(), "fax_v1")
    assert len(parts) == 60 and isinstance(parts[0], str) and "applicant.zip" in parts[0]   # 30 欄 × (ラベル + 画像)
    assert zone_parts(buf.getvalue(), "no_such_format") == []
    empty = {k: {"value": "", "confidence": 0, "evidence": ""} for k in ["name", "name_kana", "zip", "address", "phone", "product_code", "qty", "noshi_name"]}
    filled = dict(empty, name={"value": "鈴木 花子", "confidence": 0.9, "evidence": "鈴木 花子"})
    ex = _to_extraction({"applicant": {}, "deliveries": [filled, empty, empty], "raw_text": ""}, model="m", latency_ms=0)
    assert len(ex.form.deliveries) == 1
