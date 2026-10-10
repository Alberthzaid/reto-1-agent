"""Tesseract implementation of the screen text reader port."""

import asyncio
import os
from collections import defaultdict

import pytesseract
from PIL import Image
from pytesseract import Output

from agent.domain.models import TextBox


class TesseractReader:
    """Reads Spanish and English text from screen coordinates."""

    def __init__(self, tesseract_cmd: str | None = None) -> None:
        self._tesseract_cmd = tesseract_cmd or os.environ.get("TESSERACT_CMD")

    async def read(
        self, img: Image.Image, region: tuple[int, int, int, int] | None = None
    ) -> list[TextBox]:
        return await asyncio.to_thread(self._read, img, region)

    def _read(
        self, img: Image.Image, region: tuple[int, int, int, int] | None
    ) -> list[TextBox]:
        origin_x = origin_y = 0
        source = img
        if region is not None:
            origin_x, origin_y, width, height = region
            source = img.crop((origin_x, origin_y, origin_x + width, origin_y + height))
        scaled = source.convert("L").resize((source.width * 2, source.height * 2), Image.Resampling.LANCZOS)
        if self._tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self._tesseract_cmd
        data = pytesseract.image_to_data(scaled, lang="spa+eng", output_type=Output.DICT)
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