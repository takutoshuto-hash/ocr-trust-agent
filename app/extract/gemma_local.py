"""高機密プロファイル用: VPC 内 / オンプレの Gemma（Ollama 互換 API）で読む抽出器。

画像を Google のクラウド API に送らない経路。Cloud Run GPU や社内サーバーの Ollama を OLLAMA_HOST で指す。
出力スキーマ・後処理は Gemini 抽出器と同じ（_to_extraction を共用）なので、ジャッジ以降は無変更で動く。
"""
from __future__ import annotations

import base64
import json
import os
import time

import httpx

from app.schemas import Extraction, OrderForm
from .gemini import PROMPTS, RESPONSE_SCHEMA, _to_extraction


class GemmaLocalExtractor:
    name = "gemma-local"

    def __init__(self, host: str | None = None, model: str | None = None):
        self.host = (host or os.getenv("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.model = model or os.getenv("GEMMA_MODEL", "gemma3:12b")

    def extract(self, image: bytes, *, mime_type="image/png", examples=None, variant=0, hint: OrderForm | None = None) -> Extraction:
        prompt = PROMPTS[variant % len(PROMPTS)]
        if examples:
            prompt += "\n参考（過去の確定値）:\n" + "\n".join(json.dumps(f.model_dump(), ensure_ascii=False) for _, f in examples[:3])
        t0 = time.perf_counter()
        r = httpx.post(f"{self.host}/api/generate", timeout=300, json={
            "model": self.model, "prompt": prompt, "images": [base64.b64encode(image).decode()],
            "format": _ollama_schema(RESPONSE_SCHEMA), "stream": False, "options": {"temperature": 0},
        })
        r.raise_for_status()
        data = json.loads(r.json().get("response") or "{}")
        return _to_extraction(data, model=f"ollama/{self.model}", latency_ms=int((time.perf_counter() - t0) * 1000))


def _ollama_schema(s: dict) -> dict:
    """Gemini 形式（大文字 type）を JSON Schema（小文字 type）へ。"""
    if isinstance(s, dict):
        out = {}
        for k, v in s.items():
            if k == "type" and isinstance(v, str):
                out[k] = v.lower()
            else:
                out[k] = _ollama_schema(v)
        return out
    if isinstance(s, list):
        return [_ollama_schema(x) for x in s]
    return s
