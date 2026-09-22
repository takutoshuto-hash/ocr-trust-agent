"""ベースライン比較と空欄増量評価（記事冒頭の主張の証拠）。

同じ手書き合成帳票を実 Gemini で読み、項目ごとに次を記録する:
  真値・抽出値・自己申告 confidence・二重読み取りの一致・検証結果・学習ルーターの p
そのうえで、自動確定の「カバー率 vs 誤り率」曲線を2方式で比べる:
  A) 自己申告スコア方式: confidence ≥ τ なら自動確定（世間の一般解）
  B) 本作: 検証 FAIL は必ず人へ、残りは学習ルーター p < t なら自動確定
空欄率を上げた帳票（--blank-rate）で、ハルシネーション（空欄への創作）の件数と、それを confidence / 空欄検知がどれだけ捕捉するかも数える。

使い方: python eval/compare_baselines.py --n 100 --blank-rate 0.4 --state eval/out/state_mock10d --out eval/out/baselines
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import random
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data" / "synthetic"))
from generate_forms import fonts, load_master, make_truth, phone, render  # noqa: E402

from app.config import settings  # noqa: E402
from app.extract.gemini import GeminiExtractor  # noqa: E402
from app.judge import Judge  # noqa: E402
from app.learn import CorrectionRouter, build_features  # noqa: E402
from app.schemas import CheckStatus  # noqa: E402
from app.store import MemoryStore  # noqa: E402
from app.trust import Policy, TrustLedger  # noqa: E402
from app.trust.ledger import LedgerStat  # noqa: E402


def _norm(x):
    return str(x if x is not None else "").replace(" ", "").replace("　", "").replace("-", "").upper()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--blank-rate", type=float, default=0.4)
    ap.add_argument("--seed", type=int, default=99)
    ap.add_argument("--state", default="eval/out/state_mock10d", help="台帳とルーターモデル（シミュレーション運用の状態）")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default="eval/out/baselines")
    a = ap.parse_args()
    if not settings.use_gemini:
        raise SystemExit("GEMINI_API_KEY が必要です")

    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(a.seed)
    zips, products = load_master(); fnts = fonts()
    pool = [{"id": f"S{i:04d}", "phone": phone(rng)} for i in range(120)]
    policy = Policy.load(settings.policy_path)
    store = MemoryStore()
    state = Path(a.state)
    if (state / "ledger.json").exists():
        for d in json.loads((state / "ledger.json").read_text(encoding="utf-8")):
            store.put_ledger(LedgerStat.from_dict(d))
    ledger = TrustLedger(store, policy)
    router = CorrectionRouter(state if (state / "router.joblib").exists() else Path(tempfile.mkdtemp()))
    hc = policy.raw.get("hallucination", {})
    judge = Judge(blank_ink_ratio=float(hc.get("blank_ink_ratio", 0.013)), evidence_max_distance=float(hc.get("evidence_max_distance", 0.5)))
    extractor = GeminiExtractor(api_key=settings.gemini_api_key, model=settings.gemini_model)
    print(f"router loaded: n={router.trained_on} thr={router.threshold}  ledger keys={len(store.list_ledger())}", flush=True)

    forms = []
    for i in range(a.n):
        truth_d, sender = make_truth(rng, zips, products, pool, blank_rate=a.blank_rate)
        buf = io.BytesIO(); render(truth_d, fnts, rng).save(buf, format="PNG")
        forms.append((buf.getvalue(), sender, truth_d))

    def run(item):
        image, sender, truth_d = item
        from app.schemas import OrderForm
        truth = OrderForm.model_validate(truth_d)
        for attempt in range(3):
            try:
                ex1 = extractor.extract(image, variant=0); ex2 = extractor.extract(image, variant=1)
                break
            except Exception as e:
                if attempt == 2:
                    print("  failed:", str(e)[:100], flush=True); return []
        verdicts = judge.judge(ex1, ex2, image=image, format_id="fax_v1")
        tflat = truth.flatten(); rows = []
        for path, fv in ex1.fields.items():
            v = verdicts[path]; ft = v.field_type
            stats = {"sender": ledger.stat(f"{ft}|sender:{sender}"), "format": ledger.stat(f"{ft}|format:fax_v1"), "global": ledger.stat(f"{ft}|global")}
            p = router.predict(build_features(fv, v, stats))
            tv = tflat.get(path)
            rows.append({"sender": sender, "path": path, "field_type": ft, "truth": tv, "value": fv.value,
                         "correct": int(_norm(fv.value) == _norm(tv)), "confidence": fv.confidence,
                         "agreement": int(bool(v.agreement)), "judge_fail": int(v.any_fail),
                         "blank_truth": int(isinstance(tv, str) and tv == ""), "invented": int(isinstance(tv, str) and tv == "" and str(fv.value).strip() != ""),
                         "blank_zone_fail": int(any(c.name == "blank_zone" and c.status == CheckStatus.FAIL for c in v.checks)),
                         "router_p": p})
        return rows

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        results = list(ex.map(run, forms))
    rows = [r for rs in results for r in rs]
    with (out / "fields.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

    # ---- 集計 ----
    n = len(rows); acc = sum(r["correct"] for r in rows) / n
    blanks = [r for r in rows if r["blank_truth"]]; inv = [r for r in blanks if r["invented"]]
    caught = [r for r in inv if r["blank_zone_fail"]]
    conf = np.array([r["confidence"] for r in rows]); ok = np.array([r["correct"] for r in rows]); jf = np.array([r["judge_fail"] for r in rows])
    rp = np.array([r["router_p"] if r["router_p"] is not None else 1.0 for r in rows])
    summary = {"forms": a.n, "fields": n, "accuracy": round(acc, 4), "blank_truth": len(blanks), "invented": len(inv),
               "invented_rate": round(len(inv) / len(blanks), 4) if blanks else None,
               "invented_caught_by_blank_zone": len(caught), "invented_mean_confidence": round(float(np.mean([r["confidence"] for r in inv])), 3) if inv else None,
               "correct_mean_confidence": round(float(conf[ok == 1].mean()), 3), "wrong_mean_confidence": round(float(conf[ok == 0].mean()), 3) if (ok == 0).any() else None}

    # カバー率 vs 誤り率の曲線（A: confidence 閾値 / B: 検証ゲート＋ルーター p 閾値）
    curve = []
    for tau in np.round(np.arange(0.50, 1.001, 0.02), 2):
        m = conf >= tau
        curve.append({"method": "A_self_confidence", "threshold": float(tau), "coverage": round(float(m.mean()), 4),
                      "error_rate": round(float(1 - ok[m].mean()), 4) if m.any() else None, "n_auto": int(m.sum())})
    for t in np.round(np.concatenate([np.arange(0.002, 0.05, 0.002), np.arange(0.05, 0.5, 0.05)]), 3):
        m = (rp < t) & (jf == 0)
        curve.append({"method": "B_ours_judge_router", "threshold": float(t), "coverage": round(float(m.mean()), 4),
                      "error_rate": round(float(1 - ok[m].mean()), 4) if m.any() else None, "n_auto": int(m.sum())})
    with (out / "curve.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(curve[0].keys())); w.writeheader(); w.writerows(curve)

    # 同じカバー率での比較（B の各点に対し、同じ以上のカバー率を持つ A の最小誤り率）
    A = [c for c in curve if c["method"].startswith("A")]; B = [c for c in curve if c["method"].startswith("B")]
    pairs = []
    for b in B:
        if b["error_rate"] is None or b["n_auto"] < 50: continue
        cands = [x for x in A if x["error_rate"] is not None and x["coverage"] >= b["coverage"]]
        if cands:
            best = min(cands, key=lambda x: x["error_rate"])
            pairs.append({"coverage_B": b["coverage"], "error_B": b["error_rate"], "coverage_A": best["coverage"], "error_A": best["error_rate"], "A_tau": best["threshold"], "B_t": b["threshold"]})
    summary["matched_pairs"] = pairs[:12]
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "matched_pairs"}, ensure_ascii=False, indent=1))
    print("\n同じカバー率での自動確定誤り率（A: 自己申告 / B: 本作）")
    for p in pairs[:12]:
        print(f"  coverage {p['coverage_B']:.2f}: B {p['error_B']:.4f} (t={p['B_t']})  vs  A {p['error_A']:.4f} (tau={p['A_tau']}, cov {p['coverage_A']:.2f})")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
