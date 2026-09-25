"""確認画面の「要確認の理由」は現場の事務担当者の言葉で書く。検証名・自律レベル・内部値は出さない。"""
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.labels import plain_check, plain_decision, plain_verdict
from app.schemas import CheckResult, CheckStatus, FieldVerdict


def test_plain_verdict_wording():
    v = FieldVerdict(path="deliveries[0].zip", field_type="deliveries.zip", agreement=False, checks=[
        CheckResult(name="zip_format", status=CheckStatus.PASS),
        CheckResult(name="zip_address", status=CheckStatus.FAIL, detail="郵便番号は「大分県大分市」だが住所に含まれない"),
        CheckResult(name="history", status=CheckStatus.UNKNOWN, detail="履歴なし"),
    ])
    s = plain_verdict(v)
    assert s == "郵便番号と住所が合いません（郵便番号は「大分県大分市」ですが、住所に含まれない）。2 回読んで結果が違いました"
    assert plain_check(CheckResult(name="product_exists", status=CheckStatus.FAIL, detail="マスタに無い。近い候補: ['BMN-50', 'BMN-56']")) \
        == "商品番号が商品一覧にありません（近い番号: BMN-50, BMN-56）"
    assert plain_check(CheckResult(name="blank_zone", status=CheckStatus.FAIL, detail="欄は空欄（インク率 0.0012）なのに値 'x' が返った → ハルシネーション疑い")) \
        == "欄は空欄に見えるのに値が読み取られています（読み取りの作り話の疑い）"
    # 合格だけの検証は何も書かない（「検証すべて合格」を赤字で出さない）
    assert plain_verdict(FieldVerdict(path="p", field_type="deliveries.qty", agreement=True, checks=[CheckResult(name="qty_range", status=CheckStatus.PASS)])) == ""


def test_plain_decision_wording():
    kw = dict(audit=False, forced_new_sender=False, sender_n=0, router_trained=False, ledger_n=3, judge_ok=True, agreement=True)
    assert plain_decision(status="review", **{**kw, "forced_new_sender": True}).startswith("初めての送り主なので")
    assert "実績が足りません" in plain_decision(status="review", **kw)
    assert plain_decision(status="auto", **{**kw, "audit": True}) == "自動で確定しましたが、念のための抜き取り確認です"
    assert plain_decision(status="review", **{**kw, "judge_ok": False}) == ""       # 検証の失敗は plain_verdict が説明する


def test_review_page_has_no_jargon():
    from app.main import app, pipeline
    from app.schemas import OrderForm
    truth = OrderForm.model_validate({"applicant": {"name": "田中 太郎", "name_kana": "タナカ タロウ", "zip": "870-0001",
                                                    "address": "大分県大分市王子北町1-2-3", "phone": "097-555-1234"},
                                      "deliveries": [{"name": "鈴木 花子", "name_kana": "スズキ ハナコ", "zip": "150-0001",
                                                      "address": "東京都渋谷区神宮前1-1-1", "phone": "03-3000-1000",
                                                      "product_code": "BMN-50", "qty": 2}]})
    fd = pipeline.process(b"wording-test-image", sender_id="S-wording", hint=truth)
    assert fd.needs_review
    c = TestClient(app)
    html = c.get(f"/review/{fd.form_id}").text
    for jargon in ("L0", "L1", "L2", "zip_address", "name_reading", "blank_zone", "検証すべて合格", "ルーター", "閾値", "ポリシー",
                   "new_sender_review", "ハルシネーション", "【オフライン】", "streak", "修正率="):
        assert jargon not in html, jargon
    assert "初めての送り主なので" in html or "実績が足りません" in html
    assert "原本と見比べてください" in html
    queue = c.get("/review").text
    for jargon in ("教師データ", "ルーター", "監査実測"):
        assert jargon not in queue, jargon
