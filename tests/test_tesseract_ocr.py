import shutil
from pathlib import Path

import pytesseract
import pytest
from PIL import Image, ImageDraw, ImageFont, ImageStat

from agent.infra.tesseract_ocr import TesseractReader, _prepare


@pytest.mark.asyncio
async def test_reader_extracts_generated_spanish_text() -> None:
    standard_windows_install = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if shutil.which("tesseract") is None and not standard_windows_install.is_file():
        pytest.skip("Tesseract no está instalado")
    image = Image.new("RGB", (600, 180), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=44)
    draw.text((20, 20), "Guardar", fill="black", font=font)
    draw.text((20, 100), "Papelería", fill="black", font=font)

    boxes = await TesseractReader().read(image)

    text = " ".join(box.text for box in boxes).lower()
    assert "guardar" in text
    assert "papeler" in text
    assert all(box.x >= 0 and box.y >= 0 and box.conf >= 0.3 for box in boxes)


EMPTY_DATA: dict[str, list[object]] = {
    key: [] for key in ("text", "conf", "left", "top", "width", "height", "page_num", "block_num", "par_num", "line_num")
}


def _mean(image: Image.Image) -> float:
    return ImageStat.Stat(image.convert("L")).mean[0]


def test_prepare_inverts_light_text_on_dark_background() -> None:
    crop = Image.new("RGB", (200, 40), (20, 40, 160))
    ImageDraw.Draw(crop).rectangle((20, 10, 60, 30), fill="white")
    prepared = _prepare(crop)
    assert prepared.mode == "L"
    assert _mean(prepared) > 128
    assert prepared.getpixel((5, 5)) > 200, "dark background becomes light"
    assert prepared.getpixel((40, 20)) < 50, "light text becomes dark"


def test_prepare_keeps_dark_text_on_light_background() -> None:
    crop = Image.new("RGB", (200, 40), "white")
    ImageDraw.Draw(crop).rectangle((20, 10, 60, 30), fill="black")
    prepared = _prepare(crop)
    assert prepared.mode == "L"
    assert prepared.getpixel((5, 5)) == 255
    assert prepared.getpixel((40, 20)) == 0


@pytest.mark.asyncio
async def test_region_reads_invert_dark_crops_but_full_screen_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Image.Image] = []

    def fake_image_to_data(image: Image.Image, **_: object) -> dict[str, list[object]]:
        seen.append(image)
        return EMPTY_DATA

    monkeypatch.setattr(pytesseract, "image_to_data", fake_image_to_data)
    monkeypatch.setattr(pytesseract.pytesseract, "tesseract_cmd", pytesseract.pytesseract.tesseract_cmd)
    screen = Image.new("RGB", (1280, 800), (10, 20, 120))
    reader = TesseractReader(tesseract_cmd="tesseract")

    assert await reader.read(screen, (1057, 744, 204, 37)) == []
    assert await reader.read(screen) == []

    region_image, full_image = seen
    assert region_image.size == (408, 74), "the crop is still upscaled 2x"
    assert _mean(region_image) > 128, "dark crop inverted before OCR"
    assert _mean(full_image) < 128, "full-screen reads are not inverted"
