"""FastAPI application factory for task submission and status retrieval."""

from contextlib import asynccontextmanager
from collections.abc import Callable, Sequence
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, status

from agent.api.auth import team_auth
from agent.api.schemas import EstadoTarea, TareaCreada, TareaIn
from agent.application.manager import TaskManager


def create_app(
    manager: TaskManager,
    team_token: str | None,
    startup_services: Sequence[Callable[[], object]] = (),
    shutdown_services: Sequence[Callable[[], object]] = (),
) -> FastAPI:
    """Create the HTTP application around an already configured manager."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await manager.start()
        for service in startup_services:
            service()
        try:
            yield
        finally:
            for service in shutdown_services:
                result = service()
                if hasattr(result, "__await__"):
                    await result  # type: ignore[misc]
            await manager.stop()

    auth = team_auth(team_token)
    app = FastAPI(title="Agent-QA", lifespan=lifespan)

    @app.get("/health")  # public liveness probe for the delivery validator
    async def health() -> dict[str, str]:
        if not manager.ready:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="No listo")
        return {"status": "ok"}

    @app.post("/tasks", response_model=TareaCreada, dependencies=[Depends(auth)])
    async def create_task(payload: TareaIn) -> TareaCreada:
        task_id = manager.create(
            payload.tarea,
            payload.escritorio.url,
            payload.escritorio.password.get_secret_value(),
        )
        return TareaCreada(task_id=task_id)

    @app.get("/tasks/{task_id}", response_model=EstadoTarea, dependencies=[Depends(auth)])
    async def get_task(task_id: str) -> EstadoTarea:
        task = await manager.get(task_id)
        if task is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tarea no encontrada")
        return EstadoTarea(estado=task.estado, resumen=task.resumen)

    return app