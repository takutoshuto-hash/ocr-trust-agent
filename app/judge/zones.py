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


def ink_ratio(img: Image.Image, zone: tuple[float, float, float, float], inset: int = 5, dark: int = 128,
              max_run: int = 60) -> float:
    """ゾーン内の「文字らしい」暗画素率。

    枠線（横一直線）や FAX の縦筋（縦一直線）は、行または列の大半が暗い線として除外してから数える。
    傾いた帳票で枠線がゾーンに入っても空欄を誤って「記入あり」にしないため。
    """
    import numpy as np

    W, H = img.size
    x1, y1, x2, y2 = int(zone[0] * W) + inset, int(zone[1] * H) + inset, int(zone[2] * W) - inset, int(zone[3] * H) - inset
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
