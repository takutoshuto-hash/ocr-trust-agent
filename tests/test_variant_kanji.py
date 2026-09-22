from app.extract.gemini import _to_extraction
from app.judge import tools as T
from app.schemas import CheckStatus


def test_restore_variant_kanji():
    assert T.restore_variant_kanji("高田 太郎", "髙田 太郎") == ("髙田 太郎", ["高→髙"])
    assert T.restore_variant_kanji("山崎 隆", "山﨑隆") == ("山﨑 隆", ["崎→﨑"])
    assert T.restore_variant_kanji("高田 太郎", "高田 太郎") == ("高田 太郎", [])
    assert T.restore_variant_kanji("高田", "髙田太郎")[1] == []     # 長さが違えば触らない
    assert T.check_variant_kanji("高田 太郎", "髙田 太郎").status == CheckStatus.FAIL
    assert T.check_variant_kanji("髙田 太郎", "髙田 太郎").status == CheckStatus.PASS


def test_extraction_restores_variant_from_evidence():
    data = {"applicant": {k: {"value": "", "confidence": 1, "evidence": ""} for k in ["name", "name_kana", "zip", "address", "phone", "organization"]},
            "deliveries": [], "raw_text": ""}
    data["applicant"]["name"] = {"value": "高田 太郎", "confidence": 0.9, "evidence": "髙田 太郎"}
    ex = _to_extraction(data, model="gemini", latency_ms=1)
    assert ex.form.applicant.name == "髙田 太郎"
    assert ex.fields["applicant.name"].value == "髙田 太郎"
