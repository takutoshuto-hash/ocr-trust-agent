from app.judge import Judge
from app.judge import tools as T
from app.schemas import CheckStatus, Extraction, FieldValue, OrderForm


def _ex(flat: dict) -> Extraction:
    form = OrderForm.from_flat(flat)
    return Extraction(form=form, fields={p: FieldValue(path=p, value=v) for p, v in form.flatten().items()})


def test_zip_address_pass_and_fail():
    assert T.check_zip_address("870-0001", "大分県大分市王子北町1-2-3").status == CheckStatus.PASS
    assert T.check_zip_address("870-0001", "福岡県福岡市中央区天神1-1").status == CheckStatus.FAIL
    assert T.check_zip_address("999-9999", "どこか").status == CheckStatus.UNKNOWN


def test_product_and_phone():
    assert T.check_product_code("BMN-50").status == CheckStatus.PASS
    r = T.check_product_code("BMN-58")
    assert r.status == CheckStatus.FAIL and "BMN-50" in r.detail
    assert T.check_phone_format("097-555-1234").status == CheckStatus.PASS
    assert T.check_phone_format("97-555-1234").status == CheckStatus.FAIL


def test_judge_agreement_and_reason():
    base = {"applicant.name": "田中 太郎", "applicant.name_kana": "タナカ タロウ", "applicant.zip": "870-0001",
            "applicant.address": "大分県大分市王子北町1-2-3", "applicant.phone": "097-555-1234", "applicant.organization": "",
            "deliveries[0].name": "鈴木 花子", "deliveries[0].name_kana": "スズキ ハナコ", "deliveries[0].zip": "150-0001",
            "deliveries[0].address": "東京都渋谷区神宮前1-1-1", "deliveries[0].phone": "03-3000-1000",
            "deliveries[0].product_code": "BMN-50", "deliveries[0].qty": 2, "deliveries[0].noshi_name": "御中元"}
    other = dict(base, **{"deliveries[0].name": "鈴本 花子"})
    v = Judge().judge(_ex(base), _ex(other))
    assert v["applicant.zip"].all_pass and v["applicant.zip"].agreement is True
    assert v["deliveries[0].name"].agreement is False and "2 回読んで結果が違いました" in v["deliveries[0].name"].reason
    assert not v["deliveries[0].product_code"].any_fail
