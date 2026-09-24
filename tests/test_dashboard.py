"""ダッシュボード: 運用（本番ストア）と検証結果（同梱の曲線・要約）を 1 つの API で返し、画面の言葉は現場向け。"""
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.verification import count_proposals, load_curve, summarize_run, verification_data


def test_summarize_run_formula():
    rows = [
        {"day": "1", "forms": "10", "fields": "100", "review_rate": "1.0", "auto_error_rate": "0.0", "auto_error_rate_audited": "", "proposals": "2", "approved": "1"},
        {"day": "8", "forms": "10", "fields": "100", "review_rate": "0.5", "auto_error_rate": "0.02", "auto_error_rate_audited": "0.01", "proposals": "3", "approved": "2"},
        {"day": "9", "forms": "10", "fields": "100", "review_rate": "0.2", "auto_error_rate": "0.005", "auto_error_rate_audited": "", "proposals": "0", "approved": "0"},
    ]
    s = summarize_run(rows)
    assert s["days"] == 3 and s["forms"] == 30 and s["fields"] == 300
    assert s["review_rate_first"] == 1.0 and s["review_rate_last"] == 0.2
    assert abs(s["review_rate_late_avg"] - 0.35) < 1e-9
    # 実際の誤り率 = Σ(auto_error_rate × 自動確定数) / Σ自動確定数 = (0.02×50 + 0.005×80) / (0+50+80)
    assert abs(s["true_error_rate"] - (0.02 * 50 + 0.005 * 80) / 130) < 1e-9
    assert s["audited_error_rate_last"] == 0.01          # 最後に値があった日
    assert s["approved"] == 3 and s["proposals"] == 5
    assert summarize_run([]) == {"days": 0}


def test_presentation_numbers_match_bundled_curves():
    """発表の数字（HANDOVER.md）が同梱 CSV から再現できる。"""
    imp = summarize_run(load_curve("eval/out/curve_gemini_200x14_v2.csv"))
    base = summarize_run(load_curve("eval/out/curve_gemini_200x14_vertex.csv"))
    assert round(base["review_rate_last"], 3) == 0.510 and round(imp["review_rate_last"], 3) == 0.106
    assert round(imp["true_error_rate"] * 100, 2) == 0.14 and round(base["true_error_rate"] * 100, 2) == 0.27
    assert imp["audited_error_rate_last"] == 0.0025
    assert imp["approved"] == 83 and imp["proposals"] == 113


def test_count_proposals_breakdown():
    p = Path(tempfile.mkdtemp()) / "x.proposals.jsonl"
    lines = [
        {"kind": "rule", "status": "approved", "title": "a"},
        {"kind": "rule", "status": "approved", "title": "b"},
        {"kind": "retract", "status": "approved", "title": "c"},
        {"kind": "rule", "status": "rejected", "title": "（自動却下）d"},
        {"kind": "policy", "status": "rejected", "title": "e"},
        {"kind": "policy", "status": "pending", "title": "f"},
    ]
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    c = count_proposals(p)
    assert (c["total"], c["approved"], c["retracted"], c["auto_rejected"], c["rejected"], c["pending"]) == (6, 2, 1, 1, 1, 1)
    assert c["by_kind_status"]["rule/approved"] == 2
    assert count_proposals(p.with_name("missing.jsonl"))["source"] is None


def test_verification_data_bundle():
    v = verification_data()
    keys = [r["key"] for r in v["runs"]]
    assert keys == ["baseline", "improved", "noreflect", "pageread"]
    assert set(v["curves"]) >= {"baseline", "improved"}
    assert v["curves"]["improved"][0]["day"] == 1 and v["curves"]["improved"][-1]["review_rate"] == 0.1062
    assert v["measurement_after"] == {"forms": 20, "timed_forms": 9, "no_review_forms": 11, "deliveries": 34, "seconds_per_form_median": 7.2}
    assert "field_measurement" in v["summary"] and "proposals" in v["summary"]


def test_dashboard_api_and_page_wording():
    from app.main import app
    c = TestClient(app)
    d = c.get("/api/dashboard").json()
    assert {"kpi", "daily", "ledger_levels", "seeded_from_simulation", "verification"} <= set(d)
    assert d["seeded_from_simulation"] is False
    assert d["verification"]["runs"][1]["key"] == "improved"
    html = c.get("/dashboard").text
    assert "自動確定の育ち具合" in html and "運用（本番）" in html and "検証結果" in html
    # 画面に出る文字列（HTML と、JS 内の言葉の表 T）に内部用語が無いこと。データのキー名（r.L1 など）は対象外
    import re
    visible = re.sub(r"<style>.*?</style>", "", html, flags=re.S)
    body, script = visible.split("<script>", 1)
    words = script[: script.index("const $ =")]        # 言葉の表 T だけ
    for jargon in ("監査サンプル", "真値", "ポリシー", "教師データ", "検証合格", "Day ", "L0", "L1", "L2", "ルーター", "閾値"):
        assert jargon not in body and jargon not in words, jargon
