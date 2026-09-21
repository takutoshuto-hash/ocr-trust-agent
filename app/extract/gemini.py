"""Gemini による構造化抽出。

- responseSchema で厳密 JSON（OrderForm と同型）
- 項目ごとの confidence / evidence も返させる（判定には直接使わず特徴量にする）
- examples（過去の確定例）を few-shot としてプロンプトに注入
- variant=1 は言い回しを変えた別プロンプト（二重読み取り用）
"""
from __future__ import annotations

import json
import time
from typing import Optional

from google import genai
from google.genai import types

from app.schemas import Extraction, FieldValue, OrderForm

_FIELD_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "value": {"type": "STRING"},
        "confidence": {"type": "NUMBER", "description": "0-1"},
        "evidence": {"type": "STRING", "description": "画像で読んだ文字列そのもの"},
    },
    "required": ["value", "confidence", "evidence"],
}

_DELIVERY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        k: _FIELD_SCHEMA
        for k in ["name", "name_kana", "zip", "address", "phone", "product_code", "qty", "noshi_name"]
    },
    "required": ["name", "name_kana", "zip", "address", "phone", "product_code", "qty", "noshi_name"],
}

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "applicant": {
            "type": "OBJECT",
            "properties": {
                k: _FIELD_SCHEMA
                for k in ["name", "name_kana", "zip", "address", "phone", "organization"]
            },
            "required": ["name", "name_kana", "zip", "address", "phone", "organization"],
        },
        "deliveries": {"type": "ARRAY", "items": _DELIVERY_SCHEMA},
        "raw_text": {"type": "STRING"},
    },
    "required": ["applicant", "deliveries", "raw_text"],
}

PROMPTS = [
    (
        "これは日本の食品ギフトの手書き注文書（FAX）の画像です。ご依頼主とお届け先（最大3件）を読み取り、"
        "指定のJSONスキーマで返してください。\n"
        "- 各項目は value（正規化した値）、confidence（0〜1）、evidence（読んだ文字列そのもの）を返す\n"
        "- 読めない項目は value を空文字にし、推測で創作しない\n"
        "- zip は NNN-NNNN、phone はハイフン区切り、qty は整数（無ければ 1）\n"
        "- name_kana はカタカナ。product_code は商品番号欄の英数字をそのまま\n"
        "- 空欄のお届け先は配列に含めない\n"
        "- raw_text に画像全体の文字起こしを入れる"
    ),
    (
        "You are a careful transcriber of Japanese handwritten order forms (FAX). "
        "Transcribe the applicant and up to 3 delivery destinations into the given JSON schema. "
        "For each field return value (normalized), confidence (0-1), and evidence (exact characters as seen). "
        "Never invent values; leave unreadable fields empty. zip=NNN-NNNN, phone with hyphens, qty integer (default 1), "
        "name_kana in katakana, product_code as written. Omit empty delivery blocks. Put the full transcription in raw_text."
    ),
]


class GeminiExtractor:
    name = "gemini"

    def __init__(self, api_key: str, model: str):
        self.client = genai.Client(api_key=api_key)
        self.model = model

    def _few_shot(self, examples: Optional[list[tuple[str, OrderForm]]]) -> str:
        if not examples:
            return ""
        lines = ["\n【参考: この送り主／様式で過去に人が確定した値（筆跡の癖の参考にする。丸写しはしない）】"]
        for desc, form in examples[:5]:
            lines.append(f"- {desc}: {json.dumps(form.model_dump(), ensure_ascii=False)}")
        return "\n".join(lines)

    def extract(self, image: bytes, *, mime_type="image/png", examples=None, variant=0, hint=None) -> Extraction:
        prompt = PROMPTS[variant % len(PROMPTS)] + self._few_shot(examples)
        t0 = time.perf_counter()
        resp = self.client.models.generate_content(
            model=self.model,
            contents=[
                types.Part.from_bytes(data=image, mime_type=mime_type),
                prompt,
            ],
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
            ),
        )
        latency = int((time.perf_counter() - t0) * 1000)
        data = json.loads(resp.text or "{}")
        return _to_extraction(data, model=self.model, latency_ms=latency)


def _to_extraction(data: dict, *, model: str, latency_ms: int) -> Extraction:
    fields: dict[str, FieldValue] = {}
    flat: dict = {}

    def take(path: str, cell: dict, as_int: bool = False):
        v = (cell or {}).get("value", "")
        if as_int:
            try:
                v = int(str(v).strip() or 1)
            except ValueError:
                v = 1
        else:
            v = str(v or "").strip()
        flat[path] = v
        fields[path] = FieldValue(
            path=path, value=v,
            confidence=float((cell or {}).get("confidence", 0) or 0),
            evidence=str((cell or {}).get("evidence", "") or ""),
        )

    ap = data.get("applicant", {}) or {}
    for k in ["name", "name_kana", "zip", "address", "phone", "organization"]:
        take(f"applicant.{k}", ap.get(k))
    for i, d in enumerate(data.get("deliveries", []) or []):
        for k in ["name", "name_kana", "zip", "address", "phone", "product_code", "noshi_name"]:
            take(f"deliveries[{i}].{k}", d.get(k))
        take(f"deliveries[{i}].qty", d.get("qty"), as_int=True)

    return Extraction(
        form=OrderForm.from_flat(flat),
        fields=fields,
        raw_text=str(data.get("raw_text", "") or ""),
        model=model,
        latency_ms=latency_ms,
    )
