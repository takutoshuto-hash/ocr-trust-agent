"""選択バイアス補正（監査サンプルの重み）とお届け先ブロック読み落とし検知。"""
import io
import random
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data" / "synthetic"))
from generate_forms import fonts, load_master, make_truth, phone, render  # noqa: E402

from app.judge import Judge
from app.learn import CorrectionRouter
from app.schemas import Extraction, FieldValue, OrderForm, TrainingRecord


def test_weighted_threshold_uses_effective_sample_size():
    r = CorrectionRouter(Path(tempfile.mkdtemp()), target_error_rate=0.05)
    p = np.linspace(0, 1, 200)
    y = np.zeros(200, dtype=int); y[150:] = 1
    # 重みなし: 低 p 側 150 件が無誤り（Wilson 上限 ≈2.5% < 5%）→ 閾値は正
    t_plain = r._threshold_for_target(p, y)
    # 監査サンプルとして低 p 側を 50 倍の重みにすると有効標本数が増え、閾値はさらに緩む（同じか大きい）
    w = np.ones(200); w[:150] = 50.0
    t_w = r._threshold_for_target(p, y, w)
    assert t_plain > 0 and t_w >= t_plain


def test_router_trains_with_weights():
    rng = random.Random(0)
    recs = []
    for i in range(400):
        agree = rng.random() < 0.7
        corrected = (rng.random() < 0.02) if agree else (rng.random() < 0.3)
        recs.append(TrainingRecord(form_id="f", path="deliveries[0].zip", field_type="deliveries.zip", sender_id="S", format_id="fax_v1",
                                   extracted="a", final="b" if corrected else "a", corrected=corrected, was_auto=agree, verified=True,
                                   weight=50.0 if agree else 1.0, features={"agreement": 1.0 if agree else 0.0, "agreement_known": 1.0}))
    r = CorrectionRouter(Path(tempfile.mkdtemp()), min_samples=100, target_error_rate=0.05)
    s = r.train(recs)
    assert s["trained"] and s["weighted_n"] > 400 and r.threshold is not None


def test_detect_missing_block_on_rendered_form():
    rng = random.Random(9)
    zips, products = load_master()
    truth, _ = make_truth(rng, zips, products, [{"id": "S1", "phone": phone(rng)}], blank_rate=0.0)
    while len(truth["deliveries"]) < 2:   # 2 件以上のお届け先がある帳票を作る
        truth, _ = make_truth(rng, zips, products, [{"id": "S1", "phone": phone(rng)}], blank_rate=0.0)
    buf = io.BytesIO(); render(truth, fonts(), rng).save(buf, format="PNG"); png = buf.getvalue()
    # 抽出結果から最後のお届け先を落とす
    dropped = dict(truth); dropped["deliveries"] = truth["deliveries"][:-1]
    form = OrderForm.model_validate(dropped)
    ex = Extraction(form=form, fields={p: FieldValue(path=p, value=v) for p, v in form.flatten().items()}, model="mock")
    flags = Judge().detect_missing_blocks(ex, image=png, format_id="fax_v1")
    assert len(flags) == 1 and f"お届け先 {len(truth['deliveries'])} " in flags[0]
    # 落とさなければ警告なし
    full = OrderForm.model_validate(truth)
    ex2 = Extraction(form=full, fields={p: FieldValue(path=p, value=v) for p, v in full.flatten().items()}, model="mock")
    assert Judge().detect_missing_blocks(ex2, image=png, format_id="fax_v1") == []
