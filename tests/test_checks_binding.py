"""検証の組み合わせは宣言ファイル（data/master/checks.yaml）。業種依存は 様式 JSON・マスタ CSV・この宣言 の 3 ファイル。"""
import tempfile
from pathlib import Path

from app.judge.core import CheckBindings, Judge, load_check_bindings
from app.judge import tools as T


def test_bundled_bindings_reproduce_the_previous_hardcoded_checks():
    """以前コードに書かれていた組み合わせと同じ並びであること（挙動を変えずにファイルへ出しただけ）。"""
    b = load_check_bindings(Path("data/master/checks.yaml"))
    expect = {
        "deliveries.zip": ["zip_format", "zip_address"], "applicant.zip": ["zip_format", "zip_address"],
        "deliveries.address": ["nonempty", "zip_address"], "deliveries.phone": ["phone_format", "phone_area"],
        "deliveries.name_kana": ["kana_format", "name_reading"], "deliveries.product_code": ["product_exists"],
        "deliveries.qty": ["qty_range"], "deliveries.name": ["nonempty", "name_reading"],
        "applicant.organization": [], "deliveries.noshi_name": [],
    }
    for ft, names in expect.items():
        assert [n for n, _ in b.checks_for(ft)] == names, ft
    assert b.wants_history("applicant.phone") and not b.wants_history("deliveries.qty")
    assert b.wants_variant_kanji("deliveries.noshi_name") and not b.wants_variant_kanji("deliveries.zip")
    # 宣言に出てくる検証名はすべて登録済みの関数
    for _, items in b.rules:
        for name, _ in items:
            assert name in T.CHECKS, name


def test_cross_check_arguments_come_from_the_same_block():
    j = Judge()
    flat = {"deliveries[0].zip": "870-0001", "deliveries[0].address": "東京都渋谷区神宮前1-1-1",
            "deliveries[1].zip": "150-0001", "deliveries[1].address": "東京都渋谷区神宮前1-1-1"}
    c0 = {c.name: c.status.value for c in j._checks_for("deliveries.zip", "deliveries[0].zip", "870-0001", flat)}
    c1 = {c.name: c.status.value for c in j._checks_for("deliveries.zip", "deliveries[1].zip", "150-0001", flat)}
    assert c0["zip_address"] == "fail" and c1["zip_address"] == "pass"       # お届け先 1 の住所と、お届け先 2 の住所を取り違えない


def test_another_industry_only_needs_a_new_declaration(tmp_path):
    """別業種: 検証関数を 1 つ登録し、宣言ファイルを差し替えるだけで項目種別が増やせる。"""
    from app.schemas import CheckResult, CheckStatus

    def check_care_level(v: str) -> CheckResult:
        ok = str(v) in {"要支援1", "要支援2", "要介護1", "要介護2", "要介護3", "要介護4", "要介護5"}
        return CheckResult(name="care_level", status=CheckStatus.PASS if ok else CheckStatus.FAIL, detail="" if ok else f"区分にない: {v}")

    T.CHECKS["care_level"] = check_care_level
    try:
        p = tmp_path / "checks.yaml"
        p.write_text('checks:\n  "*.care_level": [care_level]\n  "*.zip": [zip_format]\nhistory: ["*.name"]\n', encoding="utf-8")
        j = Judge(checks_path=p)
        assert [c.status.value for c in j._checks_for("user.care_level", "user.care_level", "要介護9", {})] == ["fail"]
        assert [c.name for c in j._checks_for("user.zip", "user.zip", "870-0001", {})] == ["zip_format"]
        assert j._checks_for("user.address", "user.address", "x", {}) == []          # 宣言に無い項目は検証しない
    finally:
        T.CHECKS.pop("care_level", None)


def test_missing_declaration_is_an_error():
    import pytest
    with pytest.raises(FileNotFoundError):
        Judge(checks_path=Path(tempfile.mkdtemp()) / "nope.yaml")
