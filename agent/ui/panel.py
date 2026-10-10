"""Gradio wiring of the control panel. All logic lives in `agent.ui.views`; this module only binds components."""

import asyncio
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import gradio as gr

from agent.application.runtime import RuntimeValues
from agent.domain.models import Procedure
from agent.observability.log_setup import redact, register_secret
from agent.observability.traces import read_trace
from agent.ui import views
from agent.ui.views import PanelContext

log = logging.getLogger(__name__)

LIVE_REFRESH_S = 2.0
HEADER_REFRESH_S = 5.0
MAX_LOG_LINES_SHOWN = 500
PRUEBAS_DIRS = ("pruebas", "custom_tests")
DEFAULT_AGENT_URL = "http://localhost:8000"


def build_panel(ctx: PanelContext) -> gr.Blocks:
    with gr.Blocks(title="Agent-QA", analytics_enabled=False) as panel:
        gr.Markdown("# Agent-QA · Panel de control")
        header = gr.Markdown()
        header_timer = gr.Timer(HEADER_REFRESH_S)

        async def refresh_header() -> str:
            label = views.model_label(ctx.runtime.current(), ctx.uses_llm)
            return views.header_markdown(ctx.manager.ready, label, ctx.manager.running_count)

        panel.load(refresh_header, outputs=header)
        header_timer.tick(refresh_header, outputs=header)

        with gr.Tabs():
            with gr.Tab("Ejecutar"):
                _run_tab(ctx)
            with gr.Tab("Tareas") as tasks_tab:
                _tasks_tab(ctx, panel, tasks_tab)
            with gr.Tab("Registros"):
                _logs_tab(ctx)
            with gr.Tab("Configuración"):
                _config_tab(ctx, panel)
            with gr.Tab("Pruebas"):
                _tests_tab(ctx, panel)
            if ctx.procedures is not None and ctx.procedures.has_procedures_table():
                with gr.Tab("Memoria") as memory_tab:
                    _memory_tab(ctx, memory_tab)
    return panel


async def _live_view(ctx: PanelContext, task_id: str | None) -> tuple[str, list[tuple[str, str]], list[list[Any]]]:
    if not task_id:
        return views.task_detail(None), [], []
    task = await ctx.manager.get(task_id)
    steps = await asyncio.to_thread(read_trace, task_id, ctx.data_dir)
    return views.task_detail(task), views.gallery_items(steps), views.step_rows(steps)


def _steps_components() -> tuple[gr.Gallery, gr.Dataframe]:
    gallery = gr.Gallery(label="Capturas por paso", columns=4, height=420, object_fit="contain")
    table = gr.Dataframe(headers=views.STEP_HEADERS, label="Acciones", interactive=False, wrap=True)
    return gallery, table


# --- Ejecutar ------------------------------------------------------------------------------------


def _run_tab(ctx: PanelContext) -> None:
    current = gr.State(None)
    instruction = gr.Textbox(label="Instrucción", lines=3, placeholder="Describa la tarea para el agente")
    with gr.Row():
        url = gr.Textbox(label="URL del escritorio", value=ctx.desktop_url or "",
                         placeholder="wss://<escritorio>/websockify")
        password = gr.Textbox(
            label="Contraseña del escritorio",
            type="password",
            value="",
            placeholder="Vacío: se usa DESKTOP_PASSWORD de .env" if ctx.desktop_password else "",
        )
    with gr.Row():
        run_button = gr.Button("Ejecutar", variant="primary")
        cancel_button = gr.Button("Cancelar", variant="stop")
    status = gr.Markdown(views.task_detail(None))
    gallery, table = _steps_components()
    timer = gr.Timer(LIVE_REFRESH_S)

    async def run_task(text: str, desktop_url: str, desktop_password: str) -> tuple[str, str, str]:
        if not text.strip():
            raise gr.Error("Escriba una instrucción.")
        try:
            final_url, final_password = views.resolve_desktop(
                desktop_url, desktop_password, ctx.desktop_url, ctx.desktop_password
            )
        except ValueError as exc:
            raise gr.Error(str(exc)) from None
        task_id = ctx.manager.create(text.strip(), final_url, final_password)
        log.info("tarea %s creada desde el panel", task_id)
        return task_id, "", views.task_detail(await ctx.manager.get(task_id))

    async def cancel_task(task_id: str | None) -> str:
        if not task_id:
            gr.Warning("No hay ninguna tarea lanzada desde esta pestaña.")
            return views.task_detail(None)
        if await ctx.manager.cancel(task_id):
            gr.Info("Tarea cancelada.")
        else:
            gr.Info("La tarea ya había terminado.")
        return views.task_detail(await ctx.manager.get(task_id))

    async def refresh(task_id: str | None) -> tuple[str, list[tuple[str, str]], list[list[Any]]]:
        return await _live_view(ctx, task_id)

    run_button.click(run_task, inputs=[instruction, url, password], outputs=[current, password, status])
    cancel_button.click(cancel_task, inputs=current, outputs=status)
    timer.tick(refresh, inputs=current, outputs=[status, gallery, table])


# --- Tareas --------------------------------------------------------------------------------------


def _tasks_tab(ctx: PanelContext, panel: gr.Blocks, tab: gr.Tab) -> None:
    selected = gr.State(None)
    refresh_button = gr.Button("Actualizar")
    tasks = gr.Dataframe(headers=views.TASK_HEADERS, label="Tareas recientes", interactive=False, wrap=True)
    detail = gr.Markdown(views.task_detail(None))
    cancel_button = gr.Button("Cancelar tarea", variant="stop", visible=False)
    gallery, table = _steps_components()

    async def load_tasks() -> list[list[str]]:
        return views.task_rows(await ctx.manager.list_tasks())

    async def show(task_id: str | None) -> tuple[str | None, str, list[tuple[str, str]], list[list[Any]], Any]:
        text, items, rows = await _live_view(ctx, task_id)
        task = await ctx.manager.get(task_id) if task_id else None
        return task_id, text, items, rows, gr.Button(visible=views.is_active(task))

    async def select(evt: gr.SelectData) -> tuple[str | None, str, list[tuple[str, str]], list[list[Any]], Any]:
        row = evt.row_value or []
        return await show(str(row[0]) if row else None)

    async def cancel_selected(task_id: str | None) -> tuple[str | None, str, list[tuple[str, str]], list[list[Any]], Any]:
        if task_id and await ctx.manager.cancel(task_id):
            gr.Info("Tarea cancelada.")
        return await show(task_id)

    outputs = [selected, detail, gallery, table, cancel_button]
    refresh_button.click(load_tasks, outputs=tasks)
    tab.select(load_tasks, outputs=tasks)
    panel.load(load_tasks, outputs=tasks)
    tasks.select(select, outputs=outputs)
    cancel_button.click(cancel_selected, inputs=selected, outputs=outputs).then(load_tasks, outputs=tasks)


# --- Registros -----------------------------------------------------------------------------------


def _logs_tab(ctx: PanelContext) -> None:
    with gr.Row():
        level = gr.Dropdown(views.LOG_LEVELS, value="TODOS", label="Nivel")
        contains = gr.Textbox(label="Contiene")
        task_id = gr.Textbox(label="Tarea (id)")
        auto = gr.Checkbox(label="Actualizar automáticamente", value=True)
    output = gr.Textbox(label="Registros", lines=20, max_lines=30, interactive=False, autoscroll=True)
    with gr.Row():
        refresh_button = gr.Button("Actualizar")
        download_button = gr.Button("Descargar filtrados")
    download = gr.File(label="Archivo de registros", interactive=False)
    timer = gr.Timer(LIVE_REFRESH_S)
    filters = [level, contains, task_id]

    def filtered(level_choice: str, text: str, task: str) -> list[dict[str, Any]]:
        return ctx.logs.records(views.level_filter(level_choice), text, task.strip() or None)

    def show(level_choice: str, text: str, task: str) -> str:
        return views.log_text(filtered(level_choice, text, task)[-MAX_LOG_LINES_SHOWN:])

    def export(level_choice: str, text: str, task: str) -> str:
        content = views.log_text(filtered(level_choice, text, task))
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".log", prefix="agent-qa-registros-", delete=False
        ) as handle:
            handle.write(content + "\n")
        return handle.name

    refresh_button.click(show, inputs=filters, outputs=output)
    timer.tick(show, inputs=filters, outputs=output)
    for component in filters:
        component.change(show, inputs=filters, outputs=output)
    auto.change(lambda on: gr.Timer(active=on), inputs=auto, outputs=timer)
    download_button.click(export, inputs=filters, outputs=download)


# --- Configuración -------------------------------------------------------------------------------


def _config_tab(ctx: PanelContext, panel: gr.Blocks) -> None:
    gr.Markdown("Los cambios se aplican a la **próxima** tarea; no requieren reiniciar el servicio.")
    with gr.Row():
        model_fast = gr.Textbox(label="Modelo rápido")
        model_strong = gr.Textbox(label="Modelo de verificación")
    with gr.Row():
        time_limit = gr.Number(label="Tiempo límite (s)", precision=0, minimum=1)
        max_turns = gr.Number(label="Máximo de turnos", precision=0, minimum=1)
        falco = gr.Checkbox(label="Falco activado")
    combos = gr.Textbox(label="Combinaciones de teclas bloqueadas (separadas por comas)")
    with gr.Row():
        apply_button = gr.Button("Aplicar", variant="primary")
        restore_button = gr.Button("Restaurar")
    fields = [model_fast, model_strong, time_limit, max_turns, falco, combos]

    def as_fields(values: RuntimeValues) -> tuple[str, str, int, int, bool, str]:
        return (values.model_fast, values.model_strong, values.time_limit_sec, values.max_turns,
                values.falco_enabled, ", ".join(values.blocked_combos))

    def load() -> tuple[str, str, int, int, bool, str]:
        return as_fields(ctx.runtime.current())

    def apply(fast: str, strong: str, limit: float | None, turns: float | None, falco_on: bool,
              combo_text: str) -> tuple[str, str, int, int, bool, str]:
        try:
            values = ctx.runtime.apply(
                model_fast=fast or "",
                model_strong=strong or "",
                time_limit_sec=int(limit or 0),
                max_turns=int(turns or 0),
                falco_enabled=bool(falco_on),
                blocked_combos=views.parse_combos(combo_text or ""),
            )
        except ValueError as exc:
            raise gr.Error(str(exc)) from None
        log.info("configuración de ejecución actualizada desde el panel")
        gr.Info("Configuración aplicada: se usará en la próxima tarea.")
        return as_fields(values)

    def restore() -> tuple[str, str, int, int, bool, str]:
        values = ctx.runtime.restore()
        log.info("configuración de ejecución restaurada a los valores de .env")
        gr.Info("Configuración restaurada a los valores de .env.")
        return as_fields(values)

    panel.load(load, outputs=fields)
    apply_button.click(apply, inputs=fields, outputs=fields)
    restore_button.click(restore, outputs=fields)

    gr.Markdown("### Prompts\nSe releen en cada llamada al modelo. Al guardar se conserva una copia `.bak`.")
    prompt_name = gr.Dropdown(list(views.PROMPT_NAMES), value=views.PROMPT_NAMES[0], label="Prompt")
    editor = gr.Code(language="markdown", lines=20, label="Contenido")
    save_button = gr.Button("Guardar", variant="primary")

    def load_prompt(name: str) -> str:
        try:
            return views.read_prompt(ctx.runtime.current().prompts_dir, name)
        except (OSError, ValueError):
            log.exception("no se pudo leer el prompt %s", name)
            raise gr.Error("No se pudo leer el prompt.") from None

    def save_prompt(name: str, text: str) -> None:
        try:
            views.save_prompt(ctx.runtime.current().prompts_dir, name, text or "")
        except ValueError as exc:
            raise gr.Error(str(exc)) from None
        except OSError:
            log.exception("no se pudo guardar el prompt %s", name)
            raise gr.Error("No se pudo guardar el prompt.") from None
        log.info("prompt %s guardado desde el panel", name)
        gr.Info(f"Prompt {name} guardado.")

    panel.load(load_prompt, inputs=prompt_name, outputs=editor)
    prompt_name.change(load_prompt, inputs=prompt_name, outputs=editor)
    save_button.click(save_prompt, inputs=[prompt_name, editor])


# --- Pruebas -------------------------------------------------------------------------------------


def _tests_tab(ctx: PanelContext, panel: gr.Blocks) -> None:
    available = [name for name in PRUEBAS_DIRS if Path(name).is_dir()] or [PRUEBAS_DIRS[0]]
    reports_dir = ctx.data_dir / "reports"
    lock = asyncio.Lock()
    with gr.Row():
        directory = gr.Dropdown(available, value=available[0], label="Carpeta de pruebas")
        ids = gr.Textbox(label="Ids (vacío = todas)", placeholder="E1,E2")
        agent_url = gr.Textbox(label="URL del agente", value=os.environ.get("AGENT_URL") or DEFAULT_AGENT_URL)
    run_button = gr.Button("Correr pruebas", variant="primary")
    totals = gr.Markdown()
    results = gr.Dataframe(headers=views.RESULT_HEADERS, label="Resultados", interactive=False)
    gr.Markdown("### Historial de reportes")
    history_button = gr.Button("Actualizar historial")
    history = gr.Dataframe(headers=views.HISTORY_HEADERS, label="data/reports", interactive=False)

    def load_history() -> list[list[str]]:
        return views.report_history(reports_dir)

    async def run(
        folder: str, id_text: str, url: str
    ) -> AsyncIterator[tuple[list[list[str]], str, list[list[str]]]]:
        if folder not in PRUEBAS_DIRS:
            raise gr.Error("Carpeta de pruebas no permitida.")
        if lock.locked():
            raise gr.Error("Ya hay una ejecución de pruebas en curso.")
        import httpx

        from dev_tools import run_tests

        env = {
            "DESKTOP_URL": os.environ.get("DESKTOP_URL", ""),
            "DESKTOP_TOKEN": os.environ.get("DESKTOP_TOKEN", ""),
            "DESKTOP_PASSWORD": os.environ.get("DESKTOP_PASSWORD", ""),
            "AGENT_URL": (url or DEFAULT_AGENT_URL).rstrip("/"),
            "TEAM_TOKEN": os.environ.get("TEAM_TOKEN", ""),
        }
        for secret in run_tests.secrets(env):
            register_secret(secret)
        missing = [name for name in ("DESKTOP_URL", "DESKTOP_TOKEN", "DESKTOP_PASSWORD") if not env[name]]
        if missing:
            raise gr.Error(f"Faltan variables: {', '.join(missing)}")
        pruebas = run_tests.load_pruebas(Path(folder), views.parse_ids(id_text or ""))
        if not pruebas:
            raise gr.Error("No hay pruebas que coincidan.")
        done: list[dict[str, Any]] = []
        async with lock:
            async with httpx.AsyncClient(timeout=30.0) as client:
                for prueba in pruebas:
                    yield views.result_rows(done), f"Ejecutando {prueba.get('id')}…", load_history()
                    try:
                        report = await run_tests.run_prueba(client, prueba, env)
                    except httpx.HTTPError as exc:
                        log.error("%s: error HTTP (%s)", prueba.get("id"), type(exc).__name__)
                        report = {"id": prueba.get("id"), "estado": "error",
                                  "estado_esperado": prueba.get("estado_esperado"),
                                  "checks": {"http": False}, "pass": False, "segundos": 0.0}
                    done.append(report)
            summary = run_tests.summarize(done)
            path = reports_dir / f"{datetime.now():%Y-%m-%d_%H%M%S}.json"
            run_tests.write_report(path, done, summary)
        log.info("pruebas terminadas: %d de %d", summary["aciertos"], summary["total"])
        text = (f"**Aciertos:** {summary['aciertos']} de {summary['total']} · "
                f"**Falsos éxitos:** {summary['falsos_exitos']} · **Tiempo medio:** {summary['tiempo_medio']} s  \n"
                f"Reporte: `{path.name}`. Recuerde reiniciar el escritorio con `POST /__reset`.")
        yield views.result_rows(done), text, load_history()

    panel.load(load_history, outputs=history)
    history_button.click(load_history, outputs=history)
    run_button.click(run, inputs=[directory, ids, agent_url], outputs=[results, totals, history])


# --- Memoria -------------------------------------------------------------------------------------


def _memory_tab(ctx: PanelContext, tab: gr.Tab) -> None:
    store = ctx.procedures
    assert store is not None
    loaded = gr.State([])
    selected = gr.State(None)
    gr.Markdown("Procedimientos aprendidos (solo lectura). Seleccione una fila para eliminarla.")
    refresh_button = gr.Button("Actualizar")
    table = gr.Dataframe(headers=views.PROCEDURE_HEADERS, label="Procedimientos", interactive=False, wrap=True)
    info = gr.Markdown()
    delete_button = gr.Button("Eliminar seleccionado", variant="stop")

    async def load() -> tuple[list[Procedure], list[list[Any]], None, str]:
        procedures = await store.list_procedures()
        return procedures, views.procedure_rows(procedures), None, ""

    def select(procedures: list[Procedure], evt: gr.SelectData) -> tuple[int | None, str]:
        index = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
        if not isinstance(index, int) or not 0 <= index < len(procedures):
            return None, ""
        procedure = procedures[index]
        return index, f"Seleccionado: `{redact(procedure.intent)}` ({procedure.exitos} éxitos)"

    async def delete(procedures: list[Procedure], index: int | None) -> tuple[list[Procedure], list[list[Any]], None, str]:
        if index is None or not 0 <= index < len(procedures):
            raise gr.Error("Seleccione un procedimiento.")
        procedure = procedures[index]
        if await store.delete_procedure(procedure.app, procedure.intent, procedure.steps):
            log.info("procedimiento eliminado desde el panel")
            gr.Info("Procedimiento eliminado.")
        return await load()

    outputs = [loaded, table, selected, info]
    refresh_button.click(load, outputs=outputs)
    tab.select(load, outputs=outputs)
    table.select(select, inputs=loaded, outputs=[selected, info])
    delete_button.click(delete, inputs=[loaded, selected], outputs=outputs)
