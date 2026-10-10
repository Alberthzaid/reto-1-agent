import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont

from agent.infra.tesseract_ocr import TesseractReader


@pytest.mark.asyncio
async def test_reader_extracts_generated_spanish_text() -> None:
    if shutil.which("tesseract") is None:
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
