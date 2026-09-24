"""現場の人が読む画面のための言葉。項目種別コード・ポリシーキー・内部用語を、専門用語を使わない日本語に置き換える。"""
from __future__ import annotations

import re

FIELD_NAMES = {"zip": "郵便番号", "address": "住所", "name": "氏名", "name_kana": "フリガナ", "phone": "電話番号",
               "organization": "会社名", "product_code": "商品番号", "qty": "数量", "noshi_name": "のし名入れ"}


def field_label(ft: str) -> str:
    """'deliveries.name' → 'お届け先の氏名'、'applicant.zip' → 'ご依頼主の郵便番号'。"""
    who = "ご依頼主" if ft.startswith("applicant") else "お届け先"
    key = ft.rsplit(".", 1)[-1]
    return f"{who}の{FIELD_NAMES.get(key, key)}"


# ポリシーキー → (現場向けの名前, 単位の説明)
POLICY_LABELS = {
    "audit_sampling_rate": ("自動で確定した分のうち、念のため人が見直す割合", "割合（0.1 = 10%）"),
    "promotion.L1.min_samples": ("ある項目を自動で確定し始めるまでに必要な実績の件数", "件"),
    "promotion.L1.min_streak": ("自動で確定し始めるまでに必要な、続けて間違いがなかった件数", "件"),
    "router.target_error_rate": ("自動で確定した項目に許す間違いの割合の目標", "割合（0.005 = 0.5%）"),
    "min_samples_for_sender": ("新しい送り主を「常連」として扱い始めるまでの枚数", "枚"),
    "hallucination.blank_ink_ratio": ("欄を「空欄」と判断するインクの量の目安", "欄の面積に対する割合"),
}


def policy_label(key: str) -> str:
    return POLICY_LABELS.get(key, (key, ""))[0]


def policy_unit(key: str) -> str:
    return POLICY_LABELS.get(key, (key, ""))[1]


_FT_RE = re.compile(r"(?<![A-Za-z_.])(applicant|deliveries)\.(name_kana|noshi_name|product_code|zip|address|name|phone|organization|qty)(?![A-Za-z_])")
_JARGON = [
    (re.compile(r"confusions_top|by_field_type|auto_missed|window_records|overall_correction_rate"), "集計"),
    (re.compile(r"フィールド"), "欄"),
    (re.compile(r"OCRモデル|抽出モデル|VLM"), "読み取り"),
    (re.compile(r"誤認識"), "読み違い"),
    (re.compile(r"修正率"), "人が直した割合"),
    (re.compile(r"エスカレーション"), "人への引き渡し"),
    (re.compile(r"閾値"), "基準"),
    (re.compile(r"監査サンプリング率|監査サンプリング|監査サンプル"), "抜き取り確認"),
    (re.compile(r"ルーティングモデル|ルーター"), "判定"),
    (re.compile(r"プロモーション"), "自動確定への昇格"),
]


def plain(text: str) -> str:
    """自由文（提案の題名・根拠など）に混ざった項目コードや内部用語を、現場の言葉に置き換える。"""
    if not text:
        return ""
    out = _FT_RE.sub(lambda m: field_label(m.group(0)), text)
    for pat, rep in _JARGON:
        out = pat.sub(rep, out)
    return out
