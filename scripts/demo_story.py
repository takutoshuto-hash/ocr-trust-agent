"""動画の山場を毎回同じに再生する台本スクリプト。

  平常運転 → 事故（FAX の劣化で「7」が「1」に見える）→ 振り返りエージェントが読み取りのコツを提案 → 人が承認
  → 翌日も直らない → エージェントが自分の提案の効果を測り、同じ提案はガバナンスが自動却下、効かなかったコツは取り消しを提案
  → 人が承認 → 事故の原因を直す → 平常に戻る

使い方（撮影時。別のターミナルでサーバーを起動しておく）:
  uvicorn app.main:app --port 8000            # モック抽出器（GEMINI_API_KEY 未設定・STORE_BACKEND=memory）
  python scripts/demo_story.py run             # 場面ごとに Enter で進む。ブラウザで /review /briefing /dashboard を見せる
  python scripts/demo_story.py run --auto      # リハーサル: 止まらずに最後まで（承認も API で行う）
  python scripts/demo_story.py run --images data/synthetic/out   # 合成帳票の画像を使う（確認画面に本物らしい紙が出る）

台本の中身（scene 関数）は tests/test_demo_story.py でも同じものを回し、毎回同じ結末になることを固定している。
"""
from __future__ import annotations

import argparse
import io
import json
import random
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data" / "synthetic"))

DEFECT = {"field_type": "deliveries.zip", "from": "7", "to": "1", "rate": 1.0}


# ---------------------------------------------------------------------- 帳票の供給
class FormSource:
    """正解つきの帳票を順に出す。--images があれば合成帳票の PNG と JSON、無ければ正解だけ作って白紙の PNG を付ける。"""

    def __init__(self, images_dir: str | None, seed: int = 11, senders: int = 24):
        self.items: list[tuple[bytes, str, dict]] = []
        self.i = 0
        if images_dir:
            for js in sorted(Path(images_dir).glob("*.json")):
                png = js.with_suffix(".png")
                if not png.exists():
                    continue
                d = json.loads(js.read_text(encoding="utf-8"))
                self.items.append((png.read_bytes(), d["sender_id"], d["truth"]))
            if not self.items:
                raise SystemExit(f"{images_dir} に form_*.png / .json がありません（python data/synthetic/generate_forms.py --n 200）")
        else:
            # 正解はその場で作る。画像は手書き風フォントがあれば合成帳票を描き、無ければ欄に「墨」だけ置いた紙にする
            # （空欄検知が正解と食い違わないように。白紙だと全項目が「空欄なのに値がある＝創作」と判定される）
            from generate_forms import load_master, make_truth, phone
            self.rng = random.Random(seed)
            zips, products = load_master()
            self.pool = [{"id": f"S{i:04d}", "phone": phone(self.rng)} for i in range(senders)]
            self.make = lambda: make_truth(self.rng, zips, products, self.pool)
            try:
                from generate_forms import fonts, render
                self.fonts = fonts()
                self.render = render
            except Exception:
                self.fonts = None

    def next(self) -> tuple[bytes, str, dict]:
        if self.items:
            item = self.items[self.i % len(self.items)]
            self.i += 1
            return item
        truth, sender = self.make()
        self.i += 1
        if self.fonts:
            img = self.render(truth, self.fonts, self.rng)
            buf = io.BytesIO(); img.save(buf, "PNG")
            return buf.getvalue(), sender, truth
        return _ink_png(truth, self.i, self.rng), sender, truth


def _ink_png(truth: dict, n: int, rng: random.Random, format_id: str = "fax_v1") -> bytes:
    """フォントが無いときの代替画像: 値のある欄にだけ太い筆跡風の線を置いた白紙（空欄検知と整合する）。"""
    from PIL import Image, ImageDraw
    from app.judge.zones import load_zones
    from app.schemas import OrderForm
    W, H = 1240, 1754
    img = Image.new("L", (W, H), 255 - (n % 3))
    d = ImageDraw.Draw(img)
    flat = OrderForm.model_validate(truth).flatten()
    for path, zone in load_zones(format_id).items():
        v = flat.get(path)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        x1, y1, x2, y2 = int(zone[0] * W), int(zone[1] * H), int(zone[2] * W), int(zone[3] * H)
        n_ch = max(2, min(12, len(str(v))))
        for k in range(n_ch):                       # 1 文字 ≈ 24px 幅の「くしゃくしゃ」
            cx = x1 + 12 + k * 26
            if cx + 20 > x2:
                break
            for _ in range(4):
                d.line([(cx + rng.randint(0, 18), y1 + 8 + rng.randint(0, max(1, y2 - y1 - 16))),
                        (cx + rng.randint(0, 18), y1 + 8 + rng.randint(0, max(1, y2 - y1 - 16)))], fill=20, width=3)
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------- サーバーへの窓口
class HttpClient:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _req(self, method: str, path: str, data: bytes | None = None, headers: dict | None = None):
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {}, method=method)
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.load(r)

    def submit(self, image: bytes, sender_id: str, truth: dict) -> dict:
        b = "XXDEMOBOUNDARY"
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"sender_id\"\r\n\r\n{sender_id}\r\n"
                f"--{b}\r\nContent-Disposition: form-data; name=\"hint_json\"\r\n\r\n{json.dumps(truth, ensure_ascii=False)}\r\n"
                f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"f.png\"\r\nContent-Type: image/png\r\n\r\n").encode() \
            + image + f"\r\n--{b}--\r\n".encode()
        return self._req("POST", "/forms", body, {"Content-Type": f"multipart/form-data; boundary={b}"})

    def confirm(self, form_id: str, corrections: dict) -> dict:
        return self._req("POST", f"/api/confirm/{form_id}", json.dumps(corrections, ensure_ascii=False).encode(), {"Content-Type": "application/json"})

    def retrain(self) -> dict:
        return self._req("POST", "/admin/retrain", b"", {})

    def reflect(self, since: datetime) -> dict:
        return self._req("POST", f"/admin/reflect?since={urllib.request.quote(since.isoformat())}", b"", {})

    def proposals(self, status: str = "pending") -> list[dict]:
        return self._req("GET", f"/proposals?status={status}")

    def decide(self, proposal_id: str, approve: bool) -> dict:
        return self._req("POST", f"/proposals/{proposal_id}/{'approve' if approve else 'reject'}", b"", {})

    def rules(self) -> list[dict]:
        return self._req("GET", "/rules")

    def set_defect(self, defect: dict | None) -> dict:
        body = json.dumps(defect if defect else {"clear": True}).encode()
        return self._req("POST", "/admin/mock/defect", body, {"Content-Type": "application/json"})

    def dashboard(self) -> dict:
        return self._req("GET", "/api/dashboard")


class LocalClient:
    """テスト用: サーバーを立てずに Pipeline を直接叩く（同じ台本を回す）。"""

    def __init__(self, pipeline):
        from app.schemas import OrderForm
        self.pipe = pipeline
        self._OrderForm = OrderForm

    def submit(self, image: bytes, sender_id: str, truth: dict) -> dict:
        fd = self.pipe.process(image, sender_id=sender_id, hint=self._OrderForm.model_validate(truth))
        return {"form_id": fd.form_id, "review_paths": fd.review_paths, "decisions": {p: d.model_dump() for p, d in fd.decisions.items()}}

    def confirm(self, form_id: str, corrections: dict) -> dict:
        fd = self.pipe.confirm(form_id, corrections)
        return {"form_id": fd.form_id, "status": fd.status}

    def retrain(self) -> dict:
        return self.pipe.retrain()

    def reflect(self, since: datetime) -> dict:
        return self.pipe.reflect(since=since)

    def proposals(self, status: str = "pending") -> list[dict]:
        return [p.model_dump(mode="json") for p in self.pipe.store.list_proposals(status=status)]

    def decide(self, proposal_id: str, approve: bool) -> dict:
        return self.pipe.decide_proposal(proposal_id, approve, "human:demo").model_dump(mode="json")

    def rules(self) -> list[dict]:
        return [r.model_dump(mode="json") for r in self.pipe.store.list_rules()]

    def set_defect(self, defect: dict | None) -> dict:
        inner = getattr(self.pipe.extractor, "_inner", self.pipe.extractor)
        if defect:
            inner.set_defect(defect["field_type"], defect["from"], defect["to"], defect.get("rate", 1.0))
        else:
            inner.clear_defects()
        return {"defects": inner.defects}

    def dashboard(self) -> dict:
        return self.pipe.dashboard_data()


# ---------------------------------------------------------------------- 台本（場面）
def flatten(truth: dict) -> dict:
    from app.schemas import OrderForm
    return OrderForm.model_validate(truth).flatten()


def run_day(client, source: FormSource, n_forms: int, *, leave_pending: int = 0, log=print) -> dict:
    """1 日ぶんの帳票を流し、要確認は人（台本）が正解に直して確定する。leave_pending 枚は確認画面用に残す。"""
    fields = review = zip_wrong = zip_wrong_auto = 0
    left: list[tuple[str, dict]] = []
    for k in range(n_forms):
        image, sender, truth = source.next()
        r = client.submit(image, sender, truth)
        flat = flatten(truth)
        fields += len(r["decisions"]); review += len(r["review_paths"])
        wrong = [p for p, d in r["decisions"].items() if p.endswith(".zip") and str(d.get("value")) != str(flat.get(p))]
        zip_wrong += len(wrong)
        zip_wrong_auto += sum(1 for p in wrong if p not in r["review_paths"])   # 読み違いが人に回らず自動確定した数（0 であるべき）
        corr = {p: flat[p] for p in r["review_paths"]}
        if k >= n_forms - leave_pending:
            left.append((r["form_id"], corr))
        else:
            client.confirm(r["form_id"], corr)
    return {"forms": n_forms, "fields": fields, "review": review, "review_rate": round(review / fields, 3) if fields else None,
            "zip_wrong": zip_wrong, "zip_wrong_auto": zip_wrong_auto, "left": left}


def confirm_left(client, left: list[tuple[str, dict]]) -> int:
    n = 0
    for form_id, corr in left:
        try:
            client.confirm(form_id, corr); n += 1
        except Exception:
            pass          # 人がすでに確定した（HTTP で 4xx）
    return n


def run_night(client, since: datetime) -> dict:
    """夜間: 再学習 → 振り返り。提案の一覧を返す。"""
    client.retrain()
    out = client.reflect(since)
    return {"proposals": out["proposals"], "records": out["analysis"].get("window_records")}


def is_target_rule(p: dict) -> bool:
    ev = p.get("evidence") or {}
    return p.get("kind") == "rule" and ev.get("field_type") == DEFECT["field_type"] and ev.get("from") == DEFECT["to"] and ev.get("to") == DEFECT["from"]


def approve_all(client, kinds: tuple[str, ...]) -> list[dict]:
    done = []
    for p in client.proposals("pending"):
        if p["kind"] in kinds:
            done.append(client.decide(p["proposal_id"], True))
    return done


def storyboard(client, source: FormSource, *, seed_days: int = 2, per_day: int = 120, accident_forms: int = 30,
               leave_pending: int = 0, auto: bool = True, pause=None, log=print) -> dict:
    """台本全体。pause(scene_text) を渡すと場面ごとに止まる（撮影時）。auto=True なら承認も台本が行う。"""
    result: dict = {}
    pause = pause or (lambda text: None)

    log(f"\n■ 場面 0  平常運転（{seed_days} 日 × {per_day} 枚）: 台帳が育ち、郵便番号などは自動で確定するようになる")
    for d in range(seed_days):
        start = datetime.now(timezone.utc)
        day = run_day(client, source, per_day, log=log)
        client.retrain()
        log(f"   {d + 1} 日目: {day['forms']} 枚・人の確認 {day['review_rate']:.0%}")
    result["seed_last_review_rate"] = day["review_rate"]
    pause("ダッシュボード /dashboard（運用）で、確認の割合が下がり「自動で確定できる範囲」が広がった様子を見せる")

    log("\n■ 場面 1  事故: FAX の劣化で、お届け先の郵便番号の「7」が「1」に見える")
    client.set_defect(DEFECT)
    day1_start = datetime.now(timezone.utc)
    day1 = run_day(client, source, accident_forms, leave_pending=(0 if auto else leave_pending), log=log)
    log(f"   郵便番号の読み違い {day1['zip_wrong']} 件。一覧に無い番号なので検証で止まり、人の確認に回った（確認 {day1['review_rate']:.0%}。自動確定された読み違い {day1['zip_wrong_auto']} 件）")
    result["accident_day1"] = {k: v for k, v in day1.items() if k != "left"}
    pause(f"確認画面 /review で、赤枠の郵便番号を直して確定する場面を撮る（{len(day1['left'])} 枚残してある）。撮り終えたら Enter")
    confirm_left(client, day1["left"])

    log("\n■ 場面 2  その夜: 振り返りエージェントが修正ログを集計し、読み取りのコツを提案する")
    night1 = run_night(client, day1_start)
    for p in night1["proposals"]:
        log(f"   [{p['status']}] {p['title']}")
    targets = [p for p in night1["proposals"] if is_target_rule(p) and p["status"] == "pending"]
    result["night1_target_pending"] = len(targets)
    if auto:
        approved = [client.decide(p["proposal_id"], True) for p in targets]
        log(f"   → 台本が承認: {len(approved)} 件（撮影時は /briefing で「はい」）")
    else:
        pause("朝のブリーフィング /briefing を開き、郵便番号のコツの質問に「はい」を押す。押したら Enter")
        if not any(r.get("field_type") == DEFECT["field_type"] for r in client.rules()):
            log("   （まだ承認されていなかったので、台本が承認します）"); [client.decide(p["proposal_id"], True) for p in targets]
    result["rules_after_night1"] = [r for r in client.rules() if r.get("field_type") == DEFECT["field_type"] and r.get("active", True)]

    log("\n■ 場面 3  翌日: コツを覚えても直らない（原因は紙の側にある）")
    day2_start = datetime.now(timezone.utc)
    day2 = run_day(client, source, accident_forms, log=log)
    log(f"   郵便番号の読み違い {day2['zip_wrong']} 件（昨日 {day1['zip_wrong']} 件）。自動確定は止まったまま、間違いは人が全部止めている")
    result["accident_day2"] = {k: v for k, v in day2.items() if k != "left"}

    log("\n■ 場面 4  その夜: エージェントが自分の提案の効果を測る")
    night2 = run_night(client, day2_start)
    for p in night2["proposals"]:
        log(f"   [{p['status']}] {p['title']}")
    dup = [p for p in night2["proposals"] if is_target_rule(p) and p["status"] == "rejected"]
    retracts = [p for p in night2["proposals"] if p["kind"] == "retract" and (p.get("evidence") or {}).get("field_type") == DEFECT["field_type"]]
    result["night2_duplicate_rejected"] = len(dup)
    result["night2_retract_pending"] = len(retracts)
    log(f"   → 同じコツの再提案はガバナンスが自動却下（{len(dup)} 件）。効いていないコツの取り消しを提案（{len(retracts)} 件）")
    if auto:
        for p in retracts:
            client.decide(p["proposal_id"], True)
        log("   → 台本が取り消しを承認（撮影時は /briefing で「はい」）")
    else:
        pause("朝のブリーフィング /briefing で「郵便番号のコツは効いていません」に「はい」。提案履歴 /proposals?status=rejected に自動却下も見せる。押したら Enter")
        for p in retracts:
            if client.proposals("pending") and any(q["proposal_id"] == p["proposal_id"] for q in client.proposals("pending")):
                client.decide(p["proposal_id"], True)
    result["rules_after_night2"] = [r for r in client.rules() if r.get("field_type") == DEFECT["field_type"] and r.get("active", True)]

    log("\n■ 場面 5  原因を直す（FAX を修理）→ 平常に戻る")
    client.set_defect(None)
    day3 = run_day(client, source, accident_forms, log=log)
    client.retrain()
    log(f"   郵便番号の読み違い {day3['zip_wrong']} 件。確認 {day3['review_rate']:.0%}")
    result["recovery_day"] = {k: v for k, v in day3.items() if k != "left"}
    pause("ダッシュボード /dashboard（運用）で、事故の日だけ確認が跳ね、戻った様子を見せる。監査ログ /audit にすべて残っている")
    return result


# ---------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="台本を最初から最後まで")
    r.add_argument("--base", default="http://127.0.0.1:8000")
    r.add_argument("--images", default=None, help="合成帳票のフォルダ（form_*.png と .json）。無ければ白紙の画像で進める")
    r.add_argument("--seed-days", type=int, default=2)
    r.add_argument("--per-day", type=int, default=120)
    r.add_argument("--accident-forms", type=int, default=30)
    r.add_argument("--leave", type=int, default=3, help="事故の日に確認画面用に残す枚数（--auto では 0）")
    r.add_argument("--auto", action="store_true", help="止まらずに最後まで。承認も API で行う（リハーサル・動作確認）")
    d = sub.add_parser("defect", help="事故注入だけ切り替える")
    d.add_argument("--base", default="http://127.0.0.1:8000")
    d.add_argument("--off", action="store_true")
    a = ap.parse_args()

    client = HttpClient(a.base)
    try:
        client.dashboard()
    except Exception as e:
        raise SystemExit(f"サーバーに届きません（{a.base}）。先に uvicorn app.main:app --port 8000 を起動してください: {e}")

    if a.cmd == "defect":
        print(client.set_defect(None if a.off else DEFECT)); return

    def pause(text: str):
        print(f"\n   ▶ {text}")
        input("   （Enter で次の場面へ）")

    t0 = time.perf_counter()
    res = storyboard(client, FormSource(a.images), seed_days=a.seed_days, per_day=a.per_day, accident_forms=a.accident_forms,
                     leave_pending=a.leave, auto=a.auto, pause=None if a.auto else pause)
    print(f"\n完了（{time.perf_counter() - t0:.0f} 秒）")
    print(json.dumps({k: v for k, v in res.items() if not k.startswith("rules_")}, ensure_ascii=False, indent=1))
    print(f"確認画面 {a.base}/review ・ ブリーフィング {a.base}/briefing ・ ダッシュボード {a.base}/dashboard ・ 提案履歴 {a.base}/proposals?status=")


if __name__ == "__main__":
    main()
