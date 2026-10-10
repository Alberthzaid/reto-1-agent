"""Brand look of the control panel: colors and fonts of saltoangel.co (Sora for headings, Manrope for text)."""

import gradio as gr

BRAND: dict[str, str] = {
    "forest": "#2f6b35",
    "jungle": "#225026",
    "growth": "#4a8c4f",
    "lime": "#a8d946",
    "lime_soft": "#e8f5c8",
    "ink": "#14201a",
    "text_2": "#475067",
    "text_3": "#5f6b63",
    "parchment": "#f5f7f6",
    "white": "#ffffff",
    "border": "#e8ebe9",
    "border_2": "#dde3de",
    "green_50": "#f2f6f2",
    "green_100": "#e6eee7",
    "nav_black": "#0d130f",
}

_GREENS = gr.themes.Color(
    c50="#f2f6f2",
    c100="#e6eee7",
    c200="#c3d7c6",
    c300="#9fc0a3",
    c400="#6fa275",
    c500="#4a8c4f",
    c600="#2f6b35",
    c700="#2a5e30",
    c800="#225026",
    c900="#17361a",
    c950="#0d130f",
    name="saltoangel_green",
)

BRAND_CSS = f"""
h1, h2, h3, h4, .prose h1, .prose h2, .prose h3 {{
    font-family: "Sora", system-ui, sans-serif !important;
    letter-spacing: -0.01em;
    color: {BRAND["ink"]};
}}
.gradio-container {{ letter-spacing: -0.008em; }}
button.primary {{ border-radius: 999px !important; }}
.tab-nav button.selected {{ color: {BRAND["forest"]} !important; border-color: {BRAND["lime"]} !important; }}
"""


def brand_theme() -> gr.themes.Base:
    """Gradio theme with the saltoangel.co palette and typography."""
    return gr.themes.Base(
        primary_hue=_GREENS,
        secondary_hue=_GREENS,
        neutral_hue=gr.themes.colors.gray,
        font=[gr.themes.GoogleFont("Manrope"), "system-ui", "sans-serif"],
        font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "ui-monospace", "monospace"],
        radius_size=gr.themes.sizes.radius_lg,
    ).set(
        body_background_fill=BRAND["parchment"],
        body_text_color=BRAND["ink"],
        body_text_color_subdued=BRAND["text_3"],
        background_fill_primary=BRAND["white"],
        background_fill_secondary=BRAND["green_50"],
        block_background_fill=BRAND["white"],
        block_border_color=BRAND["border"],
        block_label_text_color=BRAND["text_2"],
        border_color_primary=BRAND["border_2"],
        input_background_fill=BRAND["white"],
        input_border_color=BRAND["border_2"],
        input_border_color_focus=BRAND["growth"],
        link_text_color=BRAND["forest"],
        button_primary_background_fill=BRAND["forest"],
        button_primary_background_fill_hover=BRAND["jungle"],
        button_primary_text_color=BRAND["white"],
        button_secondary_background_fill=BRAND["lime_soft"],
        button_secondary_background_fill_hover=BRAND["lime"],
        button_secondary_text_color=BRAND["ink"],
        checkbox_background_color_selected=BRAND["forest"],
        slider_color=BRAND["forest"],
        color_accent=BRAND["lime"],
        color_accent_soft=BRAND["lime_soft"],
    )
