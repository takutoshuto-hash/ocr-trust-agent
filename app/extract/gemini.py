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
from app.extract.usage import GLOBAL as USAGE

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

ZONE_PROMPT = (
    "これは日本の食品ギフトの手書き注文書（FAX）を、様式の欄ごとに切り出した画像の一覧です。"
    "各画像の直前に【ブロック / 項目】のラベルがあります。ラベルの項目に対応する手書き文字だけを読み、"
    "指定のJSONスキーマで返してください。\n"
    "- 各項目は value（正規化した値）、confidence（0〜1）、evidence（読んだ文字列そのもの）を返す\n"
    "- 画像が空欄・判読不能なら value を空文字にし、推測で創作しない。他の欄の内容から補わない\n"
    "- zip は NNN-NNNN、phone はハイフン区切り、qty は整数（無ければ 1）、name_kana はカタカナ、product_code は英数字をそのまま\n"
    "- 氏名・会社名・住所・のし名入れの漢字は書かれたとおりに保ち、異体字（髙・﨑・邊・齋 など）を常用漢字に置き換えない\n"
    "- お届け先ブロックは、氏名・郵便番号・住所がすべて空欄なら配列に含めない\n"
    "- raw_text は空文字でよい"
)
ZONE_LABELS = {"zip": "郵便番号", "address": "住所", "name_kana": "フリガナ", "name": "氏名", "phone": "電話番号",
               "organization": "会社名", "product_code": "商品番号", "qty": "数量", "noshi_name": "のし名入れ"}


def zone_parts(image: bytes, format_id: str, *, min_height: int = 128) -> list:
    """様式ゾーンごとの切り出し画像を、ラベル付きの Part 列にする（二重読み取りの 2 回目 = 独立した入力）。

    1 回目の全面読みと同じモデルでも、入力（切り出し・拡大・文脈なし）が違えば誤りの相関が切れ、
    「二重読み取り一致」が初めて独立した根拠になる。小さい欄は高さ min_height まで拡大する。"""
    import io
    from PIL import Image
    from app.judge.zones import load_zones
    zones = load_zones(format_id)
    if not zones or not image:
        return []
    try:
        img = Image.open(io.BytesIO(image)).convert("RGB")
    except Exception:
        return []
    W, H = img.size
    parts: list = []
    for path, (x1, y1, x2, y2) in zones.items():
        block, key = path.rsplit(".", 1)
        blabel = "ご依頼主" if block == "applicant" else f"お届け先{int(block[block.index('[') + 1:-1]) + 1}"
        box = (max(0, int((x1 - 0.004) * W)), max(0, int((y1 - 0.004) * H)), min(W, int((x2 + 0.004) * W)), min(H, int((y2 + 0.004) * H)))
        crop = _trim_right(img.crop(box))
        if crop.height < min_height:
            k = min_height / crop.height
            crop = crop.resize((int(crop.width * k), min_height), Image.LANCZOS)
        buf = io.BytesIO(); crop.save(buf, format="PNG")
        parts.append(f"【{blabel} / {ZONE_LABELS.get(key, key)}（{path}）】")
        parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))
    return parts


def _trim_right(crop, *, dark: int = 170, min_frac: float = 0.25, margin: int = 60, min_col_ink: int = 3):
    """欄の右側の空白を詰める。横長のまま送るとモデル側で縮小されて細い数字が欠けるため、
    インクのある右端（＋余白）までに切る。枠線（長い横の走り）を消し、上下 8px を除いた内側で数え、
    FAX の点ノイズは列あたり min_col_ink 画素未満なら無視する。"""
    import numpy as np
    from app.judge.zones import _remove_lines
    a = np.asarray(crop.convert("L"), dtype=np.uint8) < dark
    a = _remove_lines(a, max_run=60, axis=1)                          # 横の枠線
    a = _remove_lines(a, max_run=max(12, int(a.shape[0] * 0.6)), axis=0)   # 縦の枠線・FAX の縦筋
    inner = a[8:-8, :-12] if a.shape[0] > 24 else a[:, :-12]     # 右端 12px は枠線（傾いた帳票では走りが短く残る）
    cols = np.where(inner.sum(axis=0) >= min_col_ink)[0]
    right = int(cols.max()) + margin if len(cols) else 0
    right = max(right, int(crop.width * min_frac))
    return crop.crop((0, 0, min(crop.width, right), crop.height))


class GeminiExtractor:
    name = "gemini"

    def __init__(self, api_key: str = "", model: str = "gemini-2.5-flash", premium_model: str = "gemini-2.5-pro",
                 second_read: str = "zones", second_model: str = "", thinking_budget: Optional[int] = None):
        import os
        from app.config import make_genai_client
        self.client = make_genai_client()   # Vertex AI（GOOGLE_GENAI_USE_VERTEXAI）または AI Studio キー
        self.model = model
        self.premium_model = premium_model
        # 思考トークンの上限（0 = 思考なし）。転記作業では思考が精度に効かない一方、費用の 4 割を占める（実測）
        tb = os.getenv("GEMINI_THINKING_BUDGET")
        self.thinking_budget = thinking_budget if thinking_budget is not None else (int(tb) if tb not in (None, "") else None)
        # 二重読み取りの 2 回目: "zones"=欄ごとの切り出し画像（独立した入力）/ "page"=全面画像を別プロンプトで
        self.second_read = second_read
        self.second_model = second_model or model

    def _generate(self, **kwargs):
        """generate_content を混雑（429）・一時障害（500/503）のときだけ指数バックオフで再試行する。
        待ち時間 2,4,8,16,30 秒（GEMINI_MAX_RETRIES 回、既定 5）。それでも駄目なら例外をそのまま上げる（呼び出し側が退避する）。"""
        import os
        import time as _t
        from google.genai import errors
        tries = int(os.getenv("GEMINI_MAX_RETRIES", "5"))
        delay = 2.0
        for i in range(tries + 1):
            try:
                return self.client.models.generate_content(**kwargs)
            except errors.APIError as e:
                code = getattr(e, "code", None) or getattr(e, "status_code", None)
                if code not in (429, 500, 502, 503, 504) or i >= tries:
                    raise
                _t.sleep(delay)
                delay = min(delay * 2, 30.0)

    def extract_field(self, crop: bytes, field_type: str, *, premium: bool = False, hint=None):
        """欄の切り出し画像を1項目だけ読む（行動するエージェントの再読み取り）。戻り値 (value, evidence) or None。"""
        key = field_type.rsplit(".", 1)[-1]
        prompt = FIELD_PROMPT.get(key, "この画像は注文書の1つの欄だけを切り出したものです。手書きの記入内容を読んでください。")
        prompt += (" 空欄なら value を空文字にし、推測で創作しないこと。evidence には読んだ文字列そのものを入れること。"
                   "漢字は書かれたとおりに保ち、異体字（髙・﨑・邊・齋 など）を常用漢字に置き換えないこと。")
        resp = self._generate(
            model=self.premium_model if premium else self.model,
            contents=[types.Part.from_bytes(data=crop, mime_type="image/png"), prompt],
            config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json", response_schema=FIELD_SCHEMA,
                                               **self._thinking(premium)),
        )
        USAGE.add(self.premium_model if premium else self.model, getattr(resp, "usage_metadata", None))
        data = json.loads(resp.text or "{}")
        return str(data.get("value", "") or "").strip(), str(data.get("evidence", "") or "")

    def _thinking(self, premium: bool) -> dict:
        """思考トークンの設定（Flash 系のみ。Pro は思考を止められないので触らない）。"""
        if self.thinking_budget is None or premium:
            return {}
        return {"thinking_config": types.ThinkingConfig(thinking_budget=int(self.thinking_budget))}

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

    def extract(self, image: bytes, *, mime_type="image/png", examples=None, variant=0, hint=None, rules=None,
                format_id: Optional[str] = None) -> Extraction:
        model = self.model
        parts = zone_parts(image, format_id) if (variant == 1 and self.second_read == "zones" and format_id) else []
        if parts:
            model = self.second_model
            contents = [ZONE_PROMPT + self._rules(rules) + self._few_shot(examples), *parts]
        else:
            contents = [types.Part.from_bytes(data=image, mime_type=mime_type),
                        PROMPTS[variant % len(PROMPTS)] + self._rules(rules) + self._few_shot(examples)]
        t0 = time.perf_counter()
        resp = self._generate(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
                **self._thinking(False),
            ),
        )
        latency = int((time.perf_counter() - t0) * 1000)
        USAGE.add(model, getattr(resp, "usage_metadata", None))
        data = json.loads(resp.text or "{}")
        return _to_extraction(data, model=model + (":zones" if parts else ""), latency_ms=latency)


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
            if path.endswith(".product_code"):
                v = v.upper().replace("_", "-").replace("－", "-").replace("ー", "-")   # 商品番号はハイフン表記に正規化
        flat[path] = v
        fields[path] = FieldValue(
            path=path, value=v,
            confidence=float((cell or {}).get("confidence", 0) or 0),
            evidence=str((cell or {}).get("evidence", "") or ""),
        )

    ap = data.get("applicant", {}) or {}
    for k in ["name", "name_kana", "zip", "address", "phone", "organization"]:
        take(f"applicant.{k}", ap.get(k))
    # 欄ごとの読みでは空のお届け先ブロックが配列に残ることがあるので落とす（氏名・郵便番号・住所・商品番号がすべて空）
    deliveries = [d for d in (data.get("deliveries", []) or [])
                  if any(str(((d or {}).get(k) or {}).get("value", "") or "").strip() for k in ("name", "zip", "address", "product_code"))]
    for i, d in enumerate(deliveries):
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
