"""混雑（429）・一時障害は再試行し、それでも駄目なら修復を諦めて人に回す。受付そのものは止めない。"""
import tempfile
from pathlib import Path


class _FakeModels:
    def __init__(self, fail_times: int, code: int = 429):
        from google.genai import errors
        self.calls = 0
        self.fail_times = fail_times
        self.err = errors.APIError(code, {"error": {"code": code, "message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED"}}, None)

    def generate_content(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.err
        class R:  # noqa
            text = '{"value": "870-0001", "evidence": "870-0001"}'
            usage_metadata = None
        return R()


def _extractor(fail_times: int, monkeypatch):
    import app.extract.gemini as gm
    monkeypatch.setenv("GEMINI_MAX_RETRIES", "3")
    monkeypatch.setattr(gm.time, "sleep", lambda s: None, raising=False)
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    ex = gm.GeminiExtractor.__new__(gm.GeminiExtractor)
    ex.client = type("C", (), {})(); ex.client.models = _FakeModels(fail_times)
    ex.model, ex.premium_model, ex.thinking_budget, ex.second_read, ex.second_model = "m", "p", None, "zones", "m"
    return ex


def test_generate_retries_on_429_then_succeeds(monkeypatch):
    ex = _extractor(2, monkeypatch)
    assert ex.extract_field(b"crop", "deliveries.zip") == ("870-0001", "870-0001")
    assert ex.client.models.calls == 3


def test_generate_gives_up_after_max_retries(monkeypatch):
    import pytest
    from google.genai import errors
    ex = _extractor(10, monkeypatch)
    with pytest.raises(errors.APIError):
        ex.extract_field(b"crop", "deliveries.zip")
    assert ex.client.models.calls == 4          # 1 + 再試行 3


def test_resolver_survives_reread_failure_and_escalates():
    """欄の再読み取りが例外を投げても、その項目は人に回り、受付は完了する。"""
    from app.config import settings
    from app.extract.mock import MockExtractor
    from app.learn import CorrectionRouter
    from app.pipeline import Pipeline
    from app.schemas import OrderForm
    from app.store import MemoryStore
    from app.trust import Policy

    class Flaky(MockExtractor):
        def extract_field(self, *a, **k):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    pipe = Pipeline(store=MemoryStore(), extractor=Flaky(), policy=Policy.load(settings.policy_path),
                    router=CorrectionRouter(Path(tempfile.mkdtemp()), min_samples=10**6), seed=1, budget_enabled=False)
    truth = OrderForm.model_validate({"applicant": {"name": "田中 太郎", "zip": "870-0001", "address": "大分県大分市王子北町1-2-3", "phone": "097-555-1234"},
                                      "deliveries": [{"name": "鈴木 花子", "zip": "150-0001", "address": "東京都渋谷区神宮前1-1-1", "phone": "03-3000-1000", "product_code": "BMN-50", "qty": 2}]})
    pipe.extractor.set_defect("deliveries.zip", "1", "7", 1.0)     # 検証で止まる項目を作り、修復（再読み取り）を走らせる
    image = Path("data/measurement/handwriting/blank_fax_v1.png").read_bytes()   # 本物の様式画像（欄の切り出しができる）
    fd = pipe.process(image, sender_id="S1", hint=truth)
    assert "deliveries[0].zip" in fd.review_paths
    ev = [e for e in pipe.store.list_audit(fd.form_id) if e.event == "resolved"]
    assert ev and any(a.get("error", "").startswith("RuntimeError") for a in ev[0].detail["actions"])
