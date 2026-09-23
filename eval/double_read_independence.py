"""二重読み取りの「独立性」を実測する。

1 回目（全面画像・Flash・通常プロンプト）に対して、2 回目の候補を複数比べる:
  <model>:page  = 全面画像を別の言い回しのプロンプトで（従来）
  <model>:zones = 欄ごとの切り出し画像を 1 リクエストで（新しい 2 回目）
model は flash / lite / pro（gemini-2.5-*）。
項目種別ごとに、各読みの誤り率、1 回目との一致率、**一致したのに誤っていた率**（= 一致が根拠としてどれだけ信用できるか）を出す。

使い方（Vertex）: GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_GENAI_USE_GCLOUD_TOKEN=1 GOOGLE_CLOUD_PROJECT=... \\
    python eval/double_read_independence.py --n 60 --seed 31 --workers 2 --variants flash:page,flash:zones,lite:zones,pro:zones
"""
from __future__ import annotations

import argparse
import csv
import io
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data" / "synthetic"))

from generate_forms import fonts, load_master, make_truth, phone, render  # noqa: E402

from app.extract.gemini import GeminiExtractor  # noqa: E402
from app.schemas import OrderForm, field_type_of  # noqa: E402

MODELS = {"flash": "gemini-2.5-flash", "lite": "gemini-2.5-flash-lite", "pro": "gemini-2.5-pro",
          "flash0": "gemini-2.5-flash", "lite0": "gemini-2.5-flash-lite"}     # *0 = 思考なし（thinking_budget=0）


def _norm(x):
    return str(x if x is not None else "").replace(" ", "").replace("　", "").replace("-", "").replace("_", "").upper()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=31)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--variants", default="flash:page,flash:zones,lite:zones,pro:zones")
    ap.add_argument("--out", default="eval/out/double_read_independence.csv")
    a = ap.parse_args()
    variants = [v.strip() for v in a.variants.split(",") if v.strip()]

    rng = random.Random(a.seed)
    zips, products = load_master()
    fnts = fonts()
    pool = [{"id": f"S{i:04d}", "phone": phone(rng)} for i in range(40)]
    forms = []
    for _ in range(a.n):
        td, sender = make_truth(rng, zips, products, pool)
        buf = io.BytesIO(); render(td, fnts, rng).save(buf, format="PNG")
        forms.append((OrderForm.model_validate(td), buf.getvalue()))

    primary = GeminiExtractor(second_read="page")
    readers = {}
    for v in variants:
        m, mode = v.split(":")
        readers[v] = GeminiExtractor(model=MODELS[m], second_read=mode, thinking_budget=(0 if m.endswith("0") else None))

    def call(fn, *args, **kw):
        for attempt in range(5):
            try:
                return fn(*args, **kw)
            except Exception as e:   # 429 など
                print(f"  retry {attempt + 1}: {str(e)[:80]}", flush=True)
                time.sleep(5 * (attempt + 1))
        return None

    def read(item):
        truth, png = item
        r1 = call(primary.extract, png, variant=0, format_id="fax_v1")
        # *:page の候補は variant=0（1回目と同じプロンプト）で読み、思考の有無だけの差を測れるようにする
        outs = {v: call(rd.extract, png, variant=(0 if v.endswith("0:page") else 1), format_id="fax_v1") for v, rd in readers.items()}
        return truth, r1, outs

    stats = defaultdict(lambda: defaultdict(int))
    with ThreadPoolExecutor(max_workers=a.workers) as pool_:
        for i, (truth, r1, outs) in enumerate(pool_.map(read, forms), 1):
            if r1 is None:
                continue
            t, f1 = truth.flatten(), r1.form.flatten()
            for path, tv in t.items():
                ft = field_type_of(path)
                s = stats[ft]
                v1, tv = _norm(f1.get(path)), _norm(tv)
                s["n"] += 1
                s["err1"] += v1 != tv
                for v, r in outs.items():
                    if r is None:
                        continue
                    v2 = _norm(r.form.flatten().get(path))
                    s[f"n:{v}"] += 1
                    s[f"err:{v}"] += v2 != tv
                    s[f"agree:{v}"] += v1 == v2
                    s[f"agree_wrong:{v}"] += (v1 == v2 and v1 != tv)
            if i % 10 == 0:
                print(f"  {i}/{len(forms)} forms", flush=True)

    rows, tot = [], defaultdict(int)
    for ft, s in sorted(stats.items()):
        for k, v in s.items():
            tot[k] += v
        rows.append(_row(ft, s, variants))
    rows.append(_row("ALL", tot, variants))
    hdr = f"{'field_type':26}{'n':>6}{'err1':>7}" + "".join(f"{'err:' + v:>13}{'agree:' + v:>13}{'w|agree:' + v:>15}" for v in variants)
    print(hdr)
    for r in rows:
        print(f"{r['field_type']:26}{r['n']:6}{r['err_read1']:7.3f}" + "".join(
            f"{r['err:' + v]:13.3f}{r['agree:' + v]:13.3f}{r['wrong_given_agree:' + v]:15.4f}" for v in variants))
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    print(f"wrote {out}")


def _row(ft, s, variants):
    n = s["n"] or 1
    r = {"field_type": ft, "n": s["n"], "err_read1": round(s["err1"] / n, 4)}
    for v in variants:
        nv = s[f"n:{v}"] or 1
        r[f"err:{v}"] = round(s[f"err:{v}"] / nv, 4)
        r[f"agree:{v}"] = round(s[f"agree:{v}"] / nv, 4)
        r[f"wrong_given_agree:{v}"] = round(s[f"agree_wrong:{v}"] / max(s[f"agree:{v}"], 1), 4)
        r[f"agree_wrong_n:{v}"] = s[f"agree_wrong:{v}"]
    return r


if __name__ == "__main__":
    main()
