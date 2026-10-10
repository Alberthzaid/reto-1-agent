"""Tesseract implementation of the screen text reader port."""

import asyncio
import os
from collections import defaultdict
from pathlib import Path

import pytesseract
from PIL import Image, ImageOps, ImageStat
from pytesseract import Output

from agent.domain.models import TextBox

DARK_MEAN_LUMINANCE = 128


def _prepare(crop: Image.Image) -> Image.Image:
    """Grayscale crop; inverted when mostly dark so light-on-dark text (buttons, toasts) reads as dark-on-light."""
    gray = crop.convert("L")
    if ImageStat.Stat(gray).mean[0] < DARK_MEAN_LUMINANCE:
        return ImageOps.invert(gray)
    return gray


class TesseractReader:
    """Reads Spanish and English text from screen coordinates."""

    def __init__(self, tesseract_cmd: str | None = None) -> None:
        default_cmd = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        configured_cmd = tesseract_cmd or os.environ.get("TESSERACT_CMD")
        self._tesseract_cmd = configured_cmd or (str(default_cmd) if default_cmd.is_file() else None)
        default_tessdata = Path(os.environ.get("LOCALAPPDATA", "")) / "Tesseract-OCR" / "tessdata"
        configured_tessdata = os.environ.get("TESSDATA_PREFIX")
        self._tessdata_dir = configured_tessdata or (str(default_tessdata) if default_tessdata.is_dir() else None)

    async def read(
        self, img: Image.Image, region: tuple[int, int, int, int] | None = None
    ) -> list[TextBox]:
        return await asyncio.to_thread(self._read, img, region)

    def _read(
        self, img: Image.Image, region: tuple[int, int, int, int] | None
    ) -> list[TextBox]:
        origin_x = origin_y = 0
        if region is None:
            gray = img.convert("L")
        else:
            origin_x, origin_y, width, height = region
            gray = _prepare(img.crop((origin_x, origin_y, origin_x + width, origin_y + height)))
        scaled = gray.resize((gray.width * 2, gray.height * 2), Image.Resampling.LANCZOS)
        if self._tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self._tesseract_cmd
        tessdata_dir = self._tessdata_dir.replace("\\", "/") if self._tessdata_dir else None
        config = f"--tessdata-dir {tessdata_dir}" if tessdata_dir else ""
        data = pytesseract.image_to_data(
            scaled, lang="spa+eng", config=config, output_type=Output.DICT
        )
        lines: dict[tuple[int, int, int, int], list[tuple[str, int, int, int, int, float]]] = defaultdict(list)
        for index, raw_text in enumerate(data["text"]):
            text = raw_text.strip()
            try:
                confidence = float(data["conf"][index])
            except (TypeError, ValueError):
                continue
            if not text or confidence < 30:
                continue
            key = tuple(int(data[field][index]) for field in ("page_num", "block_num", "par_num", "line_num"))
            lines[key].append(
                (
                    text,
                    int(data["left"][index]),
                    int(data["top"][index]),
                    int(data["width"][index]),
                    int(data["height"][index]),
                    confidence,
                )
            )
        boxes: list[TextBox] = []
        for words in lines.values():
            left = min(word[1] for word in words)
            top = min(word[2] for word in words)
            right = max(word[1] + word[3] for word in words)
            bottom = max(word[2] + word[4] for word in words)
            boxes.append(
                TextBox(
                    text=" ".join(word[0] for word in words),
                    x=origin_x + left // 2,
                    y=origin_y + top // 2,
                    w=max(1, (right - left + 1) // 2),
                    h=max(1, (bottom - top + 1) // 2),
                    conf=sum(word[5] for word in words) / (100 * len(words)),
                )
            )
        return sorted(boxes, key=lambda box: (box.y, box.x))