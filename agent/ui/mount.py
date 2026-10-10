"""Optional mounting of the Gradio control panel; `gradio` is only imported when the panel is enabled."""

import logging

from fastapi import FastAPI

from agent.ui.views import PanelContext

log = logging.getLogger(__name__)

UI_PATH = "/ui"
UI_USER = "admin"


def mount_ui(app: FastAPI, context: PanelContext, *, enabled: bool, password: str | None) -> bool:
    """Mount the panel at /ui behind a login (user `admin`, `password`). True when mounted."""
    if not enabled:
        return False
    if not password:
        log.warning("UI_ENABLED está activo pero faltan UI_PASSWORD y TEAM_TOKEN: el panel no se monta")
        return False
    import gradio as gr

    from agent.ui.panel import build_panel
    from agent.ui.theme import BRAND_CSS, brand_theme

    traces_dir = (context.data_dir / "traces").resolve()
    traces_dir.mkdir(parents=True, exist_ok=True)
    gr.mount_gradio_app(
        app,
        build_panel(context),
        path=UI_PATH,
        # Behind a proxy (X-Forwarded-Host) Gradio builds event file URLs from root_path alone.
        root_path=UI_PATH,
        auth=[(UI_USER, password)],
        auth_message="Inicie sesión con el usuario admin.",
        allowed_paths=[str(traces_dir)],
        show_error=False,
        ssr_mode=False,
        mcp_server=False,
        theme=brand_theme(),
        css=BRAND_CSS,
    )
    log.info("panel de control montado en %s", UI_PATH)
    return True
