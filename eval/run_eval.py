"""評価: 合成帳票（正解付き）に対して抽出→判定を回し、項目別の正解率・要確認率・自動確定の誤り率を出す。

GEMINI_API_KEY があれば実画像を Gemini で読む。なければモック抽出器（正解に誤りを混入）。
使い方: python eval/run_eval.py --dir data/synthetic/out --limit 100
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from app.pipeline import Pipeline
from app.schemas import FieldStatus, OrderForm
from app.store import MemoryStore


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/synthetic/out")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--mock", action="store_true", help="GEMINI_API_KEY があってもモック抽出器を使う")
    a = ap.parse_args()

    extractor = None
    if a.mock:
        from app.extract.mock import MockExtractor
        extractor = MockExtractor()
    pipe = Pipeline(store=MemoryStore(), extractor=extractor, explain=False)
    print(f"extractor = {pipe.extractor.name}")
    per_ft = defaultdict(lambda: {"n": 0, "correct": 0, "review": 0, "auto_wrong": 0})
    hal = {"blank_truth": 0, "invented": 0, "invented_caught": 0}   # 空欄の正解数 / 創作された数 / うち検知された数
    files = sorted(Path(a.dir).glob("*.json"))[: a.limit]
    for jf in files:
        meta = json.loads(jf.read_text(encoding="utf-8"))
        truth = OrderForm.model_validate(meta["truth"])
        png = jf.with_suffix(".png")
        image = png.read_bytes() if png.exists() else jf.name.encode()
        fd = pipe.process(image, sender_id=meta["sender_id"], format_id=meta["format_id"], hint=truth)
        print(f"  {jf.name}: review {len(fd.review_paths)}/{len(fd.decisions)}  ({fd.extraction.latency_ms} ms)", flush=True)
        tflat = truth.flatten()
        for path, d in fd.decisions.items():
            s = per_ft[d.field_type]
            s["n"] += 1
            ok = _norm(d.value) == _norm(tflat.get(path))
            s["correct"] += ok
            s["review"] += d.status == FieldStatus.REVIEW
            s["auto_wrong"] += (d.status == FieldStatus.AUTO and not ok)
            tv = tflat.get(path)
            if isinstance(tv, str) and not tv:
                hal["blank_truth"] += 1
                if str(d.value).strip():
                    hal["invented"] += 1
                    hal["invented_caught"] += any(c.name == "blank_zone" and c.status.value == "fail"
                                                  for c in fd.verdicts[path].checks)   # 空欄検知そのものの検知率

    print(f"{'field_type':28} {'n':>5} {'acc':>7} {'review':>7} {'auto_err':>9}")
    tot = {"n": 0, "correct": 0, "review": 0, "auto_wrong": 0}
    for ft, s in sorted(per_ft.items()):
        for k in tot:
            tot[k] += s[k]
        auto_n = s["n"] - s["review"]
        print(f"{ft:28} {s['n']:5d} {s['correct']/s['n']:7.3f} {s['review']/s['n']:7.3f} {(s['auto_wrong']/auto_n if auto_n else 0):9.4f}")
    auto_n = tot["n"] - tot["review"]
    print(f"{'ALL':28} {tot['n']:5d} {tot['correct']/tot['n']:7.3f} {tot['review']/tot['n']:7.3f} {(tot['auto_wrong']/auto_n if auto_n else 0):9.4f}")
    if hal["blank_truth"]:
        print(f"\nハルシネーション: 空欄 {hal['blank_truth']} 件中、値を創作 {hal['invented']} 件"
              f"（創作率 {hal['invented']/hal['blank_truth']:.3f}）、うち空欄検知で捕捉 {hal['invented_caught']} 件"
              f"（検知率 {(hal['invented_caught']/hal['invented'] if hal['invented'] else 1):.3f}）")


def _norm(x):
    return str(x if x is not None else "").replace(" ", "").replace("　", "").replace("-", "").upper()


if __name__ == "__main__":
    main()
