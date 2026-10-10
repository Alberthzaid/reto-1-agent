import pytest

gr = pytest.importorskip("gradio")

from agent.ui.theme import BRAND, BRAND_CSS, brand_theme  # noqa: E402


def test_brand_palette_matches_saltoangel() -> None:
    assert BRAND["forest"] == "#2f6b35"
    assert BRAND["lime"] == "#a8d946"
    assert BRAND["ink"] == "#14201a"
    assert BRAND["parchment"] == "#f5f7f6"


def test_theme_uses_brand_fonts_and_colors() -> None:
    theme = brand_theme()
    assert isinstance(theme, gr.themes.Base)
    body_fonts = [getattr(f, "name", str(f)) for f in theme._font]
    assert body_fonts[0] == "Manrope"
    assert theme.button_primary_background_fill == BRAND["forest"]
    assert theme.body_background_fill == BRAND["parchment"]
    assert theme.body_text_color == BRAND["ink"]


def test_css_applies_display_font_to_headings() -> None:
    assert "Sora" in BRAND_CSS
    assert "h1" in BRAND_CSS
