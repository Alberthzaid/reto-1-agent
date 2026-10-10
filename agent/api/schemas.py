"""Request and response models for the task HTTP contract."""

from pydantic import BaseModel, SecretStr, field_validator

from agent.domain.models import TaskStatus


class Escritorio(BaseModel):
    url: str
    password: SecretStr

    @field_validator("url")
    @classmethod
    def validate_websocket_url(cls, value: str) -> str:
        if not value.startswith(("ws://", "wss://")):
            raise ValueError("url debe empezar por ws:// o wss://")
        return value


class TareaIn(BaseModel):
    tarea: str
    escritorio: Escritorio


class TareaCreada(BaseModel):
    task_id: str


class EstadoTarea(BaseModel):
    estado: TaskStatus
    resumen: str