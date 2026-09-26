"""評価: 正解付きの帳票（合成 or 実手書きのスキャン）に対して抽出→判定を回し、項目別の正解率・要確認率・自動確定の誤り率を出す。

GEMINI_API_KEY（または Vertex AI の設定）があれば実画像を Gemini で読む。なければモック抽出器（正解に誤りを混入）。
使い方:
  python eval/run_eval.py --dir data/synthetic/out --limit 100
  python eval/run_eval.py --dir data/measurement/handwriting/scans --truth-dir data/measurement/handwriting --report eval/out/handwriting_eval.csv
入力: <dir> の hw_01.pdf / .png / .jpg。正解は同名の .json（<dir> か --truth-dir）。PDF は 1 ページ目を画像にし、位置合わせ（向き・傾き・ずれ）を通す。
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

from app.pipeline import Pipeline
from app.schemas import FieldStatus, OrderForm
from app.store import MemoryStore

IMAGE_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg")


def find_inputs(d: Path, truth_dir: Path | None) -> list[tuple[Path, Path]]:
    """(画像, 正解 json) の組を、画像のファイル名（拡張子除く）で結ぶ。"""
    out = []
    imgs = {p.stem: p for p in sorted(d.iterdir()) if p.suffix.lower() in IMAGE_SUFFIXES}
    for stem, img in imgs.items():
        for cand in ((d / f"{stem}.json"), (truth_dir / f"{stem}.json") if truth_dir else None):
            if cand and cand.exists():
                out.append((img, cand)); break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/synthetic/out")
    ap.add_argument("--truth-dir", default=None, help="正解 json が画像と別のフォルダにあるとき")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--mock", action="store_true", help="GEMINI_API_KEY があってもモック抽出器を使う")
    ap.add_argument("--report", default=None, help="項目ごとの結果を CSV に書く（file, path, truth, value, status, correct, reason）")
    a = ap.parse_args()

    extractor = None
    if a.mock:
        from app.extract.mock import MockExtractor
        extractor = MockExtractor()
    pipe = Pipeline(store=MemoryStore(), extractor=extractor, explain=False)
    print(f"extractor = {pipe.extractor.name}")
    per_ft = defaultdict(lambda: {"n": 0, "correct": 0, "review": 0, "auto_wrong": 0})
    hal = {"blank_truth": 0, "invented": 0, "invented_caught": 0}   # 空欄の正解数 / 創作された数 / うち検知された数
    rows: list[dict] = []
    failed: list[str] = []
    d = Path(a.dir)
    pairs = find_inputs(d, Path(a.truth_dir) if a.truth_dir else None)[: a.limit]
    if not pairs:
        raise SystemExit(f"{d} に画像（pdf/png/jpg）と同名の正解 json が見つかりません")
    from app.judge.zones import registration_report
    for img_path, jf in pairs:
        meta = json.loads(jf.read_text(encoding="utf-8"))
        truth = OrderForm.model_validate(meta["truth"])
        image = img_path.read_bytes()
        reg = registration_report(image, meta.get("format_id", "fax_v1"))
        try:
            fd = pipe.process(image, sender_id=meta["sender_id"], format_id=meta.get("format_id", "fax_v1"), hint=truth)
        except Exception as ex:           # 混雑（429）などで 1 枚が読めなくても残りを続ける
            print(f"  {img_path.name}: 失敗 {type(ex).__name__}: {str(ex)[:160]}", flush=True)
            failed.append(img_path.name)
            continue
        tflat = truth.flatten()
        wrong = [p for p, dcs in fd.decisions.items() if _norm(dcs.value) != _norm(tflat.get(p))]
        print(f"  {img_path.name}: review {len(fd.review_paths)}/{len(fd.decisions)}  wrong {len(wrong)}  "
              f"({fd.extraction.latency_ms} ms)  位置合わせ: 逆さ={reg.get('upside_down')} 傾き={reg.get('skew_deg')}° 枠={reg.get('frame_after')}", flush=True)
        for p in wrong:
            dcs = fd.decisions[p]
            print(f"      {p}: 読み {dcs.value!r} / 正解 {tflat.get(p)!r} / {'自動確定' if dcs.status == FieldStatus.AUTO and not dcs.audit else '人が確認'}")
        for path, dcs in fd.decisions.items():
            s = per_ft[dcs.field_type]
            s["n"] += 1
            ok = _norm(dcs.value) == _norm(tflat.get(path))
            s["correct"] += ok
            s["review"] += dcs.status == FieldStatus.REVIEW
            s["auto_wrong"] += (dcs.status == FieldStatus.AUTO and not ok)
            tv = tflat.get(path)
            if isinstance(tv, str) and not tv:
                hal["blank_truth"] += 1
                if str(dcs.value).strip():
                    hal["invented"] += 1
                    hal["invented_caught"] += any(c.name == "blank_zone" and c.status.value == "fail"
                                                  for c in fd.verdicts[path].checks)   # 空欄検知そのものの検知率
            rows.append({"file": img_path.name, "path": path, "truth": tv, "value": dcs.value, "status": dcs.status.value,
                         "audit": dcs.audit, "correct": int(ok), "resolved_from": dcs.resolved_from,
                         "reason": fd.verdicts[path].reason, "why": getattr(dcs, "why", "")})

    print(f"\n{'field_type':28} {'n':>5} {'acc':>7} {'review':>7} {'auto_err':>9}")
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
    if failed:
        print(f"\n読めなかった帳票 {len(failed)} 枚: {', '.join(failed)}（混雑なら時間をおいて再実行）")
    if a.report and rows:
        Path(a.report).parent.mkdir(parents=True, exist_ok=True)
        with open(a.report, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
        print(f"\nwrote {a.report}")


def _norm(x):
    """比較の正規化: 空白・ハイフンの有無・全角半角の英数は同じとみなす（現場ではハイフンを省くことがある）。"""
    s = str(x if x is not None else "").replace(" ", "").replace("　", "").replace("-", "").replace("ー", "").replace("−", "").replace("‐", "")
    return s.translate(str.maketrans("０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ", "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")).upper()


if __name__ == "__main__":
    main()
