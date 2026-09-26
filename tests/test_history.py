"""町域までの郵便番号突合と、送り主履歴との照合。"""
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data" / "synthetic"))
from generate_forms import load_master, make_truth, phone  # noqa: E402

from app.config import settings
from app.extract.mock import MockExtractor
from app.judge import Judge
from app.judge import tools as T
from app.learn import CorrectionRouter, build_features
from app.pipeline import Pipeline
from app.schemas import CheckStatus, Extraction, FieldValue, OrderForm
from app.store import MemoryStore
from app.trust import Policy
from app.trust.ledger import LedgerStat


def test_zip_address_checks_town():
    assert T.check_zip_address("870-0001", "大分県大分市王子北町1-2-3").status == CheckStatus.PASS
    # 同じ大分市内の別番号（町域が違う）→ FAIL
    r = T.check_zip_address("870-0021", "大分県大分市王子北町1-2-3")
    assert r.status == CheckStatus.FAIL and "町域" in r.detail
    # 町域が登録されていない番号は市区町村までで PASS
    assert T.check_zip_address("874-0000", "大分県別府市3-7-20").status == CheckStatus.PASS


def _ex(flat):
    form = OrderForm.from_flat(flat)
    return Extraction(form=form, fields={p: FieldValue(path=p, value=v) for p, v in form.flatten().items()}, model="mock")


BASE = {"applicant.name": "田中 太郎", "applicant.name_kana": "タナカ タロウ", "applicant.zip": "870-0001",
        "applicant.address": "大分県大分市王子北町1-2-3", "applicant.phone": "097-555-1234", "applicant.organization": "",
        "deliveries[0].name": "鈴木 花子", "deliveries[0].name_kana": "スズキ ハナコ", "deliveries[0].zip": "150-0001",
        "deliveries[0].address": "東京都渋谷区神宮前1-1-1", "deliveries[0].phone": "03-3000-1000",
        "deliveries[0].product_code": "BMN-50", "deliveries[0].qty": 2, "deliveries[0].noshi_name": "御中元"}


def test_history_check_pass_unknown_and_feature():
    past = [OrderForm.from_flat(BASE)]
    cur = dict(BASE, **{"applicant.phone": "097-555-1284"})   # 電話を誤読
    v = Judge().judge(_ex(cur), None, history=past)
    assert any(c.name == "history" and c.status == CheckStatus.PASS for c in v["applicant.name"].checks)
    ph = next(c for c in v["applicant.phone"].checks if c.name == "history")
    assert ph.status == CheckStatus.FAIL and "読み違い" in ph.detail        # 1 桁違い → 読み違いの疑い
    v2 = Judge().judge(_ex(dict(BASE, **{"applicant.phone": "03-1234-5678"})), None, history=past)   # 別の番号 → 判定不能
    ph2 = next(c for c in v2["applicant.phone"].checks if c.name == "history")
    assert ph2.status == CheckStatus.UNKNOWN and "不一致" in ph2.detail
    # お届け先: 同じ氏名の過去のお届け先と郵便番号が一致
    assert any(c.name == "history" and c.status == CheckStatus.PASS for c in v["deliveries[0].zip"].checks)
    stats = {"sender": LedgerStat(key=""), "format": LedgerStat(key=""), "global": LedgerStat(key="")}
    assert build_features(FieldValue(path="applicant.name", value="田中 太郎"), v["applicant.name"], stats)["history_match"] == 1.0
    assert build_features(FieldValue(path="applicant.phone", value="x"), v["applicant.phone"], stats)["history_match"] == -1.0


def test_generator_keeps_applicant_fixed_per_sender():
    rng = random.Random(1)
    zips, products = load_master()
    pool = [{"id": "S1", "phone": phone(rng)}]
    t1, _ = make_truth(rng, zips, products, pool, blank_rate=0.0)
    t2, _ = make_truth(rng, zips, products, pool, blank_rate=0.0)
    assert t1["applicant"]["name"] == t2["applicant"]["name"] and t1["applicant"]["address"] == t2["applicant"]["address"]


def test_pipeline_uses_sender_history():
    router = CorrectionRouter(Path(tempfile.mkdtemp()), min_samples=10**6)
    pipe = Pipeline(store=MemoryStore(), extractor=MockExtractor(error_scale=0.0), policy=Policy.load(settings.policy_path), router=router, seed=1, budget_enabled=False)
    truth = OrderForm.from_flat(BASE)
    fd1 = pipe.process(b"a", sender_id="S1", hint=truth); pipe.confirm(fd1.form_id, {})
    fd2 = pipe.process(b"b", sender_id="S1", hint=truth)
    ev = [e for e in pipe.store.list_audit(fd2.form_id) if e.event == "judged"][0]
    assert ev.detail["history_forms"] == 1 and "applicant.name" in ev.detail["history_match"]


def test_customer_history_matches_across_senders():
    """同じ依頼主（電話番号が同じ）が別の送り主IDから送ってきても、顧客照合で過去の確定値と一致する。"""
    from app.pipeline import _phone_key
    assert _phone_key("097-555-1234") == "0975551234" and _phone_key("097-55") == "" and _phone_key("") == ""
    store = MemoryStore()
    router = CorrectionRouter(Path(tempfile.mkdtemp()), min_samples=10_000, target_error_rate=0.005)
    pipe = Pipeline(store=store, extractor=MockExtractor(error_scale=0.0), policy=Policy.load(settings.policy_path), router=router, seed=1,
                    budget_enabled=False)
    truth = OrderForm.from_flat(BASE)
    fd = pipe.process(b"img-a", sender_id="FAX-A", hint=truth)
    pipe.confirm(fd.form_id, {p: truth.flatten()[p] for p in fd.review_paths})
    assert store.customer_history("0975551234") and not store.customer_history("0000000000")
    fd2 = pipe.process(b"img-b", sender_id="FAX-B", hint=truth)     # 別の送り主IDから同じ依頼主
    for p in ("applicant.name", "applicant.address", "applicant.zip"):
        assert any(c.name == "history" and c.status == CheckStatus.PASS for c in fd2.verdicts[p].checks), p


def test_record_match_auto_confirms_even_for_new_sender():
    """記録と完全一致した項目は、初見の送り主でも（判定ルーター未学習・台帳実績なしでも）自動確定する。
    近い誤読は check_history が FAIL にするので自動確定しない（塞いだ穴を守る回帰テスト）。"""
    from app.schemas import FieldStatus

    store = MemoryStore()
    router = CorrectionRouter(Path(tempfile.mkdtemp()), min_samples=10_000, target_error_rate=0.005)   # 未学習
    pipe = Pipeline(store=store, extractor=MockExtractor(error_scale=0.0), policy=Policy.load(settings.policy_path),
                    router=router, seed=1, budget_enabled=False)
    truth = OrderForm.from_flat(BASE)
    fd = pipe.process(b"img-a", sender_id="FAX-A", hint=truth)          # 初回: 履歴なし → 氏名・住所は初見送り主で要確認
    assert "applicant.name" in fd.review_paths and "applicant.address" in fd.review_paths
    pipe.confirm(fd.form_id, {p: truth.flatten()[p] for p in fd.review_paths})

    fd2 = pipe.process(b"img-b", sender_id="FAX-B", hint=truth)         # 同じ顧客(電話一致)が別の送り主IDから
    for p in ("applicant.name", "applicant.address", "applicant.phone", "applicant.zip"):
        assert fd2.decisions[p].status == FieldStatus.AUTO, p
        assert any("記録と完全一致" in r for r in fd2.decisions[p].reasons), p
    assert "applicant.name" not in fd2.review_paths and "applicant.address" not in fd2.review_paths

    near = OrderForm.from_flat(dict(BASE, **{"applicant.address": "大分県大分市王子北町1-2-9"}))   # 1 文字違いの誤読
    fd3 = pipe.process(b"img-c", sender_id="FAX-C", hint=near)
    assert fd3.decisions["applicant.address"].status == FieldStatus.REVIEW   # 近い誤読は自動確定しない


def test_record_key_declared_by_format(monkeypatch):
    """顧客照合キーは様式ファイルの record_key 宣言から引く（電話番号の決め打ちではない）。
    宣言を別の項目に差し替えれば、その欄で顧客照合する（他業種への移行はこの 1 行）。"""
    import app.judge.zones as Z
    from app.pipeline import _record_key

    Z.load_record_key.cache_clear()
    form = OrderForm.from_flat(BASE)
    assert Z.load_record_key("fax_v1") == "applicant.phone"          # fax_v1 の宣言
    assert _record_key(form, "fax_v1") == "0975551234"               # 依頼主の電話番号で照合
    monkeypatch.setattr(Z, "load_record_key", lambda fid: "deliveries[0].phone")
    assert _record_key(form, "fax_v1") == "0330001000"               # 宣言を差し替えるとお届け先の電話で照合


def test_history_near_miss_is_a_misread_signal():
    """常連の宛先の確定値と 1〜2 文字だけ違う値（4-21-20 → 4-2-20）は「不一致」ではなく読み違いの疑いで不合格にする。
    実手書きの 2 周目で、二重読みが一致した読み違いがこの形で自動確定された。大きく違う値は転居・別人として判定不能のまま。"""
    past = ["東京都新宿区新宿4-21-20"]
    r = T.check_history("東京都新宿区新宿4-2-20", past)
    assert r.status == CheckStatus.FAIL and "読み違い" in r.detail
    assert T.check_history("東京都新宿区新宿4-21-20", past).status == CheckStatus.PASS
    assert T.check_history("東京都新宿区新宿4-21-20", ["大分県大分市王子北町1-2-3"]).status == CheckStatus.UNKNOWN
    assert T.check_history("097-555-1284", ["097-555-1234"]).status == CheckStatus.FAIL
    assert T.check_history("鈴木 花", ["鈴木 花子"]).status == CheckStatus.UNKNOWN      # 短い値は近似で判断しない
