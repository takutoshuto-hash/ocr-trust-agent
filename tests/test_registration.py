"""スキャン・FAX の位置ずれ: 用紙の枠を検出して様式の枠に合わせてからゾーン（欄の位置）を当てる。"""
import io

from PIL import Image

from app.judge.zones import detect_frame, ink_ratio, load_frame, load_zones, open_image, register_to_template


def _template() -> Image.Image:
    return Image.open("data/measurement/handwriting/blank_fax_v1.png").convert("L")


def _shifted(img: Image.Image, dx: int, dy: int, scale: float = 1.0) -> bytes:
    """様式画像を dx, dy だけずらし、scale 倍して同じ紙面サイズに置く（複合機のずれ・縮小の模倣）。"""
    W, H = img.size
    small = img.resize((int(W * scale), int(H * scale)), Image.BILINEAR)
    out = Image.new("L", (W, H), 255)
    out.paste(small, (dx, dy))
    buf = io.BytesIO(); out.save(buf, format="PNG")
    return buf.getvalue()


def test_frame_of_template_matches_declared_frame():
    frame, page = load_frame("fax_v1")
    W, H = page
    found = detect_frame(_template())
    assert found is not None
    for got, want in zip(found, (frame[0] * W, frame[1] * H, frame[2] * W, frame[3] * H)):
        assert abs(got - want) <= 2


def test_registration_undoes_shift_and_scale():
    frame, page = load_frame("fax_v1")
    W, H = page
    want = (round(frame[0] * W), round(frame[1] * H), round(frame[2] * W), round(frame[3] * H))
    for dx, dy, k in ((-34, 3, 1.0), (25, -12, 1.0), (10, 20, 0.96), (-8, -60, 1.04)):   # 1.04 は下端が紙に収まる範囲で
        reg = register_to_template(_shifted(_template(), dx, dy, k), "fax_v1")
        got = detect_frame(open_image(reg))
        assert got is not None, (dx, dy, k)
        assert all(abs(g - w) <= 8 for g, w in zip(got, want)), (dx, dy, k, got, want)


def test_registration_keeps_original_when_no_frame():
    blank = Image.new("L", (1240, 1754), 255)
    buf = io.BytesIO(); blank.save(buf, format="PNG")
    assert register_to_template(buf.getvalue(), "fax_v1") == buf.getvalue()
    assert register_to_template(b"not an image", "fax_v1") == b"not an image"


def test_zones_start_right_after_the_printed_label():
    """実物はラベルのすぐ右から書き始める。ゾーンはラベルの直後から始まり、ラベルの印字は含まない（空欄が空欄と判定される）。"""
    img = _template()
    zones = load_zones("fax_v1")
    for path, z in zones.items():
        assert ink_ratio(img, z) < 0.013, path          # 空欄の様式: どの欄も印字ラベルが混ざらない
    # ラベル直後に書いた短い文字を拾う: 〒 の直後（x=115px〜）に 3 桁を描く
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    x1, y1, x2, y2 = zones["applicant.zip"]
    W, H = img.size
    for k in range(3):
        d.rectangle((int(x1 * W) + 4 + k * 24, int(y1 * H) + 12, int(x1 * W) + 18 + k * 24, int(y2 * H) - 12), fill=0)
    assert ink_ratio(img, zones["applicant.zip"]) >= 0.0208


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO(); img.save(buf, format="PNG")
    return buf.getvalue()


def _frame_ok(image: bytes) -> bool:
    frame, page = load_frame("fax_v1")
    W, H = page
    want = (frame[0] * W, frame[1] * H, frame[2] * W, frame[3] * H)
    got = detect_frame(open_image(register_to_template(image, "fax_v1")))
    return got is not None and all(abs(g - w) <= 8 for g, w in zip(got, want))


def test_registration_handles_upside_down_landscape_and_skew():
    """複合機・FAX で実際に起きる置き方: 上下逆さ、横向き、1〜3 度の傾き。すべて様式の枠に戻る。"""
    img = _template()
    assert _frame_ok(_png(img.rotate(180)))
    assert _frame_ok(_png(img.rotate(90, expand=True, fillcolor=255)))
    for deg in (1.5, -1.5, 3.0):
        assert _frame_ok(_png(img.rotate(deg, expand=False, fillcolor=255))), deg
    # 上下逆さ + ずれ + 傾きの組み合わせ
    combo = img.rotate(180).rotate(-1.2, expand=False, fillcolor=255)
    assert _frame_ok(_shifted(combo, -20, 15))


def test_pdf_scan_is_rendered_to_an_image():
    from app.judge.zones import to_image_bytes
    img = _template()
    buf = io.BytesIO(); img.convert("RGB").save(buf, format="PDF", resolution=150)
    pdf = buf.getvalue()
    assert pdf[:5] == b"%PDF-"
    png = to_image_bytes(pdf)
    assert png[:8].startswith(b"\x89PNG")
    out = open_image(png)
    assert out is not None and abs(out.width / out.height - img.width / img.height) < 0.01
    assert _frame_ok(pdf)                      # PDF のまま位置合わせに渡せる
    assert to_image_bytes(_png(img)) == _png(img)   # 画像はそのまま


def test_real_handwritten_scans_register_and_ink_matches_truth():
    """本人の複合機スキャン（ずれ・傾き 8°・上下逆さ・左端の欠け・薄い縦枠・黒い縁・欄の中央に書いた値を含む）。全部が様式の枠に戻り、
    記入欄にはインクがあり、空欄にはインクが無いと判定される（空欄検知が実物で成り立つ）。"""
    import glob
    import json
    from app.schemas import OrderForm
    zones = load_zones("fax_v1")
    frame, page = load_frame("fax_v1")
    W, H = page
    want = (frame[0] * W, frame[1] * H, frame[2] * W, frame[3] * H)
    pdfs = sorted(glob.glob("data/measurement/handwriting/hw_*.pdf"))
    assert len(pdfs) >= 17
    for pdf in pdfs:
        raw = open(pdf, "rb").read()
        img = open_image(register_to_template(raw, "fax_v1"))
        got = detect_frame(img)
        assert got is not None and all(abs(g - w) <= 12 for g, w in zip(got, want)), (pdf, got)
        flat = OrderForm.model_validate(json.load(open(pdf[:-4] + ".json", encoding="utf-8"))["truth"]).flatten()
        for path, z in zones.items():
            v = flat.get(path)
            r = ink_ratio(img, z)
            if v is None:                                   # 使っていないお届け先ブロック
                assert r < 0.013, (pdf, path, r)
            elif isinstance(v, int) or len(str(v).strip()) == 1:
                continue                                    # 数量・1 文字は空欄検知の対象外（設計どおり）
            elif str(v).strip():
                assert r >= 0.0208, (pdf, path, r)
            else:
                assert r < 0.013, (pdf, path, r)
