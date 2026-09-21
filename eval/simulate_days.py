"""日次シミュレーション: 「数をさばくほど要確認率が下がる」曲線を作る。

各日: N枚を処理 → 要確認項目は人が正解に直す（＝教師データ）→ 自動確定項目は正解と照合して"見逃し誤り"を集計
→ 夜間に再学習。日ごとの要確認率・自動確定誤り率・台帳の昇格数を CSV に出す。

使い方: python eval/simulate_days.py --days 14 --per-day 300 --senders 150 --out eval/out/curve.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data" / "synthetic"))

from generate_forms import load_master, make_truth, phone  # noqa: E402

from app.config import settings  # noqa: E402
from app.learn import CorrectionRouter  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402
from app.schemas import FieldStatus, OrderForm  # noqa: E402
from app.store import MemoryStore  # noqa: E402
from app.trust import Policy  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--per-day", type=int, default=300)
    ap.add_argument("--senders", type=int, default=150)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="eval/out/curve.csv")
    a = ap.parse_args()

    rng = random.Random(a.seed)
    zips, products = load_master()
    pool = [{"id": f"S{i:04d}", "phone": phone(rng)} for i in range(a.senders)]
    policy = Policy.load(settings.policy_path)
    rc = policy.router
    tmp = Path(tempfile.mkdtemp())   # モデルは一時ディレクトリに（本番モデルを汚さない）
    router = CorrectionRouter(tmp, min_samples=int(rc.get("min_training_samples", 200)),
                              target_error_rate=float(rc.get("target_error_rate", 0.005)))
    from app.extract.mock import MockExtractor
    pipe = Pipeline(store=MemoryStore(), extractor=MockExtractor(), policy=policy, router=router, seed=a.seed)   # 常にモック

    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for day in range(1, a.days + 1):
        n_fields = n_review = n_auto = n_auto_wrong = n_human_corr = 0
        for i in range(a.per_day):
            truth_d, sender = make_truth(rng, zips, products, pool)
            truth = OrderForm.model_validate(truth_d)
            image = json.dumps(truth_d, ensure_ascii=False).encode() + bytes([day % 251, i % 251])
            fd = pipe.process(image, sender_id=sender, format_id="fax_v1", hint=truth)
            tflat = truth.flatten()
            corrections = {}
            for path, d in fd.decisions.items():
                n_fields += 1
                ok = _norm(d.value) == _norm(tflat[path])
                if d.status == FieldStatus.REVIEW:
                    n_review += 1
                else:
                    n_auto += 1
                    n_auto_wrong += (not ok)              # 自動確定の誤り（真値と照合。監査サンプル分は人が直す）
                if d.human_sees and not ok:
                    corrections[path] = tflat[path]       # 人が原本を見て直す（要確認＋監査サンプル）
                    n_human_corr += 1
            pipe.confirm(fd.form_id, corrections, actor="human:sim")
        summary = pipe.retrain()                            # 夜間再学習
        promoted = sum(1 for s in pipe.store.list_ledger() if s.level > 0)
        m = pipe.metrics()
        row = {"day": day, "forms": a.per_day, "fields": n_fields,
               "review_rate": round(n_review / n_fields, 4),
               "auto_error_rate": round(n_auto_wrong / n_auto, 5) if n_auto else 0.0,   # 真値比較（神の視点）
               "auto_error_rate_audited": m["auto_error_rate_audited"],                   # 監査サンプルからの推定（運用で見える値）
               "human_corrections": n_human_corr, "ledger_promoted_keys": promoted,
               "router_trained": summary.get("trained", False), "router_threshold": summary.get("threshold")}
        rows.append(row)
        print(f"day {day:2d}: review {row['review_rate']:.3f}  auto_err {row['auto_error_rate']:.4f} (audited {row['auto_error_rate_audited']})  "
              f"promoted {promoted:4d}  router {'yes' if row['router_trained'] else 'no '} thr={row['router_threshold']}")

    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print(f"-> {out}")


def _norm(x):
    return str(x if x is not None else "").replace(" ", "").replace("　", "").replace("-", "").upper()


if __name__ == "__main__":
    main()
