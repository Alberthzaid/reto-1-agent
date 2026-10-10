import ast
import dataclasses
import inspect
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image

from agent.domain import models
from agent.domain.models import (
    Action,
    Classification,
    Commit,
    Decision,
    Observation,
    Procedure,
    Task,
    TaskStatus,
    TextBox,
)
from agent.ports.interfaces import Classifier, Memory, Model, Reader, Screen

ROOT = Path(__file__).resolve().parent.parent
ALL_MODELS = [Task, Observation, Action, Commit, Procedure, Decision, Classification, TextBox]


def test_text_box_center() -> None:
    assert TextBox(text="ok", x=10, y=20, w=100, h=40).center == (60, 40)


def test_text_box_center_triangulated() -> None:
    assert TextBox(text="a", x=0, y=0, w=5, h=3).center == (2, 1)


def test_task_status_values() -> None:
    assert [s.value for s in TaskStatus] == ["pendiente", "en_curso", "terminada", "fallida", "cancelada"]
    assert TaskStatus.terminada == "terminada"


def test_task_has_estado_resumen_and_no_password() -> None:
    names = {f.name for f in dataclasses.fields(Task)}
    assert {"estado", "resumen"} <= names
    assert not any("password" in n or "secret" in n for n in names)


@pytest.mark.parametrize("cls", ALL_MODELS)
def test_models_are_frozen_dataclasses_with_slots(cls: type) -> None:
    assert dataclasses.is_dataclass(cls)
    assert hasattr(cls, "__slots__")
    assert cls.__dataclass_params__.frozen  # type: ignore[attr-defined]


def test_model_instance_is_immutable() -> None:
    box = TextBox(text="a", x=1, y=2, w=3, h=4)
    with pytest.raises(dataclasses.FrozenInstanceError):
        box.x = 5  # type: ignore[misc]


def test_task_defaults() -> None:
    t = Task(id="1", instruction="abrir", desktop_url="wss://h/websockify")
    assert t.estado is TaskStatus.pendiente
    assert t.resumen == ""
    assert t.created_at.tzinfo is not None
    assert t.created_at.utcoffset() == timedelta(0)


def test_task_field_names() -> None:
    assert [f.name for f in dataclasses.fields(Task)] == [
        "id", "instruction", "desktop_url", "estado", "resumen", "created_at",
    ]


def test_text_box_field_names() -> None:
    assert [f.name for f in dataclasses.fields(TextBox)] == ["text", "x", "y", "w", "h", "conf"]
    assert TextBox(text="a", x=0, y=0, w=1, h=1, conf=0.5).conf == 0.5


@pytest.mark.parametrize("module", [models, inspect.getmodule(Screen)])
def test_domain_and_ports_do_not_import_outer_layers(module: object) -> None:
    tree = ast.parse(Path(inspect.getsourcefile(module)).read_text(encoding="utf-8"))  # type: ignore[arg-type]
    forbidden = {"agent.infra", "agent.api", "agent.application"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not any(node.module.startswith(f) for f in forbidden)
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith(f) for a in node.names for f in forbidden)


def test_screen_signature() -> None:
    sig = inspect.signature(Screen.click)
    assert list(sig.parameters) == ["self", "x", "y", "button", "double"]
    assert sig.parameters["button"].default == "left"
    assert sig.parameters["double"].default is False
    assert list(inspect.signature(Screen.scroll).parameters) == ["self", "x", "y", "steps"]
    assert list(inspect.signature(Screen.connect).parameters) == ["self", "url", "password"]
    assert list(inspect.signature(Screen.screenshot).parameters) == ["self"]
    assert list(inspect.signature(Screen.type_text).parameters) == ["self", "text"]
    assert list(inspect.signature(Screen.key).parameters) == ["self", "combo"]
    assert list(inspect.signature(Screen.last_change_ts).parameters) == ["self"]
    assert list(inspect.signature(Screen.close).parameters) == ["self"]
    methods = {n for n, v in vars(Screen).items() if inspect.isfunction(v) and not n.startswith("_")}
    assert methods == {
        "connect", "screenshot", "click", "type_text", "key", "scroll", "last_change_ts", "close",
    }


class FakeScreen:
    async def connect(self, url: str, password: str) -> None: ...
    async def screenshot(self) -> Image.Image:
        return Image.new("RGB", (1280, 800))
    async def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None: ...
    async def type_text(self, text: str) -> None: ...
    async def key(self, combo: str) -> None: ...
    async def scroll(self, x: int, y: int, steps: int) -> None: ...
    def last_change_ts(self) -> float:
        return 0.0
    async def close(self) -> None: ...


class FakeNoClassifier:
    async def classify(self, instruction: str) -> Classification | None:
        return None


def test_structural_conformance_without_inheritance() -> None:
    assert isinstance(FakeScreen(), Screen)
    assert not isinstance(object(), Screen)
    assert isinstance(FakeNoClassifier(), Classifier)


@pytest.mark.asyncio
async def test_classifier_may_return_none() -> None:
    assert await FakeNoClassifier().classify("x") is None


@pytest.mark.parametrize(
    "proto,members",
    [
        (Reader, {"read"}),
        (Model, {"decide"}),
        (Memory, {"fingerprint", "procedures", "save_procedure", "save_task", "load_task",
                  "mark_stale_running_as_failed"}),
        (Classifier, {"classify"}),
        (Screen, {"connect", "screenshot", "click", "type_text", "key", "scroll", "last_change_ts", "close"}),
    ],
)
def test_port_members(proto: type, members: set[str]) -> None:
    public = {n for n in vars(proto) if not n.startswith("_")}
    assert members == public
