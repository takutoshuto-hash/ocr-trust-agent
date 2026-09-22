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
    return tpl.render(forms=pipeline.store.list_pending(), metrics=pipeline.metrics())


@app.get("/review/{form_id}", response_class=HTMLResponse)
def review_form(form_id: str):
    fd = pipeline.store.get_form(form_id)
    if not fd:
        raise HTTPException(404)
    from app.judge.agent import explain_review
    tpl = env.get_template("review.html")
    return tpl.render(fd=fd, FieldStatus=FieldStatus, explanation=explain_review(fd))


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


@app.get("/audit")
def audit(form_id: Optional[str] = None):
    return JSONResponse([e.model_dump(mode="json") for e in pipeline.store.list_audit(form_id)])


@app.get("/ledger")
def ledger():
    return [s.to_dict() for s in pipeline.store.list_ledger()]


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/review")
