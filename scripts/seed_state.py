"""シミュレーション運用の状態（台帳・教師データ・ルーターモデル）を本番ストアに読み込む。

用途: 「育った後」のデモ・実測。合成データ由来であることを監査ログ（state_seeded）に明記する。
使い方:
  python scripts/seed_state.py --state eval/out/state_mock10d --project ocr-trust-agent
  → Firestore に台帳・教師データを書き込み、models/router.joblib を差し替える（次の deploy で有効）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT", "ocr-trust-agent"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-training", action="store_true", help="教師データは書き込まない（台帳とモデルだけ。Firestore の読み書き量を抑える）")
    a = ap.parse_args()

    os.environ["STORE_BACKEND"] = "firestore"
    os.environ["GOOGLE_CLOUD_PROJECT"] = a.project
    from app.schemas import AuditEvent, TrainingRecord
    from app.store import get_store
    from app.trust.ledger import LedgerStat

    d = Path(a.state)
    meta = json.loads((d / "META.json").read_text(encoding="utf-8"))
    ledger = [LedgerStat.from_dict(x) for x in json.loads((d / "ledger.json").read_text(encoding="utf-8"))]
    recs = [TrainingRecord.model_validate_json(line) for line in (d / "training.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"state: {meta}\nledger keys: {len(ledger)}  training records: {len(recs)}")
    if a.dry_run:
        return

    store = get_store()
    for s in ledger:
        store.put_ledger(s)
    if not a.skip_training:
        for i in range(0, len(recs), 400):
            store.add_training(recs[i:i + 400])
    if (d / "router.joblib").exists():
        (ROOT / "models").mkdir(exist_ok=True)
        shutil.copy(d / "router.joblib", ROOT / "models" / "router.joblib")
        print("router.joblib -> models/ (deploy で反映)")
    store.add_audit(AuditEvent(form_id="-", event="state_seeded", actor="human:operator", detail={
        "note": "シミュレーション運用の状態を読み込み（合成データ由来。実運用データではない）", **meta,
        "ledger_keys": len(ledger), "training_records": 0 if a.skip_training else len(recs),
        "router_model": "seeded from simulation (models/router.joblib)"}))
    print("seeded.")


if __name__ == "__main__":
    main()
