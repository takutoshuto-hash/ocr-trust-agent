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
        "- 氏名・会社名・住所・のし名入れの漢字は書かれたとおりに保つ。異体字（髙・﨑・邊・邉・齋・齊・德・栁・濵・瀨 など）を"
        "常用漢字（高・崎・辺・斎・斉・徳・柳・浜・瀬）に置き換えない。宛名に使う文字なので字体そのものが情報である\n"
        "- 空欄のお届け先は配列に含めない\n"
        "- raw_text に画像全体の文字起こしを入れる"
    ),
    (
        "You are a careful transcriber of Japanese handwritten order forms (FAX). "
        "Transcribe the applicant and up to 3 delivery destinations into the given JSON schema. "
        "For each field return value (normalized), confidence (0-1), and evidence (exact characters as seen). "
        "Never invent values; leave unreadable fields empty. zip=NNN-NNNN, phone with hyphens, qty integer (default 1), "
        "name_kana in katakana, product_code as written. Keep kanji exactly as written for names, company names, addresses and "
        "noshi names: never normalize variant characters (髙→高, 﨑→崎, 邊/邉→辺, 齋→斎, 齊→斉, 德→徳, 栁→柳, 濵→浜, 瀨→瀬) — the glyph itself "
        "is information on a shipping label. Omit empty delivery blocks. Put the full transcription in raw_text."
    ),
]


FIELD_PROMPT = {
    "zip": "この画像は注文書の郵便番号欄だけを切り出したものです。手書きの郵便番号を NNN-NNNN 形式で読んでください。",
    "address": "この画像は注文書の住所欄だけを切り出したものです。手書きの住所を都道府県から番地・建物名まで読んでください。",
    "name": "この画像は注文書の氏名欄だけを切り出したものです。手書きの氏名（漢字）を読んでください。姓と名の間は半角スペース。",
    "name_kana": "この画像は注文書のフリガナ欄だけを切り出したものです。カタカナで読んでください。姓と名の間は半角スペース。",
    "phone": "この画像は注文書の電話番号欄だけを切り出したものです。ハイフン区切りで読んでください。",
    "organization": "この画像は注文書の会社名欄だけを切り出したものです。会社名・団体名を読んでください。",
    "product_code": "この画像は注文書の商品番号欄だけを切り出したものです。英数字とハイフンの商品コードをそのまま読んでください。",
    "noshi_name": "この画像は注文書の「のし名入れ」欄だけを切り出したものです。名入れの文字を読んでください。",
}
FIELD_SCHEMA = {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "evidence": {"type": "STRING"}}, "required": ["value", "evidence"]}


class GeminiExtractor:
    name = "gemini"

    def __init__(self, api_key: str = "", model: str = "gemini-2.5-flash", premium_model: str = "gemini-2.5-pro"):
        from app.config import make_genai_client
        self.client = make_genai_client()   # Vertex AI（GOOGLE_GENAI_USE_VERTEXAI）または AI Studio キー
        self.model = model
        self.premium_model = premium_model

    def extract_field(self, crop: bytes, field_type: str, *, premium: bool = False, hint=None):
        """欄の切り出し画像を1項目だけ読む（行動するエージェントの再読み取り）。戻り値 (value, evidence) or None。"""
        key = field_type.rsplit(".", 1)[-1]
        prompt = FIELD_PROMPT.get(key, "この画像は注文書の1つの欄だけを切り出したものです。手書きの記入内容を読んでください。")
        prompt += (" 空欄なら value を空文字にし、推測で創作しないこと。evidence には読んだ文字列そのものを入れること。"
                   "漢字は書かれたとおりに保ち、異体字（髙・﨑・邊・齋 など）を常用漢字に置き換えないこと。")
        resp = self.client.models.generate_content(
            model=self.premium_model if premium else self.model,
            contents=[types.Part.from_bytes(data=crop, mime_type="image/png"), prompt],
            config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json", response_schema=FIELD_SCHEMA),
        )
        data = json.loads(resp.text or "{}")
        return str(data.get("value", "") or "").strip(), str(data.get("evidence", "") or "")

    def _few_shot(self, examples: Optional[list[tuple[str, OrderForm]]]) -> str:
        if not examples:
            return ""
        lines = ["\n【参考: この送り主／様式で過去に人が確定した値（筆跡の癖の参考にする。丸写しはしない）】"]
        for desc, form in examples[:5]:
            lines.append(f"- {desc}: {json.dumps(form.model_dump(), ensure_ascii=False)}")
        return "\n".join(lines)

    @staticmethod
    def _rules(rules: Optional[list[str]]) -> str:
        if not rules:
            return ""
        return "\n【運用で確認された読み取りルール（振り返りで人が承認したもの）】\n" + "\n".join(f"- {r}" for r in rules[:10])

    def extract(self, image: bytes, *, mime_type="image/png", examples=None, variant=0, hint=None, rules=None) -> Extraction:
        prompt = PROMPTS[variant % len(PROMPTS)] + self._rules(rules) + self._few_shot(examples)
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

    # 異体字の復元: モデルが value を常用漢字に寄せても evidence（読んだ文字列そのもの）に異体字があれば value を戻す
    from app.judge.tools import restore_variant_kanji
    for path, fv in fields.items():
        if isinstance(fv.value, str) and fv.evidence and path.rsplit(".", 1)[-1] in ("name", "organization", "address", "noshi_name"):
            new, restored = restore_variant_kanji(fv.value, fv.evidence)
            if restored:
                fv.value = new
                flat[path] = new

    return Extraction(
        form=OrderForm.from_flat(flat),
        fields=fields,
        raw_text=str(data.get("raw_text", "") or ""),
        model=model,
        latency_ms=latency_ms,
    )
