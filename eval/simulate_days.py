"""日次シミュレーション: 「数をさばくほど要確認率が下がる」曲線を作る。

各日: N枚を処理 → 人が見る項目（要確認＋監査サンプル）は正解に直す（＝教師データ）
→ 自動確定項目は正解と照合して"見逃し誤り"を集計 → 夜間に再学習。
日ごとの要確認率・自動確定誤り率・台帳の昇格数を CSV に出す。

抽出器:
  --extractor mock    合成帳票の正解に誤りを混入（速い。誤り率は Gemini 実測に合わせて校正済み）
  --extractor gemini  手書き合成帳票を実際に Gemini で読む（遅い・課金。実データに近い曲線）
--images を付けると帳票画像を描画して渡す（空欄検知・マスクが有効になる）。gemini では常に描画。

使い方:
  python eval/simulate_days.py --days 10 --per-day 200 --senders 120 --images
  python eval/simulate_days.py --extractor gemini --days 6 --per-day 30 --senders 20 --out eval/out/curve_gemini.csv
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data" / "synthetic"))

from generate_forms import fonts, load_master, make_truth, phone, render  # noqa: E402

from app.config import settings  # noqa: E402
from app.learn import CorrectionRouter  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402
from app.schemas import FieldStatus, OrderForm  # noqa: E402
from app.store import MemoryStore  # noqa: E402
from app.trust import Policy  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--per-day", type=int, default=200)
    ap.add_argument("--senders", type=int, default=120)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--extractor", choices=["mock", "gemini"], default="mock")
    ap.add_argument("--images", action="store_true", help="帳票画像を描画して渡す（mock でも空欄検知を有効にする）")
    ap.add_argument("--out", default="eval/out/curve.csv")
    ap.add_argument("--export-state", default=None, help="運用状態（台帳・教師データ・ルーターモデル）をこのディレクトリに書き出す")
    ap.add_argument("--workers", type=int, default=1, help="1日分の帳票を並列に処理するスレッド数（Gemini は I/O 待ちが主なので 8 程度）")
    ap.add_argument("--field-log", default=None, help="項目ごとの判定・真偽・ルーター p・閾値を日別に書き出す CSV（誤りの内訳分析用）")
    a = ap.parse_args()

    rng = random.Random(a.seed)
    zips, products = load_master()
    fnts = fonts()
    pool = [{"id": f"S{i:04d}", "phone": phone(rng)} for i in range(a.senders)]
    policy = Policy.load(settings.policy_path)
    rc = policy.router
    tmp = Path(tempfile.mkdtemp())   # モデルは一時ディレクトリに（本番モデルを汚さない）
    router = CorrectionRouter(tmp, min_samples=int(rc.get("min_training_samples", 200)),
                              target_error_rate=float(rc.get("target_error_rate", 0.005)))
    if a.extractor == "gemini":
        if not settings.use_gemini:
            raise SystemExit("GEMINI_API_KEY が未設定です")
        from app.extract.gemini import GeminiExtractor
        extractor = GeminiExtractor(api_key=settings.gemini_api_key, model=settings.gemini_model)
        use_images = True
    else:
        from app.extract.mock import MockExtractor
        extractor = MockExtractor()
        use_images = a.images
    pipe = Pipeline(store=MemoryStore(), extractor=extractor, policy=policy, router=router, seed=a.seed, explain=False,
                    budget_enabled=False)   # 日付が進まないシミュレーションでは日次予算を無効化
    print(f"extractor={extractor.name} images={use_images} days={a.days} per_day={a.per_day} senders={a.senders}", flush=True)

    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    field_log = None
    if a.field_log:
        Path(a.field_log).parent.mkdir(parents=True, exist_ok=True)
        field_log = open(a.field_log, "w", newline="", encoding="utf-8")
        field_writer = csv.DictWriter(field_log, fieldnames=["day", "form_id", "sender", "path", "field_type", "status", "audit", "level",
                                                             "judge_ok", "agreement", "router_p", "router_threshold", "resolved", "correct"])
        field_writer.writeheader()
    for day in range(1, a.days + 1):
        t0 = time.perf_counter()
        n_fields = n_review = n_auto = n_auto_wrong = n_human_corr = n_ocr_wrong = 0
        # 1日分の帳票を先に生成し、処理（抽出＋ジャッジ＋判定）は並列、確定（学習）は逐次
        batch = []
        for i in range(a.per_day):
            truth_d, sender = make_truth(rng, zips, products, pool)
            truth = OrderForm.model_validate(truth_d)
            if use_images:
                buf = io.BytesIO(); render(truth_d, fnts, rng).save(buf, format="PNG"); image = buf.getvalue()
            else:
                image = json.dumps(truth_d, ensure_ascii=False).encode() + bytes([day % 251, i % 251])
            batch.append((image, sender, truth))

        def _proc(item):
            image, sender, truth = item
            for attempt in range(3):   # API の一時エラーは再試行
                try:
                    return pipe.process(image, sender_id=sender, format_id="fax_v1", hint=truth), truth
                except Exception as e:
                    if attempt == 2:
                        print("  process failed:", str(e)[:120], flush=True); return None, truth
                    time.sleep(2 * (attempt + 1))

        if a.workers > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=a.workers) as ex:
                results = list(ex.map(_proc, batch))
        else:
            results = [_proc(b) for b in batch]

        for fd, truth in results:
            if fd is None:
                continue
            tflat = truth.flatten()
            corrections = {}
            for path, d in fd.decisions.items():
                n_fields += 1
                ok = _norm(d.value) == _norm(tflat[path])
                n_ocr_wrong += (not ok)
                if field_log is not None:
                    v = fd.verdicts[path]
                    field_writer.writerow({"day": day, "form_id": fd.form_id, "sender": fd.sender_id, "path": path, "field_type": d.field_type,
                                           "status": d.status.value, "audit": int(d.audit), "level": int(d.level), "judge_ok": int(d.judge_ok),
                                           "agreement": ("" if v.agreement is None else int(v.agreement)),
                                           "router_p": ("" if d.p_correction is None else round(d.p_correction, 5)),
                                           "router_threshold": ("" if pipe.router.threshold is None else round(pipe.router.threshold, 5)),
                                           "resolved": int(d.resolved_from is not None), "correct": int(ok)})
                if d.status == FieldStatus.REVIEW:
                    n_review += 1
                else:
                    n_auto += 1
                    n_auto_wrong += (not ok)              # 自動確定の誤り（真値と照合。監査サンプル分は人が直す）
                if d.human_sees and not ok:
                    corrections[path] = tflat[path]       # 人が原本を見て直す（要確認＋監査サンプル）
                    n_human_corr += 1
            pipe.confirm(fd.form_id, corrections, actor="human:sim")
        if n_fields == 0:
            print(f"day {day:2d}: 処理できた帳票がありません（API エラー等）。この日は記録せず続行", flush=True)
            continue
        summary = pipe.retrain()                            # 夜間再学習
        promoted = sum(1 for s in pipe.store.list_ledger() if s.level > 0)
        m = pipe.metrics()
        row = {"day": day, "forms": a.per_day, "fields": n_fields,
               "ocr_error_rate": round(n_ocr_wrong / n_fields, 4),                       # 抽出そのものの誤り率
               "review_rate": round(n_review / n_fields, 4),
               "auto_error_rate": round(n_auto_wrong / n_auto, 5) if n_auto else 0.0,   # 真値比較（神の視点）
               "auto_error_rate_audited": m["auto_error_rate_audited"],                   # 監査サンプルからの推定
               "human_corrections": n_human_corr, "ledger_promoted_keys": promoted,
               "router_trained": summary.get("trained", False), "router_threshold": summary.get("threshold"),
               "seconds": round(time.perf_counter() - t0, 1)}
        rows.append(row)
        print(f"day {day:2d}: ocr_err {row['ocr_error_rate']:.3f}  review {row['review_rate']:.3f}  "
              f"auto_err {row['auto_error_rate']:.4f} (audited {row['auto_error_rate_audited']})  "
              f"promoted {promoted:4d}  router {'yes' if row['router_trained'] else 'no '} thr={row['router_threshold']}  {row['seconds']}s", flush=True)
        with out.open("w", newline="", encoding="utf-8") as f:   # 毎日書き出す（途中で止めても残る）
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
        if field_log is not None:
            field_log.flush()
    if field_log is not None:
        field_log.close()
        print(f"field log -> {a.field_log}")
    print(f"-> {out}")

    if a.export_state:
        import shutil
        from datetime import datetime, timedelta, timezone
        d = Path(a.export_state); d.mkdir(parents=True, exist_ok=True)
        (d / "ledger.json").write_text(json.dumps([s.to_dict() for s in pipe.store.list_ledger()], ensure_ascii=False), encoding="utf-8")
        # 教師データ: created_at をシミュレーション日に付け替え、夜間の振り返り窓（1日）に入らないようにする
        recs = pipe.store.list_training()
        per_day = max(1, len(recs) // a.days)
        now = datetime.now(timezone.utc)
        with (d / "training.jsonl").open("w", encoding="utf-8") as f:
            for i, r in enumerate(recs):
                day = min(a.days, i // per_day + 1)
                r.created_at = now - timedelta(days=(a.days - day + 1))
                r.form_id = "sim-" + r.form_id
                f.write(r.model_dump_json() + "\n")
        if router.path.exists():
            shutil.copy(router.path, d / "router.joblib")
        (d / "META.json").write_text(json.dumps({"source": "simulation", "extractor": extractor.name, "days": a.days, "per_day": a.per_day,
                                                  "senders": a.senders, "seed": a.seed, "exported_at": now.isoformat(),
                                                  "final_review_rate": rows[-1]["review_rate"], "final_auto_error_rate": rows[-1]["auto_error_rate"]},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"state exported -> {d} (ledger {len(pipe.store.list_ledger())} keys, training {len(recs)} records)")


def _norm(x):
    return str(x if x is not None else "").replace(" ", "").replace("　", "").replace("-", "").upper()


if __name__ == "__main__":
    main()
