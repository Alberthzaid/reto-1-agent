"""Pixel-level change detection for screen observations."""

from collections import deque

import numpy as np
from PIL import Image

Rect = tuple[int, int, int, int]


def changed_regions(prev: Image.Image, curr: Image.Image, threshold: int = 24) -> list[Rect]:
    """Return bounding rectangles for connected groups of changed pixels."""
    before = np.asarray(prev.convert("RGB"), dtype=np.int16)
    after = np.asarray(curr.convert("RGB"), dtype=np.int16)
    if before.shape != after.shape:
        raise ValueError("las imágenes deben tener el mismo tamaño")
    changed = np.any(np.abs(after - before) >= threshold, axis=2)
    height, width = changed.shape
    seen = np.zeros_like(changed, dtype=bool)
    regions: list[Rect] = []

    for y, x in zip(*np.nonzero(changed)):
        if seen[y, x]:
            continue
        seen[y, x] = True
        queue: deque[tuple[int, int]] = deque([(x, y)])
        left = right = x
        top = bottom = y
        while queue:
            current_x, current_y = queue.popleft()
            left = min(left, current_x)
            right = max(right, current_x)
            top = min(top, current_y)
            bottom = max(bottom, current_y)
            for neighbor_x, neighbor_y in (
                (current_x - 1, current_y),
                (current_x + 1, current_y),
                (current_x, current_y - 1),
                (current_x, current_y + 1),
            ):
                if (
                    0 <= neighbor_x < width
                    and 0 <= neighbor_y < height
                    and changed[neighbor_y, neighbor_x]
                    and not seen[neighbor_y, neighbor_x]
                ):
                    seen[neighbor_y, neighbor_x] = True
                    queue.append((neighbor_x, neighbor_y))
        regions.append((left, top, right - left + 1, bottom - top + 1))
    return _merge_nearby(regions)


def _merge_nearby(regions: list[Rect], gap: int = 2) -> list[Rect]:
    """Coalesce boxes separated only by a few anti-aliased pixels."""
    merged = regions[:]
    changed = True
    while changed:
        changed = False
        for index, first in enumerate(merged):
            for other_index in range(index + 1, len(merged)):
                second = merged[other_index]
                if _overlaps(first, second, gap):
                    merged[index] = _union(first, second)
                    del merged[other_index]
                    changed = True
                    break
            if changed:
                break
    return sorted(merged, key=lambda rect: (rect[1], rect[0]))


def _overlaps(first: Rect, second: Rect, gap: int) -> bool:
    first_x, first_y, first_w, first_h = first
    second_x, second_y, second_w, second_h = second
    return not (
        first_x + first_w + gap < second_x
        or second_x + second_w + gap < first_x
        or first_y + first_h + gap < second_y
        or second_y + second_h + gap < first_y
    )


def _union(first: Rect, second: Rect) -> Rect:
    left = min(first[0], second[0])
    top = min(first[1], second[1])
    right = max(first[0] + first[2], second[0] + second[2])
    bottom = max(first[1] + first[3], second[1] + second[3])
    return left, top, right - left, bottom - top
