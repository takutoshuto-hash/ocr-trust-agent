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
    pref, city, _ = table[key]
    a = (address or "").replace(" ", "").replace("　", "")
    if pref in a and city in a:
        return CheckResult(name="zip_address", status=CheckStatus.PASS)
    return CheckResult(name="zip_address", status=CheckStatus.FAIL,
                       detail=f"郵便番号は「{pref}{city}」だが住所に含まれない")


def check_phone_format(phone: str) -> CheckResult:
    """電話番号の形式（0始まり・ハイフン区切り・桁数）。"""
    digits = re.sub(r"\D", "", phone or "")
    ok = bool(PHONE_RE.fullmatch(phone or "")) and len(digits) in (10, 11)
    return CheckResult(name="phone_format", status=CheckStatus.PASS if ok else CheckStatus.FAIL,
                       detail="" if ok else f"形式不正: {phone!r}")


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


def check_nonempty(value: str) -> CheckResult:
    ok = bool((value or "").strip())
    return CheckResult(name="nonempty", status=CheckStatus.PASS if ok else CheckStatus.FAIL,
                       detail="" if ok else "空欄")


def _lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
