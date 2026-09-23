"""FastAPI: 受付 API・要確認キュー・確認画面・指標・監査。"""
from __future__ import annotations

import json
from typing import Optional

import base64
import logging
import os
import secrets

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, PackageLoader, select_autoescape

from app.pipeline import Pipeline
from app.schemas import FieldStatus, OrderForm

app = FastAPI(title="OCR Trust Agent", version="0.2.0")
pipeline = Pipeline()
env = Environment(loader=PackageLoader("app", "templates"), autoescape=select_autoescape(["html"]))
log = logging.getLogger("uvicorn.error")

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
_AUTH_USER = os.getenv("REVIEW_USER", "")
_AUTH_PASS = os.getenv("REVIEW_PASSWORD", "")
if not (_AUTH_USER and _AUTH_PASS):
    log.warning("REVIEW_USER / REVIEW_PASSWORD 未設定: 認証なしで公開されます（ローカル開発のみ想定）")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """全ルート（/health 以外）に HTTP Basic 認証。資格情報は環境変数（Cloud Run では Secret）。"""
    if _AUTH_USER and _AUTH_PASS and request.url.path != "/health":
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, _AUTH_USER) and secrets.compare_digest(pw, _AUTH_PASS)
            except Exception:
                ok = False
        if not ok:
            return Response("認証が必要です", status_code=401, headers={"WWW-Authenticate": 'Basic realm="ocr-trust-agent"'})
    return await call_next(request)


@app.get("/health")          # /healthz は Cloud Run のフロントエンドに予約されていて 404 になるため /health を使う
def health():
    return {"ok": True, "extractor": pipeline.extractor.name, "store": type(pipeline.store).__name__,
            "profile": pipeline.profile, "auth": bool(_AUTH_USER and _AUTH_PASS)}


# ---- 受付 ----
@app.post("/forms")
async def submit_form(image: UploadFile = File(...), sender_id: str = Form(...), format_id: str = Form("fax_v1"),
                      hint_json: Optional[str] = Form(None)):
    data = await image.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"画像が大きすぎます（上限 {MAX_UPLOAD_BYTES // 1024 // 1024} MB）")
    if not data[:8].startswith((b"\x89PNG", b"\xff\xd8\xff")):
        raise HTTPException(415, "PNG または JPEG のみ受け付けます")
    hint = OrderForm.model_validate(json.loads(hint_json)) if hint_json else None
    if pipeline.extractor.name == "mock" and hint is None:
        raise HTTPException(400, "モック抽出器では hint_json（正解）が必要です。GEMINI_API_KEY を設定すると実画像を読みます。")
    fd = pipeline.process(data, sender_id=sender_id, format_id=format_id, hint=hint)
    return {"form_id": fd.form_id, "needs_review": fd.needs_review, "review_paths": fd.review_paths,
            "decisions": {p: d.model_dump() for p, d in fd.decisions.items()}}


@app.get("/forms/{form_id}")
def get_form(form_id: str):
    fd = pipeline.store.get_form(form_id)
    if not fd:
        raise HTTPException(404)
    return fd.model_dump(mode="json")


@app.get("/forms/{form_id}/image")
def get_image(form_id: str):
    img = pipeline.store.get_image(form_id)
    if not img:
        raise HTTPException(404)
    return Response(content=img, media_type="image/png")


# ---- 確認 ----
@app.get("/review", response_class=HTMLResponse)
def review_queue():
    tpl = env.get_template("queue.html")
    return tpl.render(forms=pipeline.store.list_pending(), metrics=pipeline.metrics(),
                      proposals=pipeline.store.list_proposals(status="pending"))


@app.get("/review/{form_id}", response_class=HTMLResponse)
def review_form(form_id: str):
    fd = pipeline.store.get_form(form_id)
    if not fd:
        raise HTTPException(404)
    from app.judge.zones import load_zones
    pipeline.mark_review_opened(form_id)   # 確認時間の実測（開いた時刻）
    fd = pipeline.store.get_form(form_id)  # 開いた時刻を含む最新の状態を取り直す（古いオブジェクトで上書きしない）
    if fd.explanation is None:             # 旧データ: 一度だけ生成して保存
        from app.judge.agent import explain_review
        fd.explanation = explain_review(fd)
        pipeline.store.put_form(fd, b"")
    tpl = env.get_template("review.html")
    zones = {k: list(v) for k, v in load_zones(fd.format_id).items()}
    return tpl.render(fd=fd, FieldStatus=FieldStatus, explanation=fd.explanation, zones_json=json.dumps(zones),
                      groups=_group_fields(fd))


_JP = {"zip": "郵便番号", "address": "住所", "name": "氏名", "name_kana": "フリガナ", "phone": "電話番号",
       "organization": "会社名", "product_code": "商品番号", "qty": "数量", "noshi_name": "のし名入れ"}


def _group_fields(fd) -> list[dict]:
    """確認画面用: 依頼主／お届け先ごとに項目をまとめ、日本語ラベルを付ける。"""
    import re
    groups: dict[str, dict] = {}
    for path, d in fd.decisions.items():
        m = re.fullmatch(r"applicant\.(\w+)", path)
        if m:
            key, title, field = "applicant", "ご依頼主", m.group(1)
        else:
            m = re.fullmatch(r"deliveries\[(\d+)\]\.(\w+)", path)
            key, title, field = f"d{m.group(1)}", f"お届け先 {int(m.group(1)) + 1}", m.group(2)
        g = groups.setdefault(key, {"title": title, "fields": [], "review": 0})
        g["fields"].append({"path": path, "label": _JP.get(field, field), "d": d, "v": fd.verdicts[path]})
        g["review"] += int(d.human_sees)
    return list(groups.values())


@app.post("/review/{form_id}")
async def confirm(form_id: str, request: Request):
    form = await request.form()
    corrections = {k[6:]: v for k, v in form.items() if k.startswith("field:")}
    pipeline.confirm(form_id, corrections, actor=f"human:{form.get('reviewer', 'anonymous')}")
    return RedirectResponse("/review", status_code=303)


@app.post("/api/confirm/{form_id}")
async def confirm_api(form_id: str, corrections: dict):
    fd = pipeline.confirm(form_id, corrections)
    return {"form_id": fd.form_id, "status": fd.status}


# ---- 運用 ----
@app.get("/metrics")
def metrics():
    return pipeline.metrics()


@app.post("/admin/retrain")
def retrain():
    return pipeline.retrain()


@app.post("/admin/reflect")
def reflect(days: int = 1):
    return pipeline.reflect(days=days)


@app.get("/proposals")
def proposals(status: Optional[str] = "pending"):
    return [p.model_dump(mode="json") for p in pipeline.store.list_proposals(status=status)]


@app.post("/proposals/{proposal_id}/{decision}")
async def decide_proposal(proposal_id: str, decision: str, request: Request):
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision は approve か reject")
    form = await request.form() if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded") else {}
    actor = f"human:{form.get('reviewer', 'anonymous')}" if form else "human:api"
    p = pipeline.decide_proposal(proposal_id, decision == "approve", actor)
    if form:
        return RedirectResponse("/review", status_code=303)
    return p.model_dump(mode="json")


@app.get("/rules")
def rules():
    return [r.model_dump(mode="json") for r in pipeline.store.list_rules()]


# ---- ダッシュボード ----
@app.get("/api/dashboard")
def dashboard_api(source: str = "live"):
    """source=live: 本番データ / sim: 同梱のシミュレーション曲線（デモ用）"""
    data = pipeline.dashboard_data()
    data["sim"] = _load_sim_curves()
    return data


def _load_sim_curves() -> dict:
    import csv
    from app.config import ROOT
    out = {}
    for name, path in (("mock", ROOT / "eval/out/curve_mock_handwriting.csv"),
                       ("gemini_baseline", ROOT / "eval/out/curve_gemini_200x14_vertex.csv"),
                       ("gemini", ROOT / "eval/out/curve_gemini_200x14_improved.csv")):
        if path.exists():
            with path.open(encoding="utf-8") as f:
                out[name] = [{k: (float(v) if v not in ("", "None", "True", "False") and k != "day" else v) for k, v in row.items()} for row in csv.DictReader(f)]
    return out


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    return env.get_template("dashboard.html").render()


@app.get("/audit")
def audit(form_id: Optional[str] = None):
    return JSONResponse([e.model_dump(mode="json") for e in pipeline.store.list_audit(form_id)])


@app.get("/ledger")
def ledger():
    return [s.to_dict() for s in pipeline.store.list_ledger()]


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/review")
