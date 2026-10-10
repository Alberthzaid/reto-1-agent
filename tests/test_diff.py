from PIL import Image, ImageDraw

from agent.domain.diff import changed_regions


def test_changed_regions_returns_union_of_changed_rectangle() -> None:
    before = Image.new("RGB", (100, 80), "white")
    after = before.copy()
    ImageDraw.Draw(after).rectangle((20, 30, 39, 44), fill="black")

    assert changed_regions(before, after) == [(20, 30, 20, 15)]


def test_changed_regions_returns_empty_list_for_identical_images() -> None:
    image = Image.new("RGB", (10, 10), "white")
    assert changed_regions(image, image.copy()) == []
