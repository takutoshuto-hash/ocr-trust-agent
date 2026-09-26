"""様式ゾーン: 帳票様式ごとに「各項目が紙のどこにあるか」を正規化座標 (0-1) で持つ。

用途:
  - 空欄検知（欄にインクが無いのに値が返ってきた → ハルシネーション）
  - クラウド送信前の機密欄マスク（データ最小化）
  - 将来: 欄ごとの切り出し読み
様式ファイル: data/master/formats/<format_id>.json  { "zones": { "<path>": [x1,y1,x2,y2], ... } }
"""
from __future__ import annotations

import io
import json
from functools import lru_cache
from typing import Optional

from PIL import Image

from app.config import settings


@lru_cache(maxsize=16)
def load_zones(format_id: str) -> dict[str, tuple[float, float, float, float]]:
    path = settings.master_dir / "formats" / f"{format_id}.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: tuple(v) for k, v in data.get("zones", {}).items()}


def open_image(image: bytes) -> Optional[Image.Image]:
    try:
        return Image.open(io.BytesIO(image)).convert("L")
    except Exception:
        return None


def ink_ratio(img: Image.Image, zone: tuple[float, float, float, float], inset: int = 5, inset_y: int = 8,
              dark: int = 170, max_run: int = 60, window_px: int = 320) -> float:
    """ゾーン内の「文字らしい」暗画素率。

    - 枠線（横）や FAX の縦筋（縦）は「長い run」として除外してから数える（傾いた帳票でも空欄を誤らない）
    - 記入は左寄せが普通なので、欄の左端から window_px だけを見る（短い値を空欄と誤らない）
    - 上下は inset_y だけ内側を見る（隣の行の文字の裾が傾きで入り込むのを避ける）
    - dark=170: 薄いペン・細い書体がぼかしで灰色になっても拾う
    """
    import numpy as np

    W, H = img.size
    x1, y1, x2, y2 = int(zone[0] * W) + inset, int(zone[1] * H) + inset_y, int(zone[2] * W) - inset, int(zone[3] * H) - inset_y
    x2 = min(x2, x1 + window_px)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    a = np.asarray(img.crop((x1, y1, x2, y2)), dtype=np.uint8) < dark
    if a.size == 0:
        return 0.0
    # 傾いた枠線は行全体を暗くしないので、「長い連続した暗画素の走り（run）」を線とみなして除外する。
    # 斜めの線はアンチエイリアスで細切れになるため、線と直交する方向に 1px 膨張させてから run を測り、
    # 見つかった線は膨張分ごと元画像から消す。文字の横画は 30px フォントで 30px 前後なので残る。
    a = _remove_lines(a, max_run=max_run, axis=1)   # 横線（枠）
    a = _remove_lines(a, max_run=max_run, axis=0)   # 縦筋（FAX ノイズ）
    return float(a.mean())


def _remove_lines(a, max_run: int, axis: int):
    import numpy as np
    b = a if axis == 1 else a.T
    dil = b | np.roll(b, 1, axis=0) | np.roll(b, -1, axis=0)          # 直交方向に膨張
    mask = np.zeros_like(b)
    for i in range(dil.shape[0]):
        row = dil[i]
        if not row.any():
            continue
        d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
        for s, e in zip(starts, ends):
            if e - s > max_run:
                mask[i, s:e] = True
    for k in (1, -1, 2, -2):                                         # ぼかしで太った線の縁まで消す
        mask = mask | np.roll(mask, k, axis=0)
    out = b & ~mask
    return out if axis == 1 else out.T


def mask_zones(image: bytes, zones: list[tuple[float, float, float, float]]) -> bytes:
    """指定ゾーンを白で塗りつぶした PNG を返す（クラウドへ送る前の機密欄マスク）。"""
    img = Image.open(io.BytesIO(image)).convert("RGB")
    W, H = img.size
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    for x1, y1, x2, y2 in zones:
        d.rectangle((int(x1 * W), int(y1 * H), int(x2 * W), int(y2 * H)), fill=(255, 255, 255))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def load_frame(format_id: str):
    path = settings.master_dir / "formats" / f"{format_id}.json"
    if not path.exists():
        return None, None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("frame"), data.get("page")


def _sum3(v):
    w = v.copy(); w[1:] += v[:-1]; w[:-1] += v[1:]
    return w


def _long_rows(a, frac: float = 0.6) -> list[int]:
    """幅の frac 以上が暗い行（横線）。傾きで線が 2〜3 行に散っても拾えるよう、隣接 3 行の合計で見る。
    画像の上下 1% と左右 1% はスキャナの黒い縁が入ることがあるので数えない。"""
    H, W = a.shape
    mx, my = max(2, W // 100), max(2, H // 100)
    r3 = _sum3(a[:, mx:W - mx].sum(axis=1))
    return [y for y in range(my, H - my) if r3[y] > (W - 2 * mx) * frac]


def _row_extents(a, rows: list[int]) -> Optional[tuple[int, int, bool, bool]]:
    """横線それぞれの左端・右端（いちばん長い暗い run）を測り、中央値を返す。端が画像の縁に達していれば「切れている」印を付ける。"""
    import numpy as np
    H, W = a.shape
    mx = max(2, W // 100)
    lefts, rights = [], []
    for y in rows:
        band = a[max(0, y - 1): y + 2].any(axis=0)
        band[:mx] = False; band[W - mx:] = False
        xs = np.flatnonzero(band)
        if len(xs) < W * 0.3:
            continue
        # 5px 以内の途切れは同じ run とみなして最長の run を取る
        breaks = np.flatnonzero(np.diff(xs) > 5)
        starts = np.concatenate(([0], breaks + 1)); ends = np.concatenate((breaks, [len(xs) - 1]))
        k = int(np.argmax(xs[ends] - xs[starts]))
        lefts.append(int(xs[starts[k]])); rights.append(int(xs[ends[k]]))
    if len(lefts) < 2:
        return None
    lefts.sort(); rights.sort()
    l, r = lefts[len(lefts) // 2], rights[len(rights) // 2]
    return l, r, l <= mx + 2, r >= W - mx - 3


def _long_cols(a, frac: float = 0.5) -> list[int]:
    """高さの frac 以上が暗い列（縦枠）。ブロックの間に見出しの空きがあるので 5 割で足りる。"""
    H, W = a.shape
    c3 = _sum3(a.sum(axis=0))
    return [x for x in range(W) if c3[x] > H * frac]


def _dedupe_lines(ys: list[int]) -> list[int]:
    """連続する y（同じ線の太さ分）を 1 本にまとめ、中央の y を返す。"""
    out: list[list[int]] = []
    for y in ys:
        if out and y - out[-1][-1] <= 3:
            out[-1].append(y)
        else:
            out.append([y])
    return [(g[0] + g[-1]) // 2 for g in out]


def detect_frame(img: Image.Image, dark: int = 170):
    """欄の枠の外周（左, 上, 右, 下）。上下は横線の最初と最後、左右は横線の両端の中央値（縦の枠線は薄くて拾えないことがある）。
    見つからなければ None。"""
    ex = detect_frame_ex(img, dark)
    return None if ex is None else ex["frame"]


def detect_frame_ex(img: Image.Image, dark: int = 170) -> Optional[dict]:
    import numpy as np
    a = np.asarray(img, dtype=np.uint8) < dark
    rows = _dedupe_lines(_long_rows(a))
    if len(rows) < 2:
        return None
    ext = _row_extents(a, rows)
    if ext is None:
        return None
    l, r, left_cut, right_cut = ext
    return {"frame": (l, rows[0], r, rows[-1]), "rows": rows, "left_cut": left_cut, "right_cut": right_cut}


def to_image_bytes(data: bytes, dpi: int = 150) -> bytes:
    """PDF（複合機のスキャン）なら 1 ページ目を画像にして返す。画像ならそのまま。"""
    if data[:5] == b"%PDF-":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(data)
        if len(pdf) == 0:
            return data
        img = pdf[0].render(scale=dpi / 72).to_pil().convert("L")
        buf = io.BytesIO(); img.save(buf, format="PNG")
        return buf.getvalue()
    return data


def _upside_down(img: Image.Image, blocks: list[int], dark: int = 170) -> Optional[bool]:
    """横線の並びを上から数え、様式のブロック行数（上から）と比べる。逆順に合えば上下逆さ。判定できなければ None。"""
    import numpy as np
    a = np.asarray(img, dtype=np.uint8) < dark
    H, W = a.shape
    ys = _dedupe_lines(_long_rows(a))
    if len(ys) < 4:
        return None
    gaps = [b - a_ for a_, b in zip(ys, ys[1:])]
    pitch = sorted(gaps)[len(gaps) // 2]
    groups: list[list[int]] = [[ys[0]]]
    for prev, y in zip(ys, ys[1:]):
        (groups[-1].append(y) if y - prev <= pitch * 1.2 else groups.append([y]))
    counts = [len(g) - 1 for g in groups if len(g) >= 2]     # 線の数 − 1 = 行数
    if len(counts) < 2:
        return None
    if counts[0] == blocks[0] and counts[-1] == blocks[-1]:
        return False
    if counts[0] == blocks[-1] and counts[-1] == blocks[0] and blocks[0] != blocks[-1]:
        return True
    return None


def _skew_angle(img: Image.Image, dark: int = 170, max_deg: float = 10.0) -> float:
    """傾き（度）。縮小した 2 値画像を少しずつ回し、横線がいちばん揃う（行ごとの暗画素数の分散が最大になる）角度を探す。
    戻り値をそのまま Image.rotate に渡せば水平になる。"""
    import numpy as np
    w = 400
    small = img.resize((w, max(1, int(img.height * w / img.width))), Image.BILINEAR)
    base = Image.fromarray(((np.asarray(small, dtype=np.uint8) < dark) * 255).astype("uint8"))
    def score(angle: float) -> float:
        r = np.asarray(base.rotate(angle, expand=False, fillcolor=0), dtype=np.float64) / 255.0
        p = r.sum(axis=1)
        return float(((p - p.mean()) ** 2).sum())
    best, best_s = 0.0, score(0.0)
    for a in np.arange(-max_deg, max_deg + 0.001, 0.5):
        sc = score(float(a))
        if sc > best_s:
            best, best_s = float(a), sc
    for a in np.arange(best - 0.5, best + 0.501, 0.1):
        sc = score(float(a))
        if sc > best_s:
            best, best_s = float(a), sc
    return round(best, 2)


def register_to_template(image: bytes, format_id: str) -> bytes:
    """スキャン・FAX 画像を様式の枠に合わせる。順に、PDF なら画像化 → 横向きなら縦に → 傾き補正（横線がいちばん揃う角度、±10°）
    → 上下逆さなら 180° 回転（横線の並びを様式の blocks と比べる）→ 横線の並びと両端から枠を求め、平行移動と拡大縮小。
    倍率は横線の間隔から（下端や片側が紙からはみ出していても効く）。枠が見つからない／倍率が 15% 以上ずれる場合は元のまま返す。

    実物のスキャンは用紙の枠が数十 px ずれ、傾き、上下逆さ、片側の欠けも起きる。ゾーン（欄の位置）を当てる前に必ず通す。"""
    image = to_image_bytes(image)
    frame, page = load_frame(format_id)
    if not frame or not page:
        return image
    img = open_image(image)
    if img is None:
        return image
    W, H = int(page[0]), int(page[1])
    changed = False
    if img.width > img.height and H > W:                     # 横向きに置かれた
        img = img.rotate(90, expand=True, fillcolor=255); changed = True
    angle = _skew_angle(img)
    if 0.3 <= abs(angle) <= 10.0:
        img = img.rotate(angle, expand=False, fillcolor=255, resample=Image.BILINEAR); changed = True
    blocks = _load_blocks(format_id)
    if blocks and _upside_down(img, blocks) is True:
        img = img.rotate(180, expand=False, fillcolor=255); changed = True
    ex = detect_frame_ex(img)
    if ex is None:
        return _png(img) if changed else image
    tx1, ty1, tx2, ty2 = frame[0] * W, frame[1] * H, frame[2] * W, frame[3] * H
    sx1, sy1, sx2, sy2 = ex["frame"]
    ky = _pitch_scale(img, format_id, H)                              # 行の間隔から（下端が紙からはみ出していても効く）
    if ky is None:
        if sy2 - sy1 < 10:
            return _png(img) if changed else image
        ky = (ty2 - ty1) / (sy2 - sy1)
    if ex["left_cut"] and ex["right_cut"]:
        kx = ky
    elif ex["left_cut"]:                                              # 左が切れている: 右端を基準に置く
        kx = ky; sx1 = sx2 - (tx2 - tx1) / kx
    elif ex["right_cut"]:
        kx = ky; sx2 = sx1 + (tx2 - tx1) / kx
    else:
        kx = (tx2 - tx1) / (sx2 - sx1)
        if abs(kx - ky) > 0.06:                                        # 横線の端が欠けている等: 行の間隔の倍率に揃える
            kx = ky
    if not (0.85 <= kx <= 1.15 and 0.85 <= ky <= 1.15):
        return _png(img) if changed else image
    # PIL の affine は 出力座標 → 入力座標 の行列: x_in = a*x_out + b*y_out + c
    a, c = 1 / kx, sx1 - tx1 / kx
    e, f = 1 / ky, sy1 - ty1 / ky
    out = img.transform((W, H), Image.AFFINE, (a, 0, c, 0, e, f), resample=Image.BILINEAR, fillcolor=255)
    return _png(out)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO(); img.save(buf, format="PNG")
    return buf.getvalue()


def _load_blocks(format_id: str) -> Optional[list[int]]:
    path = settings.master_dir / "formats" / f"{format_id}.json"
    if not path.exists():
        return None
    b = json.loads(path.read_text(encoding="utf-8")).get("blocks")
    return [int(x) for x in b] if b else None


def registration_report(image: bytes, format_id: str) -> dict:
    """評価・診断用: 位置合わせの前後で何が起きたか（向き・傾き・枠）。"""
    image = to_image_bytes(image)
    img = open_image(image)
    if img is None:
        return {"ok": False, "reason": "画像として開けない"}
    blocks = _load_blocks(format_id)
    return {"ok": True, "size": img.size, "landscape": img.width > img.height,
            "upside_down": (_upside_down(img.rotate(_skew_angle(img), expand=False, fillcolor=255), blocks) if blocks else None), "skew_deg": round(_skew_angle(img), 2),
            "frame_before": detect_frame(img), "frame_after": detect_frame(open_image(register_to_template(image, format_id)))}


def _pitch_scale(img: Image.Image, format_id: str, H: int) -> Optional[float]:
    """横線の間隔（中央値）と様式の行間隔の比 = スキャン → 様式 の縦倍率。"""
    import numpy as np
    path = settings.master_dir / "formats" / f"{format_id}.json"
    rp = json.loads(path.read_text(encoding="utf-8")).get("row_pitch") if path.exists() else None
    if not rp:
        return None
    a = np.asarray(img, dtype=np.uint8) < 170
    ys = _dedupe_lines(_long_rows(a))
    gaps = sorted(b - a_ for a_, b in zip(ys, ys[1:]))
    if len(gaps) < 3:
        return None
    pitch = gaps[len(gaps) // 2]
    return (rp * H) / pitch if pitch > 0 else None
