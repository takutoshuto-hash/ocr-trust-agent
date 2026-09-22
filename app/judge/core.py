"""ジャッジ本体: 項目ごとにツールで検証し、二重読み取りの一致を付与して FieldVerdict を返す。

LLM を使わない決定的な部分。ADK エージェント（agent.py）はこの結果を人向けの説明に変換する役。
検証の種類:
  - 形式・外部照合（郵便番号↔住所、商品マスタ、電話桁、カナ、数量、必須）
  - ハルシネーション対策: 空欄検知（欄のインク量 vs 値）、根拠整合（value vs evidence）
  - 二重読み取りの一致
"""
from __future__ import annotations

from typing import Optional

from app.schemas import CheckStatus, Extraction, FieldVerdict, field_type_of
from . import tools as T
from .zones import ink_ratio, load_zones, open_image


class Judge:
    def __init__(self, blank_ink_ratio: float = 0.013, evidence_max_distance: float = 0.5):
        self.blank_ink_ratio = blank_ink_ratio
        self.evidence_max_distance = evidence_max_distance

    def judge(self, primary: Extraction, secondary: Optional[Extraction] = None, *,
              image: Optional[bytes] = None, format_id: Optional[str] = None) -> dict[str, FieldVerdict]:
        flat = primary.form.flatten()
        flat2 = secondary.form.flatten() if secondary else None
        zones = load_zones(format_id) if format_id else {}
        img = open_image(image) if (image and zones) else None
        verdicts: dict[str, FieldVerdict] = {}

        for path, value in flat.items():
            ft = field_type_of(path)
            v = FieldVerdict(path=path, field_type=ft)
            v.checks = self._checks_for(ft, path, value, flat)
            # ハルシネーション対策（空欄検知）
            #   - 整数項目（数量）は「未記入なら 1」がプロンプト上の既定値なので対象外
            #   - 1 文字の値は薄い線 1 本と FAX ノイズを画素で区別できないため対象外（2 文字以上のみ）
            if img is not None and path in zones and not isinstance(value, int) and len(str(value).strip()) != 1:
                v.checks.append(T.check_blank_zone(ink_ratio(img, zones[path]), str(value), self.blank_ink_ratio))
            fv = primary.fields.get(path)
            if fv is not None and fv.evidence and primary.model != "mock" and not isinstance(value, int):
                v.checks.append(T.check_evidence(str(value), fv.evidence, self.evidence_max_distance))
            if flat2 is not None:
                v.agreement = _norm(flat2.get(path)) == _norm(value)
            v.reason = self._explain(v)
            verdicts[path] = v
        return verdicts

    # --- 項目種別ごとの検証セット ---
    def _checks_for(self, ft: str, path: str, value, flat: dict) -> list:
        prefix = path.rsplit(".", 1)[0]
        if ft.endswith(".zip"):
            return [T.check_zip_format(value), T.check_zip_address(value, flat.get(f"{prefix}.address", ""))]
        if ft.endswith(".address"):
            return [T.check_nonempty(value), T.check_zip_address(flat.get(f"{prefix}.zip", ""), value)]
        if ft.endswith(".phone"):
            return [T.check_phone_format(value)]
        if ft.endswith(".name_kana"):
            return [T.check_kana(value)]
        if ft.endswith(".product_code"):
            return [T.check_product_code(value)]
        if ft.endswith(".qty"):
            return [T.check_qty(value)]
        if ft.endswith(".name"):
            return [T.check_nonempty(value)]
        return []   # organization / noshi_name: 任意項目

    def _explain(self, v: FieldVerdict) -> str:
        parts = []
        for c in v.checks:
            if c.status == CheckStatus.FAIL:
                parts.append(f"{c.name}: {c.detail}")
            elif c.status == CheckStatus.UNKNOWN and c.name == "blank_zone" and "読み落とし" in c.detail:
                parts.append(f"{c.name}: {c.detail}")
            elif c.status == CheckStatus.UNKNOWN:
                parts.append(f"{c.name}: 判定不能（{c.detail}）")
        if v.agreement is False:
            parts.append("二重読み取りが不一致")
        if not parts:
            return "検証すべて合格" + ("・二重読み取り一致" if v.agreement else "")
        return "; ".join(parts)


def _norm(x) -> str:
    return str(x if x is not None else "").replace(" ", "").replace("　", "").replace("-", "").upper()
