"""ジャッジ本体: 項目ごとにツールで検証し、二重読み取りの一致を付与して FieldVerdict を返す。

LLM を使わない決定的な部分。ADK エージェント（agent.py）はこの結果を人向けの説明に変換する役。
検証の種類:
  - 形式・外部照合（郵便番号↔住所（町域まで）、商品マスタ、電話桁、カナ、数量、必須）
  - 項目間の相互検証（電話の市外局番↔郵便番号の都道府県、氏名の姓↔フリガナの読み）
  - 送り主の履歴照合（過去に確定した依頼主・常連のお届け先）
  - ハルシネーション対策: 空欄検知（欄のインク量 vs 値）、根拠整合（value vs evidence）
  - 二重読み取りの一致
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Optional

import yaml

from app.config import settings
from app.schemas import CheckStatus, Extraction, FieldVerdict, field_type_of
from . import tools as T
from .zones import ink_ratio, load_zones, open_image


class CheckBindings:
    """data/master/checks.yaml の中身。項目種別のパターン（*.zip など）→ [(検証名, 引数の欄名 or None)]。"""

    def __init__(self, raw: dict):
        self.rules: list[tuple[str, list[tuple[str, Optional[list[str]]]]]] = []
        for pattern, specs in (raw.get("checks") or {}).items():
            items: list[tuple[str, Optional[list[str]]]] = []
            for spec in specs or []:
                if isinstance(spec, str):
                    items.append((spec, None))
                elif isinstance(spec, dict) and len(spec) == 1:
                    (name, args), = spec.items()
                    items.append((str(name), [str(a) for a in (args or [])]))
            self.rules.append((str(pattern), items))
        self.history = [str(p) for p in raw.get("history") or []]
        self.variant_kanji = [str(p) for p in raw.get("variant_kanji") or []]

    def checks_for(self, field_type: str) -> list[tuple[str, Optional[list[str]]]]:
        for pattern, items in self.rules:
            if fnmatch.fnmatch(field_type, pattern):
                return items
        return []

    def wants_history(self, field_type: str) -> bool:
        return any(fnmatch.fnmatch(field_type, p) for p in self.history)

    def wants_variant_kanji(self, field_type: str) -> bool:
        return any(fnmatch.fnmatch(field_type, p) for p in self.variant_kanji)

    def field_types(self) -> list[str]:
        return [p for p, _ in self.rules]


def load_check_bindings(path: Path) -> CheckBindings:
    if not Path(path).exists():
        raise FileNotFoundError(f"検証の宣言ファイルがありません: {path}")
    return CheckBindings(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})


class Judge:
    def __init__(self, blank_ink_ratio: float = 0.013, evidence_max_distance: float = 0.5, checks_path: Optional[Path] = None):
        self.blank_ink_ratio = blank_ink_ratio
        self.evidence_max_distance = evidence_max_distance
        # 検証の組み合わせは宣言ファイル（業種依存の 3 ファイルのひとつ）。項目種別のパターン → 検証の並び
        self.bindings = load_check_bindings(checks_path or settings.master_dir / "checks.yaml")

    def judge(self, primary: Extraction, secondary: Optional[Extraction] = None, *,
              image: Optional[bytes] = None, format_id: Optional[str] = None,
              history: Optional[list] = None) -> dict[str, FieldVerdict]:
        """history: 同じ送り主の過去の確定 OrderForm（新しい順）。依頼主項目とお届け先項目の履歴照合に使う。"""
        flat = primary.form.flatten()
        flat2 = secondary.form.flatten() if secondary else None
        zones = load_zones(format_id) if format_id else {}
        img = open_image(image) if (image and zones) else None
        past = _history_index(history or [])
        verdicts: dict[str, FieldVerdict] = {}

        for path, value in flat.items():
            ft = field_type_of(path)
            v = FieldVerdict(path=path, field_type=ft)
            v.checks = self._checks_for(ft, path, value, flat)
            if history and not isinstance(value, int) and self.bindings.wants_history(ft):
                v.checks.append(T.check_history(value, _past_values(past, path, ft, flat)))
            # ハルシネーション対策（空欄検知）
            #   - 整数項目（数量）は「未記入なら 1」がプロンプト上の既定値なので対象外
            #   - 1 文字の値は薄い線 1 本と FAX ノイズを画素で区別できないため対象外（2 文字以上のみ）
            if img is not None and path in zones and not isinstance(value, int) and len(str(value).strip()) != 1:
                v.checks.append(T.check_blank_zone(ink_ratio(img, zones[path]), str(value), self.blank_ink_ratio))
            fv = primary.fields.get(path)
            if fv is not None and fv.evidence and primary.model != "mock" and not isinstance(value, int):
                v.checks.append(T.check_evidence(str(value), fv.evidence, self.evidence_max_distance))
                if self.bindings.wants_variant_kanji(ft):
                    v.checks.append(T.check_variant_kanji(str(value), fv.evidence))   # 復元済みなら PASS、残っていれば FAIL
            if flat2 is not None:
                v.agreement = _norm(flat2.get(path)) == _norm(value)
            v.reason = self._explain(v)
            verdicts[path] = v
        return verdicts

    def detect_missing_blocks(self, primary: Extraction, *, image: Optional[bytes], format_id: Optional[str]) -> list[str]:
        """抽出結果に無いお届け先ブロックの欄にインクがあれば「読み落とし疑い」を返す（帳票全体の警告）。"""
        if not image or not format_id:
            return []
        zones = load_zones(format_id)
        img = open_image(image)
        if img is None or not zones:
            return []
        n_extracted = len(primary.form.deliveries)
        flags: list[str] = []
        b = n_extracted
        while f"deliveries[{b}].name" in zones:
            inked = [k for k in ("name", "zip", "address") if f"deliveries[{b}].{k}" in zones
                     and ink_ratio(img, zones[f"deliveries[{b}].{k}"]) >= self.blank_ink_ratio]
            if len(inked) >= 2:
                flags.append(f"お届け先 {b + 1} が読み取られていません（欄に記入あり: {', '.join(inked)}）。原本を確認して追加してください")
            b += 1
        return flags

    # --- 項目種別ごとの検証セット ---
    def _checks_for(self, ft: str, path: str, value, flat: dict) -> list:
        """宣言ファイルの並びどおりに検証を掛ける。相互検証の引数は同じブロック（依頼主／お届け先 n）の欄から取る。"""
        prefix = path.rsplit(".", 1)[0]
        out = []
        for name, args in self.bindings.checks_for(ft):
            fn = T.CHECKS.get(name)
            if fn is None:
                continue
            if args is None:
                out.append(fn(value))
            else:
                out.append(fn(*[value if a == ft.rsplit(".", 1)[-1] else flat.get(f"{prefix}.{a}", "") for a in args]))
        return out

    def _explain(self, v: FieldVerdict) -> str:
        """人向けの一文（確認画面の赤字）。検証の名前や内部値は出さない。検証の詳細は checks に残る。"""
        from app.labels import plain_verdict
        return plain_verdict(v)


def _history_index(history: list) -> dict:
    """過去の確定 OrderForm から、依頼主項目の値リストと、お届け先（氏名キー）ごとの値を索引にする。"""
    idx = {"applicant": {}, "recipients": {}}
    for form in history:
        for k, v in form.applicant.model_dump().items():
            idx["applicant"].setdefault(k, []).append(v)
        for d in form.deliveries:
            key = _norm(d.name)
            if key:
                idx["recipients"].setdefault(key, []).append(d.model_dump())
    return idx


def _past_values(past: dict, path: str, ft: str, flat: dict) -> list:
    field = ft.rsplit(".", 1)[-1]
    if ft.startswith("applicant"):
        return [x for x in past["applicant"].get(field, []) if x]
    # お届け先: 同じ氏名の過去のお届け先があれば、その郵便番号・住所・電話などと照合
    prefix = path.rsplit(".", 1)[0]
    name_key = _norm(flat.get(f"{prefix}.name", ""))
    recs = past["recipients"].get(name_key, []) if name_key else []
    return [r.get(field) for r in recs if r.get(field)]


def _norm(x) -> str:
    return str(x if x is not None else "").replace(" ", "").replace("　", "").replace("-", "").replace("_", "").upper()
