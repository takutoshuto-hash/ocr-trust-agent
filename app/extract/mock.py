"""モック抽出器。

合成帳票の正解（hint）を受け取り、OCR らしい誤りを確率的に混入させて返す。
オフラインの評価・日次シミュレーション・テストで使う。
variant ごとに乱数系列を変えるので、二重読み取りの「不一致」も自然に発生する。
"""
from __future__ import annotations

import hashlib
import random
from typing import Optional

from app.schemas import Extraction, FieldValue, OrderForm

# OCR が混同しやすい文字（誤読パターン）
CONFUSIONS = {
    "1": "7", "7": "1", "0": "6", "6": "0", "3": "8", "8": "3",
    "ー": "一", "一": "ー", "崎": "﨑", "﨑": "崎", "高": "髙", "髙": "高",
    "ロ": "口", "口": "ロ", "力": "カ", "カ": "力", "エ": "工", "工": "エ",
}

# 空欄に対して値を創作する確率（ハルシネーション）と、その内容
# Gemini 2.5 Flash 実測（手書き合成60枚）: 空欄 82 件中 2 件を創作 = 0.024 → 少し安全側に 0.03
HALLUCINATION_RATE = 0.03
HALLUCINATIONS = {
    "applicant.organization": ["株式会社", "有限会社", "商店"],
    "deliveries.noshi_name": ["御中元", "御歳暮", "内祝"],
    "applicant.name_kana": ["ヤマダ タロウ"], "deliveries.name_kana": ["サトウ ハナコ"],
    "deliveries.phone": ["090-0000-0000"], "applicant.phone": ["090-0000-0000"],
}

# 項目種別ごとの誤り率。Gemini 2.5 Flash の実測（手書き合成60枚・1,160項目、全体 96.5%）に合わせて校正
#   applicant: address .100 name .033 kana .050 org .000 phone .100 zip .017
#   deliveries: address .060 name .030 kana .040 noshi .010 phone .040 product .040 qty .000 zip .010
BASE_ERROR_RATE = {
    "applicant.name": 0.035, "applicant.name_kana": 0.05, "applicant.zip": 0.02,
    "applicant.address": 0.10, "applicant.phone": 0.09, "applicant.organization": 0.01,
    "deliveries.name": 0.03, "deliveries.name_kana": 0.04, "deliveries.zip": 0.01,
    "deliveries.address": 0.065, "deliveries.phone": 0.04, "deliveries.product_code": 0.04,
    "deliveries.qty": 0.005, "deliveries.noshi_name": 0.01,
}


def _perturb(value: str, rng: random.Random) -> str:
    if not value:
        return value
    chars = list(value)
    idxs = [i for i, c in enumerate(chars)]
    i = rng.choice(idxs)
    c = chars[i]
    roll = rng.random()
    if c in CONFUSIONS and roll < 0.6:
        chars[i] = CONFUSIONS[c]
    elif roll < 0.8:
        del chars[i]                           # 脱字
    else:
        chars.insert(i, rng.choice("一二三ノ丶")) # ノイズ混入
    return "".join(chars)


class MockExtractor:
    name = "mock"

    def __init__(self, error_scale: float = 1.0):
        self.error_scale = error_scale
        # 事故注入（デモ・テスト用）: 項目種別ごとの系統的な読み違い。ルールや few-shot では消えない
        # （FAX の劣化で「7」の横棒がかすれて「1」に見える、など物理的な原因を模倣する）
        # [{"field_type": "deliveries.zip", "from": "7", "to": "1", "rate": 1.0}]
        self.defects: list[dict] = []

    def set_defect(self, field_type: str, frm: str, to: str, rate: float = 1.0) -> None:
        self.defects = [d for d in self.defects if d["field_type"] != field_type]
        self.defects.append({"field_type": field_type, "from": frm, "to": to, "rate": float(rate)})

    def clear_defects(self) -> None:
        self.defects = []

    def _apply_defects(self, field_type: str, truth: str, value: str, rng: random.Random) -> str:
        """真値に from が含まれていれば、確率 rate で from→to に置き換える（1回目・2回目・欄の再読み取りすべてに同じ癖）。"""
        for d in self.defects:
            if d["field_type"] == field_type and d["from"] in truth and rng.random() < d["rate"]:
                value = value.replace(d["from"], d["to"])
        return value

    def extract_field(self, crop: bytes, field_type: str, *, premium: bool = False, hint=None):
        """欄だけの再読み取りを模倣: 誤り率は通常の 1/2（高精度モデルなら 1/5）。hint は正解値。"""
        if hint is None:
            return None
        seed = int(hashlib.sha256(crop[:2048] + (b"p" if premium else b"z")).hexdigest(), 16) % (2**32)
        rng = random.Random(seed)
        p = BASE_ERROR_RATE.get(field_type, 0.05) * self.error_scale * (0.2 if premium else 0.5)
        v = str(hint)
        nv = v if rng.random() >= p else _perturb(v, rng)
        nv = self._apply_defects(field_type, v, nv, random.Random(seed ^ 0x5EED))
        return nv, nv

    def extract(self, image: bytes, *, mime_type="image/png", examples=None, variant=0, hint: Optional[OrderForm] = None, rules=None, format_id=None) -> Extraction:
        if hint is None:
            raise ValueError("MockExtractor には hint（正解 OrderForm）が必要です")
        # 画像とvariantから決定的な乱数系列を作る（再現性のため）
        seed = int(hashlib.sha256(image[:4096] + bytes([variant])).hexdigest(), 16) % (2**32)
        rng = random.Random(seed)
        # few-shot / 承認済みルールが渡されたら誤り率を下げる（学習・振り返りの効果のモデル化）
        scale = self.error_scale * (0.6 if examples else 1.0) * (0.9 ** min(len(rules or []), 5))

        from app.schemas import field_type_of
        flat = hint.flatten()
        fields: dict[str, FieldValue] = {}
        out: dict = {}
        for path, v in flat.items():
            ft = field_type_of(path)
            p = BASE_ERROR_RATE.get(ft, 0.1) * scale
            if isinstance(v, str) and not v:
                # 空欄: 確率 HALLUCINATION_RATE で「もっともらしい値」を創作する（VLM のハルシネーションを模倣）
                nv = rng.choice(HALLUCINATIONS.get(ft, ["不明"])) if rng.random() < HALLUCINATION_RATE * scale else ""
                out[path] = nv
                fields[path] = FieldValue(path=path, value=nv, confidence=round(rng.uniform(0.6, 0.95), 3), evidence=nv)
                continue
            if isinstance(v, int):
                nv = v if rng.random() >= p else max(1, v + rng.choice([-1, 1, 9]))
                conf = 0.95 if nv == v else rng.uniform(0.5, 0.9)
            else:
                nv = v if rng.random() >= p else _perturb(str(v), rng)
                if self.defects:
                    # 画像ごとに決まる乱数（variant に依らない）: 同じ紙の同じ欄なら 1 回目も 2 回目も同じ読み違いになる
                    drng = random.Random(int(hashlib.sha256(image[:4096] + path.encode()).hexdigest(), 16) % (2**32))
                    nv = self._apply_defects(ft, str(v), nv, drng)
                conf = rng.uniform(0.85, 0.99) if nv == v else rng.uniform(0.4, 0.9)
            out[path] = nv
            fields[path] = FieldValue(path=path, value=nv, confidence=round(conf, 3), evidence=str(nv))
        return Extraction(form=OrderForm.from_flat(out), fields=fields, raw_text="(mock)", model="mock", latency_ms=1)
