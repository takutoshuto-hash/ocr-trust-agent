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


# 検証名 → 失敗したときの現場向けの一文。詳細（detail）は読んで意味がある検証だけ添える
CHECK_FAIL = {
    "zip_format": "郵便番号の形が違います（000-0000 の形）",
    "zip_address": "郵便番号と住所が合いません",
    "phone_format": "電話番号の形が違います",
    "phone_area": "電話の市外局番と住所の都道府県が合いません",
    "name_reading": "氏名とフリガナの読みが合いません",
    "kana_format": "フリガナにカタカナ以外が混ざっています",
    "product_exists": "商品番号が商品一覧にありません",
    "qty_range": "数量がふつうの範囲（1〜99）を外れています",
    "nonempty": "空欄になっています",
    "history": "この送り主の過去の注文と違います",
    "blank_zone": "欄は空欄に見えるのに値が読み取られています（読み取りの作り話の疑い）",
    "evidence": "読み取った文字と最終の値が食い違っています",
    "variant_kanji": "旧字体（髙・﨑など）が新字体に置き換わった疑いがあります",
}
_DETAIL_WORTH_SHOWING = {"zip_address", "phone_area", "name_reading", "product_exists", "history", "variant_kanji"}
_DETAIL_JARGON = [
    (re.compile(r"マスタに無い。近い候補: \[(.*?)\]"), lambda m: "近い番号: " + m.group(1).replace("'", "")),
    (re.compile(r"マスタ"), "一覧"),
    (re.compile(r"だが"), "ですが、"),
    (re.compile(r"（同じ市内の別番号に誤読の疑い）"), "。同じ市内の別の番号を読み違えたかもしれません"),
    (re.compile(r"確定値"), "注文"),
    (re.compile(r"異体字が常用漢字に置換された疑い: "), "元の字: "),
]


def plain_check(check) -> str:
    """CheckResult（FAIL）を現場の一文に。"""
    head = CHECK_FAIL.get(check.name, "読み取りの検証で引っかかりました")
    detail = (check.detail or "").strip()
    if check.name == "zip_address" and "一覧に無い" in detail:
        return "郵便番号が一覧にありません（読み違いの疑い）"
    if check.name in _DETAIL_WORTH_SHOWING and detail:
        for pat, rep in _DETAIL_JARGON:
            detail = pat.sub(rep, detail)
        return f"{head}（{plain(detail)}）"
    return head


def plain_verdict(verdict) -> str:
    """FieldVerdict を現場の言葉で。失敗した検証・2 回の読み取りの不一致・読み落としの疑いだけを書く（合格は書かない）。"""
    parts: list[str] = []
    for c in verdict.checks:
        st = getattr(c.status, "value", c.status)
        if st == "fail":
            parts.append(plain_check(c))
        elif st == "unknown" and c.name == "blank_zone" and "読み落とし" in (c.detail or ""):
            parts.append("欄に記入があるのに値が空です（読み落としの疑い）")
    if verdict.agreement is False:
        parts.append("2 回読んで結果が違いました")
    return "。".join(parts)


def plain_decision(*, status: str, audit: bool, forced_new_sender: bool, sender_n: int, router_trained: bool,
                   ledger_n: int, judge_ok: bool, agreement) -> str:
    """なぜ人が見るのか（検証の失敗以外の理由）を一文で。検証に失敗した項目は plain_verdict が理由になる。"""
    if status == "auto":
        return "自動で確定しましたが、念のための抜き取り確認です" if audit else "自動で確定"
    if not judge_ok:
        return ""
    if forced_new_sender:
        return f"初めての送り主なので、氏名と住所は人が確認する決まりです（この送り主の実績 {sender_n} 件）"
    if agreement is False:
        return ""
    if router_trained:
        return "自動で確定できるほどの確信がまだありません（似た項目で人の直しが続いているため、慎重にしています）"
    return f"自動で確定するには、まだこの項目の実績が足りません（実績 {ledger_n} 件）"
