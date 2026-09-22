"""ジャッジが使う決定的ツール群。履歴ゼロ（新規記入者）でも動く検証。

各ツールは CheckResult を返す。ADK エージェント（agent.py）からも同じ関数を FunctionTool として使う。
"""
from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path

from app.config import settings
from app.schemas import CheckResult, CheckStatus

KATAKANA_RE = re.compile(r"^[ァ-ヶー・\s]+$")
ZIP_RE = re.compile(r"^\d{3}-\d{4}$")
PHONE_RE = re.compile(r"^0\d{1,4}-\d{1,4}-\d{3,4}$")


@lru_cache(maxsize=1)
def _zip_table() -> dict[str, tuple[str, str, str]]:
    """郵便番号 -> (都道府県, 市区町村, 町域)。data/master/zip.csv（日本郵便 KEN_ALL を整形したもの）"""
    path: Path = settings.master_dir / "zip.csv"
    table: dict[str, tuple[str, str, str]] = {}
    if not path.exists():
        return table
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            table[row["zip"]] = (row["pref"], row["city"], row.get("town", ""))
    return table


@lru_cache(maxsize=1)
def _products() -> dict[str, dict]:
    path: Path = settings.master_dir / "products.csv"
    table: dict[str, dict] = {}
    if not path.exists():
        return table
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            table[row["code"].upper()] = row
    return table


def check_zip_format(zip_code: str) -> CheckResult:
    """郵便番号が NNN-NNNN 形式か。"""
    ok = bool(ZIP_RE.fullmatch(zip_code or ""))
    return CheckResult(name="zip_format", status=CheckStatus.PASS if ok else CheckStatus.FAIL,
                       detail="" if ok else f"形式不正: {zip_code!r}")


def check_zip_address(zip_code: str, address: str) -> CheckResult:
    """郵便番号から引いた都道府県・市区町村が住所に含まれるか。"""
    table = _zip_table()
    key = (zip_code or "").replace("-", "")
    if key not in table:
        return CheckResult(name="zip_address", status=CheckStatus.UNKNOWN, detail="郵便番号がマスタに無い")
    pref, city, town = table[key]
    a = (address or "").replace(" ", "").replace("　", "")
    if not (pref in a and city in a):
        return CheckResult(name="zip_address", status=CheckStatus.FAIL,
                           detail=f"郵便番号は「{pref}{city}」だが住所に含まれない")
    # 町域まで突合する。同じ市内の別の郵便番号への誤読（例 870-0001↔870-0021）は市区町村では検出できない
    if town and town not in a:
        return CheckResult(name="zip_address", status=CheckStatus.FAIL,
                           detail=f"郵便番号の町域「{town}」が住所に無い（同じ市内の別番号に誤読の疑い）")
    return CheckResult(name="zip_address", status=CheckStatus.PASS)


def check_phone_format(phone: str) -> CheckResult:
    """電話番号の形式（0始まり・ハイフン区切り・桁数）。空欄は任意項目として判定不能（空欄検知が真偽を見る）。"""
    if not (phone or "").strip():
        return CheckResult(name="phone_format", status=CheckStatus.UNKNOWN, detail="空欄")
    digits = re.sub(r"\D", "", phone or "")
    ok = bool(PHONE_RE.fullmatch(phone or "")) and len(digits) in (10, 11)
    return CheckResult(name="phone_format", status=CheckStatus.PASS if ok else CheckStatus.FAIL,
                       detail="" if ok else f"形式不正: {phone!r}")


@lru_cache(maxsize=1)
def _area_table() -> dict[str, set[str]]:
    """市外局番 -> 都道府県の集合。data/master/area_codes.csv（総務省の市外局番一覧を要約したもの）"""
    path: Path = settings.master_dir / "area_codes.csv"
    table: dict[str, set[str]] = {}
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                table[row["code"]] = set(row["pref"].split("|"))
    return table


MOBILE_PREFIXES = ("070", "080", "090", "050", "0120", "0800", "0570")


def check_phone_area(phone: str, zip_code: str) -> CheckResult:
    """電話の市外局番が、郵便番号の都道府県と整合するか（項目間の相互検証）。

    携帯・IP 電話・フリーダイヤルは所在地を持たないので判定不能。固定電話の市外局番の誤読、
    または郵便番号の誤読（別の都道府県）をどちらか一方の根拠だけで見抜ける。"""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return CheckResult(name="phone_area", status=CheckStatus.UNKNOWN, detail="空欄")
    if digits.startswith(MOBILE_PREFIXES):
        return CheckResult(name="phone_area", status=CheckStatus.UNKNOWN, detail="携帯・IP 電話（所在地なし）")
    zt = _zip_table()
    z = (zip_code or "").replace("-", "")
    if z not in zt:
        return CheckResult(name="phone_area", status=CheckStatus.UNKNOWN, detail="郵便番号がマスタに無い")
    at = _area_table()
    code = next((digits[:n] for n in (5, 4, 3, 2) if digits[:n] in at), None)
    if code is None:
        return CheckResult(name="phone_area", status=CheckStatus.UNKNOWN, detail="市外局番がマスタに無い")
    pref = zt[z][0]
    if pref in at[code]:
        return CheckResult(name="phone_area", status=CheckStatus.PASS, detail=f"市外局番 {code} = {pref}")
    return CheckResult(name="phone_area", status=CheckStatus.FAIL,
                       detail=f"市外局番 {code}（{'/'.join(sorted(at[code]))}）が郵便番号の都道府県「{pref}」と合わない")


@lru_cache(maxsize=1)
def _surname_table() -> dict[str, set[str]]:
    path: Path = settings.master_dir / "surname_readings.csv"
    table: dict[str, set[str]] = {}
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                table[row["surname"]] = set(row["readings"].split("|"))
    return table


def check_name_reading(name: str, kana: str) -> CheckResult:
    """氏名の姓とフリガナの姓が、姓の読み辞書で整合するか（項目間の相互検証）。

    姓が辞書に無ければ判定不能。読みが辞書と食い違えば、氏名かフリガナのどちらかが誤読。"""
    n = (name or "").strip()
    k = (kana or "").strip()
    if not n or not k:
        return CheckResult(name="name_reading", status=CheckStatus.UNKNOWN, detail="氏名またはフリガナが空欄")
    table = _surname_table()
    surname = re.split(r"[ \u3000]", n, 1)[0]
    if surname not in table:
        surname = next((s for s in sorted(table, key=len, reverse=True) if n.startswith(s)), "")
    if not surname:
        return CheckResult(name="name_reading", status=CheckStatus.UNKNOWN, detail="姓が読み辞書に無い")
    kana_surname = re.split(r"[ \u3000]", k, 1)[0]
    readings = table[surname]
    if kana_surname in readings or any(k.startswith(r) for r in readings):
        return CheckResult(name="name_reading", status=CheckStatus.PASS, detail=f"{surname} = {'/'.join(sorted(readings))}")
    return CheckResult(name="name_reading", status=CheckStatus.FAIL,
                       detail=f"姓「{surname}」の読みは {'/'.join(sorted(readings))} だがフリガナは「{kana_surname}」")


def check_kana(kana: str) -> CheckResult:
    """フリガナがカタカナのみか。"""
    if not kana:
        return CheckResult(name="kana_format", status=CheckStatus.UNKNOWN, detail="空欄")
    ok = bool(KATAKANA_RE.fullmatch(kana))
    return CheckResult(name="kana_format", status=CheckStatus.PASS if ok else CheckStatus.FAIL,
                       detail="" if ok else "カタカナ以外を含む")


def check_product_code(code: str) -> CheckResult:
    """商品コードが商品マスタに存在するか。"""
    table = _products()
    if not code:
        return CheckResult(name="product_exists", status=CheckStatus.FAIL, detail="空欄")
    if not table:
        return CheckResult(name="product_exists", status=CheckStatus.UNKNOWN, detail="商品マスタ未設定")
    if code.upper() in table:
        return CheckResult(name="product_exists", status=CheckStatus.PASS, detail=table[code.upper()].get("name", ""))
    near = [c for c in table if _lev(c, code.upper()) <= 1]
    return CheckResult(name="product_exists", status=CheckStatus.FAIL,
                       detail=f"マスタに無い。近い候補: {near[:3]}" if near else "マスタに無い")


def check_qty(qty: int) -> CheckResult:
    """数量の妥当性（1〜99）。"""
    ok = isinstance(qty, int) and 1 <= qty <= 99
    return CheckResult(name="qty_range", status=CheckStatus.PASS if ok else CheckStatus.FAIL,
                       detail="" if ok else f"範囲外: {qty}")


def check_history(value, past_values: list) -> CheckResult:
    """送り主の過去の確定値との照合（独立した根拠）。

    - 過去に同じ値が確定している → PASS（依頼主の氏名・住所・電話は送り主ごとにほぼ固定）
    - 過去値はあるが一致しない → UNKNOWN（転居・別人の可能性。誤読の疑いとして特徴量に残す）
    - 過去値なし → UNKNOWN
    """
    if not past_values:
        return CheckResult(name="history", status=CheckStatus.UNKNOWN, detail="履歴なし")
    v = _squash(value)
    if not v:
        return CheckResult(name="history", status=CheckStatus.UNKNOWN, detail="空欄")
    if any(_squash(p) == v for p in past_values):
        return CheckResult(name="history", status=CheckStatus.PASS, detail="過去の確定値と一致")
    return CheckResult(name="history", status=CheckStatus.UNKNOWN, detail=f"過去の確定値と不一致（例: {past_values[0]!r}）")


def check_nonempty(value: str) -> CheckResult:
    ok = bool((value or "").strip())
    return CheckResult(name="nonempty", status=CheckStatus.PASS if ok else CheckStatus.FAIL,
                       detail="" if ok else "空欄")


def check_blank_zone(ink: float | None, value: str, blank_ratio: float) -> CheckResult:
    """欄のインク量と値の整合（ハルシネーション検知）。

    - 欄がほぼ白紙なのに値がある → FAIL（読めない所をもっともらしく埋めた疑い）
    - 欄にインクがあるのに値が空 → UNKNOWN（読み落とし疑い。人が見る）
    - ゾーン情報が無い / 画像が無い → UNKNOWN
    """
    if ink is None:
        return CheckResult(name="blank_zone", status=CheckStatus.UNKNOWN, detail="ゾーン未定義")
    has_value = bool((value or "").strip())
    if ink < blank_ratio and has_value:
        return CheckResult(name="blank_zone", status=CheckStatus.FAIL,
                           detail=f"欄は空欄（インク率 {ink:.4f}）なのに値 {value!r} が返った → ハルシネーション疑い")
    if ink >= blank_ratio and not has_value:
        return CheckResult(name="blank_zone", status=CheckStatus.UNKNOWN,
                           detail=f"欄に記入あり（インク率 {ink:.4f}）だが値が空 → 読み落とし疑い")
    return CheckResult(name="blank_zone", status=CheckStatus.PASS)


def check_evidence(value: str, evidence: str, max_distance: float) -> CheckResult:
    """モデルが「読んだ文字列そのもの（evidence）」と正規化後の value の整合。"""
    v, e = _squash(value), _squash(evidence)
    if not v or not e:
        return CheckResult(name="evidence", status=CheckStatus.UNKNOWN, detail="根拠なし")
    dist = _lev(v, e) / max(len(v), len(e))
    if dist > max_distance:
        return CheckResult(name="evidence", status=CheckStatus.FAIL,
                           detail=f"value {value!r} と根拠 {evidence!r} が乖離（距離 {dist:.2f}）")
    return CheckResult(name="evidence", status=CheckStatus.PASS)


# 宛名でよく使われる異体字と、モデルが寄せがちな常用漢字
VARIANT_KANJI = {"髙": "高", "﨑": "崎", "邊": "辺", "邉": "辺", "齋": "斎", "齊": "斉", "德": "徳", "栁": "柳",
                 "濵": "浜", "濱": "浜", "瀨": "瀬", "𠮷": "吉", "𩙿": "飯", "眞": "真", "櫻": "桜", "澤": "沢", "壽": "寿"}


def restore_variant_kanji(value: str, evidence: str) -> tuple[str, list[str]]:
    """evidence（読んだ文字列そのもの）に異体字があり、value で常用漢字に置き換わっていれば value 側を異体字に戻す。

    戻り値: (復元後の値, 復元した文字の一覧)。位置は空白を除いた文字列で対応づける。
    """
    if not value or not evidence:
        return value, []
    v = list(value)
    ev = [ch for ch in evidence if not ch.isspace()]
    vi = [i for i, ch in enumerate(v) if not ch.isspace()]
    if len(ev) != len(vi):
        return value, []
    restored = []
    for pos, ch in zip(vi, ev):
        if ch in VARIANT_KANJI and v[pos] == VARIANT_KANJI[ch]:
            v[pos] = ch
            restored.append(f"{VARIANT_KANJI[ch]}→{ch}")
    return "".join(v), restored


def check_variant_kanji(value: str, evidence: str) -> CheckResult:
    """value に常用漢字があり evidence が異体字なら、字体が失われた疑い（宛名では別字扱い）。"""
    _, restored = restore_variant_kanji(value, evidence)
    if restored:
        return CheckResult(name="variant_kanji", status=CheckStatus.FAIL, detail="異体字が常用漢字に置換された疑い: " + ", ".join(restored))
    return CheckResult(name="variant_kanji", status=CheckStatus.PASS)


def _squash(s) -> str:
    """比較用の正規化: NFKC（全角英数→半角、半角カナ→全角）、空白・ハイフン類・記号を除去、大文字化。"""
    import unicodedata
    t = unicodedata.normalize("NFKC", str(s or ""))
    return re.sub(r"[\s\-‐‑–—−ー－〒()（）]", "", t).upper()


def _lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
