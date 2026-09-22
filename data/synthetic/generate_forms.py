"""合成帳票ジェネレータ（正解付き）。

Pillow で FAX 風の注文書を描画し、同名の .json に正解（OrderForm）と sender_id / format_id を書く。
手書きらしさ: フォントのランダム選択・文字ごとの位置ゆらぎ・回転・ノイズ・FAX の縦線。
data/fonts/ に手書き風 TTF を置くと使われる（無ければ Windows の MS ゴシック等）。
Nano Banana（画像生成）でさらに本物らしい手書きを作る場合は、この正解 JSON を条件に画像だけ差し替える。

使い方: python data/synthetic/generate_forms.py --n 200 --out data/synthetic/out
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
SURNAMES = ["佐藤", "鈴木", "高橋", "田中", "渡辺", "伊藤", "山本", "中村", "小林", "加藤", "吉田", "山田", "佐々木", "山口", "松本",
            "井上", "木村", "林", "斎藤", "清水", "山崎", "森", "池田", "橋本", "阿部", "石川", "山下", "中島", "石井", "小川", "首藤", "髙田"]
GIVEN = ["太郎", "花子", "健一", "美咲", "翔太", "陽子", "大輔", "恵子", "拓也", "愛", "誠", "由美", "直樹", "さくら", "隆", "裕子"]
KANA = {"佐藤": "サトウ", "鈴木": "スズキ", "高橋": "タカハシ", "田中": "タナカ", "渡辺": "ワタナベ", "伊藤": "イトウ", "山本": "ヤマモト",
        "中村": "ナカムラ", "小林": "コバヤシ", "加藤": "カトウ", "吉田": "ヨシダ", "山田": "ヤマダ", "佐々木": "ササキ", "山口": "ヤマグチ",
        "松本": "マツモト", "井上": "イノウエ", "木村": "キムラ", "林": "ハヤシ", "斎藤": "サイトウ", "清水": "シミズ", "山崎": "ヤマザキ",
        "森": "モリ", "池田": "イケダ", "橋本": "ハシモト", "阿部": "アベ", "石川": "イシカワ", "山下": "ヤマシタ", "中島": "ナカジマ",
        "石井": "イシイ", "小川": "オガワ", "首藤": "シュトウ", "髙田": "タカダ",
        "太郎": "タロウ", "花子": "ハナコ", "健一": "ケンイチ", "美咲": "ミサキ", "翔太": "ショウタ", "陽子": "ヨウコ", "大輔": "ダイスケ",
        "恵子": "ケイコ", "拓也": "タクヤ", "愛": "アイ", "誠": "マコト", "由美": "ユミ", "直樹": "ナオキ", "さくら": "サクラ", "隆": "タカシ", "裕子": "ユウコ"}
ORGS = ["", "", "", "株式会社大分商事", "有限会社山田工務店", "別府温泉組合", "湯布院観光協会", "九州食品株式会社"]
NOSHI = ["", "御中元", "御歳暮", "御礼", "内祝"]


def load_master():
    zips = list(csv.DictReader((ROOT / "data/master/zip.csv").open(encoding="utf-8")))
    products = [r["code"] for r in csv.DictReader((ROOT / "data/master/products.csv").open(encoding="utf-8"))]
    return zips, products


def fonts() -> list[ImageFont.FreeTypeFont]:
    cands = list((ROOT / "data/fonts").glob("*.tt[fc]")) + list((ROOT / "data/fonts").glob("*.otf"))
    if not cands:
        for name in ["msgothic.ttc", "YuGothM.ttc", "meiryo.ttc", "msmincho.ttc"]:
            p = Path("C:/Windows/Fonts") / name
            if p.exists():
                cands.append(p)
    out = []
    for p in cands:
        try:
            out.append(ImageFont.truetype(str(p), 30))
        except OSError:
            pass
    return out or [ImageFont.load_default()]


def person(rng):
    s, g = rng.choice(SURNAMES), rng.choice(GIVEN)
    return f"{s} {g}", f"{KANA[s]} {KANA[g]}"


def address(rng, zips):
    z = rng.choice(zips)
    town = z["town"] or ""
    return f'{z["zip"][:3]}-{z["zip"][3:]}', f'{z["pref"]}{z["city"]}{town}{rng.randint(1,5)}-{rng.randint(1,30)}-{rng.randint(1,20)}'


def phone(rng):
    return rng.choice([f"0{rng.randint(90,90)}-{rng.randint(1000,9999)}-{rng.randint(1000,9999)}",
                       f"097-{rng.randint(500,599)}-{rng.randint(1000,9999)}",
                       f"03-{rng.randint(3000,6999)}-{rng.randint(1000,9999)}"])


BLANK_RATE = 0.12   # 任意項目（フリガナ・電話・会社名・のし）が空欄で出される割合


def make_truth(rng, zips, products, sender_pool, blank_rate: float = BLANK_RATE):
    name, kana = person(rng)
    z, a = address(rng, zips)
    sender = rng.choice(sender_pool)

    def maybe_blank(v):
        return "" if rng.random() < blank_rate else v

    truth = {"applicant": {"name": name, "name_kana": maybe_blank(kana), "zip": z, "address": a, "phone": sender["phone"],
                           "organization": rng.choice(ORGS)}, "deliveries": []}
    for _ in range(rng.choice([1, 1, 2, 3])):
        n, k = person(rng)
        dz, da = address(rng, zips)
        truth["deliveries"].append({"name": n, "name_kana": maybe_blank(k), "zip": dz, "address": da,
                                    "phone": maybe_blank(phone(rng)),
                                    "product_code": rng.choice(products), "qty": rng.choice([1, 1, 1, 2, 3]),
                                    "noshi_name": rng.choice(NOSHI)})
    return truth, sender["id"]


def hand(draw, xy, text, font, rng, fill=(20, 20, 40)):
    x, y = xy
    for ch in text:
        dx, dy = rng.uniform(-1.5, 1.5), rng.uniform(-2, 2)
        draw.text((x + dx, y + dy), ch, font=font, fill=fill)
        x += font.getlength(ch) + rng.uniform(-1, 3)


def render(truth: dict, fnts, rng) -> Image.Image:
    W, H = 1240, 1754
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    label = ImageFont.truetype(str(Path("C:/Windows/Fonts/msgothic.ttc")), 22) if Path("C:/Windows/Fonts/msgothic.ttc").exists() else ImageFont.load_default()
    d.text((80, 60), "ギフト注文書（FAX）", font=label, fill=(0, 0, 0))
    d.text((80, 100), "ご依頼主", font=label, fill=(0, 0, 0))
    f = rng.choice(fnts)
    ap = truth["applicant"]
    rows = [("〒", ap["zip"]), ("住所", ap["address"]), ("フリガナ", ap["name_kana"]), ("氏名", ap["name"]),
            ("TEL", ap["phone"]), ("会社名", ap["organization"])]
    y = 140
    for lab, val in rows:
        d.rectangle((80, y, 1160, y + 44), outline=(0, 0, 0))
        d.text((90, y + 10), lab, font=label, fill=(0, 0, 0))
        hand(d, (260, y + 6), val, f, rng)
        y += 46
    for i, dl in enumerate(truth["deliveries"]):
        y += 30
        d.text((80, y), f"お届け先 {i+1}", font=label, fill=(0, 0, 0))
        y += 30
        rows = [("〒", dl["zip"]), ("住所", dl["address"]), ("フリガナ", dl["name_kana"]), ("氏名", dl["name"]),
                ("TEL", dl["phone"]), ("商品番号", dl["product_code"]), ("数量", str(dl["qty"])), ("のし名入れ", dl["noshi_name"])]
        for lab, val in rows:
            d.rectangle((80, y, 1160, y + 44), outline=(0, 0, 0))
            d.text((90, y + 10), lab, font=label, fill=(0, 0, 0))
            hand(d, (260, y + 6), val, rng.choice(fnts), rng)
            y += 46
    # FAX らしさ: 傾き・ノイズ・縦筋・にじみ
    img = img.rotate(rng.uniform(-1.2, 1.2), expand=False, fillcolor=(255, 255, 255))
    px = img.load()
    for _ in range(int(W * H * 0.002)):
        x, y2 = rng.randrange(W), rng.randrange(H)
        px[x, y2] = (rng.randint(0, 120),) * 3
    if rng.random() < 0.3:
        x = rng.randrange(W)
        d = ImageDraw.Draw(img)
        d.line((x, 0, x, H), fill=(90, 90, 90), width=2)
    return img.filter(ImageFilter.GaussianBlur(rng.uniform(0, 0.8))).convert("L").convert("RGB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--out", default=str(ROOT / "data/synthetic/out"))
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--senders", type=int, default=40, help="送り主プール数（小さいほどリピーターが多い）")
    ap.add_argument("--no-image", action="store_true", help="正解JSONだけ生成（高速）")
    a = ap.parse_args()

    rng = random.Random(a.seed)
    zips, products = load_master()
    fnts = fonts()
    pool = [{"id": f"S{i:04d}", "phone": phone(rng)} for i in range(a.senders)]
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    for i in range(a.n):
        truth, sender = make_truth(rng, zips, products, pool)
        stem = out / f"form_{a.seed:02d}_{i:05d}"
        (stem.with_suffix(".json")).write_text(json.dumps({"sender_id": sender, "format_id": "fax_v1", "truth": truth},
                                                          ensure_ascii=False, indent=1), encoding="utf-8")
        if not a.no_image:
            render(truth, fnts, rng).save(stem.with_suffix(".png"))
    print(f"generated {a.n} forms -> {out}")


if __name__ == "__main__":
    main()
