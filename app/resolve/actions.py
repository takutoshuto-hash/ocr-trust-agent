"""行動するエージェントの「行動」= 決定的なツール群。

エージェント（ルール or ADK）はどの行動を取るかを選ぶだけで、行動そのものはここにある関数が実行する。
各行動は候補値 Candidate を返す（None = 候補なし）。候補の採否は resolver.py の決定的ルールが行う。
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image

from app.judge import tools as T
from app.judge.zones import load_zones


@dataclass
class Candidate:
    value: str
    source: str                   # reread_zone | reread_premium | complete_address_from_zip | nearest_product_code
    evidence: str = ""
    independent: bool = True      # 元の抽出とは独立した根拠か（採用条件「独立した2つの読みが一致」に使う）
    detail: str = ""


@dataclass
class FieldContext:
    path: str
    field_type: str
    value: str
    evidence: str
    secondary_value: Optional[str]      # 二重読み取りの値（無ければ None）
    reasons: list[str]                  # ジャッジの理由
    sibling: dict[str, str]             # 同じブロックの他項目（zip/address など）
    image: Optional[bytes]
    format_id: str
    log: list[dict] = field(default_factory=list)
    secondary_source: str = "page"      # 二重読み取りの 2 回目の入力: page（全面）| zones（欄切り出し）
    history_values: list[str] = field(default_factory=list)   # 過去の確定値（同じ送り主／同じ顧客／常連のお届け先）


def crop_zone(image: bytes, format_id: str, path: str, margin: float = 0.004) -> Optional[bytes]:
    """様式ゾーンで欄を切り出した PNG。欄だけを読ませる（精度向上＋クラウドへ送る画素の最小化）。"""
    zones = load_zones(format_id)
    if path not in zones or not image:
        return None
    try:
        img = Image.open(io.BytesIO(image)).convert("RGB")
    except Exception:
        return None
    W, H = img.size
    x1, y1, x2, y2 = zones[path]
    box = (max(0, int((x1 - margin) * W)), max(0, int((y1 - margin) * H)), min(W, int((x2 + margin) * W)), min(H, int((y2 + margin) * H)))
    out = io.BytesIO()
    img.crop(box).save(out, format="PNG")
    return out.getvalue()


def act_reread_zone(ctx: FieldContext, extractor, *, premium: bool = False, hint=None) -> Optional[Candidate]:
    """欄だけを切り出して読み直す。premium=True は高精度モデル（ポリシーで許可されたときだけ）。"""
    crop = crop_zone(ctx.image, ctx.format_id, ctx.path) if ctx.image else None
    if crop is None or not hasattr(extractor, "extract_field"):
        return None
    r = extractor.extract_field(crop, ctx.field_type, premium=premium, hint=hint)
    if r is None:
        return None
    value, evidence = r
    src = "reread_premium" if premium else "reread_zone"
    return Candidate(value=str(value).strip(), source=src, evidence=evidence, independent=True,
                     detail=f"{'高精度モデル' if premium else '欄の切り出し'}で再読み取り → {value!r}")


def act_restore_from_history(ctx: FieldContext) -> Optional[Candidate]:
    """過去の確定値と異体字だけが違うとき（髙田→高田）、確定値の字体に戻す。人が一度直した字体を再利用する決定的な行動。"""
    v = T._squash(ctx.value)
    if not v:
        return None
    for p in ctx.history_values:
        if p != ctx.value and T.fold_variants(T._squash(p)) == T.fold_variants(v) and T._squash(p) != v:
            return Candidate(value=str(p), source="restore_from_history", independent=True,
                             detail=f"過去の確定値の字体に復元 {ctx.value!r} → {p!r}")
    return None


def act_complete_address_from_zip(ctx: FieldContext) -> Optional[Candidate]:
    """郵便番号マスタから都道府県・市区町村を確定し、住所の先頭を補完・訂正する。番地以降は元の値を保つ。"""
    zip_code = ctx.sibling.get("zip", "") if not ctx.field_type.endswith(".zip") else ctx.value
    table = T._zip_table()
    key = (zip_code or "").replace("-", "")
    if key not in table:
        return None
    pref, city, town = table[key]
    addr = ctx.value if ctx.field_type.endswith(".address") else ctx.sibling.get("address", "")
    if not addr:
        return None
    head = pref + city
    if addr.startswith(head):
        return None
    # 住所の側に同じ町域か市区町村が書かれているときだけ補完する（先頭が欠けた・崩れた住所を直す用途）。
    # 無ければ郵便番号の方が読み違いかもしれず、補完すると郵便番号の誤りが住所に伝染し、書き換えた住所に対して
    # 郵便番号が検証合格して自動確定してしまう（台本のリハーサルで実際に起きた）。
    if not any(m and m in addr for m in (town, city)):
        return None
    # 元の住所から「都道府県〜市区町村」に相当する先頭部分を捨て、番地以降を残す
    rest = addr
    for marker in (town, city, pref):
        if marker and marker in rest:
            rest = rest.split(marker, 1)[1]
    rest = rest.lstrip("県府都道市区町村郡 　")
    new = head + (town or "") + rest if not rest.startswith(town or "\0") else head + rest
    return Candidate(value=new, source="complete_address_from_zip", evidence=f"〒{zip_code} → {head}{town}", independent=True,
                     detail=f"郵便番号 {zip_code} から {head} を確定し先頭を補完")


def act_nearest_product_code(ctx: FieldContext) -> Optional[Candidate]:
    """商品マスタとの近似一致（編集距離1）が一意なら候補にする。"""
    table = T._products()
    code = (ctx.value or "").upper()
    if not table or code in table:
        return None
    near = [c for c in table if T._lev(c, code) <= 1]
    if len(near) != 1:
        return None
    return Candidate(value=near[0], source="nearest_product_code", evidence=f"{code} ≈ {near[0]}", independent=False,
                     detail=f"マスタ近似一致（一意）: {code} → {near[0]} ({table[near[0]].get('name', '')})")
