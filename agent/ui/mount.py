"""Optional mounting of the Gradio control panel; `gradio` is only imported when the panel is enabled."""

import logging

from fastapi import FastAPI

from agent.ui.views import PanelContext

log = logging.getLogger(__name__)

UI_PATH = "/ui"
UI_USER = "admin"


def mount_ui(app: FastAPI, context: PanelContext, *, enabled: bool, team_token: str | None) -> bool:
    """Mount the panel at /ui behind a login (user `admin`, password TEAM_TOKEN). True when mounted."""
    if not enabled:
        return False
    if not team_token:
        log.warning("UI_ENABLED está activo pero falta TEAM_TOKEN: el panel no se monta")
        return False
    import gradio as gr

    from agent.ui.panel import build_panel

    traces_dir = (context.data_dir / "traces").resolve()
    traces_dir.mkdir(parents=True, exist_ok=True)
    gr.mount_gradio_app(
        app,
        build_panel(context),
        path=UI_PATH,
        auth=[(UI_USER, team_token)],
        auth_message="Inicie sesión con el usuario admin y el TEAM_TOKEN.",
        allowed_paths=[str(traces_dir)],
        show_error=False,
        ssr_mode=False,
        mcp_server=False,
        theme=gr.themes.Soft(),
    )
    log.info("panel de control montado en %s", UI_PATH)
    return True
