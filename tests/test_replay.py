"""ScriptRunner replays a learned script without calling any model, step by step, under the guard."""

import pytest

from agent.domain.guard import Guard
from agent.domain.models import Plan, Procedure, ScriptStep, TextBox
from agent.domain.replay import MAX_REPLAY_FAILURES, ScriptRunner, replayable
from agent.domain.tools import ToolBox
from tests.fakes import FakeClock, FakeScreen, FakeSettler, FakeTrace, TypingReader

CLIENTES = TextBox(text="Clientes", x=100, y=100, w=60, h=20)
NIT_LABEL = TextBox(text="NIT *", x=240, y=270, w=40, h=14)
NOMBRE_LABEL = TextBox(text="Nombre *", x=240, y=330, w=60, h=14)
GUARDAR = TextBox(text="Guardar", x=600, y=700, w=80, h=20)
ENTENDIDO = TextBox(text="Entendido", x=600, y=500, w=80, h=20)
VALUES = (("nombre", "Papelería Los Andes"), ("nit", "901234567"))
PLAN = Plan(kind="crear", entity="cliente", target="Papelería Los Andes", values=VALUES, planned_commits=1)
SCRIPT = (
    ScriptStep(kind="click", anchor="Entendido", x=640, y=510, dismissal=True),
    ScriptStep(kind="click", anchor="Clientes", x=130, y=110),
    ScriptStep(kind="fill", field_label="NIT *", field="nit", dx=140, dy=23),
    ScriptStep(kind="fill", field_label="Nombre *", field="nombre", dx=140, dy=23),
    ScriptStep(kind="key", anchor="tab"),
    ScriptStep(kind="click", anchor="Guardar", x=640, y=710, commit=True),
)


class World:
    def __init__(self, boxes: list[TextBox], planned_commits: int = 1, budget: float = 200.0) -> None:
        self.screen = FakeScreen()
        self.reader = TypingReader(self.screen, boxes)
        self.clock = FakeClock()
        self.guard = Guard({"alt+F4"}, planned_commits)
        self.trace = FakeTrace()
        self.toolbox = ToolBox(self.screen, self.reader, FakeSettler(self.screen), self.guard, self.clock,
                               trace=self.trace, deadline=self.clock.now + budget, sleep=self.clock.sleep,
                               plan_values=VALUES)
        self.toolbox.prime(self.screen.image, list(boxes))
        self.runner = ScriptRunner(self.toolbox, self.guard, self.clock, self.clock.now + budget)

    @property
    def clicks(self) -> list[tuple[int, int]]:
        return [(a[1], a[2]) for a in self.screen.actions if a[0] == "click"]


@pytest.mark.asyncio
async def test_replays_every_step_and_skips_an_absent_dismissal() -> None:
    world = World([CLIENTES, NIT_LABEL, NOMBRE_LABEL, GUARDAR])
    outcome = await world.runner.run(SCRIPT, PLAN)
    assert outcome.completed, outcome.reason
    assert outcome.steps_done == len(SCRIPT)
    typed = [a[1] for a in world.screen.actions if a[0] == "type_text"]
    assert typed == ["901234567", "Papelería Los Andes"]
    assert (130, 110) in world.clicks and (640, 710) in world.clicks and (640, 510) not in world.clicks
    assert ("key", "tab") in world.screen.actions
    assert world.guard.commits == ("Guardar",)
    # The skipped dismissal is kept so the new recording can still close the modal next time.
    assert [s.anchor for s in world.toolbox.script if s.kind == "click"] == ["Entendido", "Clientes", "Guardar"]


@pytest.mark.asyncio
async def test_present_dismissal_is_clicked() -> None:
    world = World([ENTENDIDO, CLIENTES, NIT_LABEL, NOMBRE_LABEL, GUARDAR])
    outcome = await world.runner.run(SCRIPT, PLAN)
    assert outcome.completed
    assert world.clicks[0] == (640, 510)


@pytest.mark.asyncio
async def test_moved_button_is_clicked_at_its_new_position() -> None:
    moved = TextBox(text="Guardar", x=900, y=500, w=80, h=20)
    world = World([CLIENTES, NIT_LABEL, NOMBRE_LABEL, moved])
    outcome = await world.runner.run(SCRIPT, PLAN)
    assert outcome.completed
    assert (940, 510) in world.clicks and (640, 710) not in world.clicks


@pytest.mark.asyncio
async def test_noisy_ocr_of_the_anchor_is_accepted() -> None:
    noisy = TextBox(text="Guardar.", x=600, y=700, w=80, h=20)
    world = World([CLIENTES, NIT_LABEL, NOMBRE_LABEL, noisy])
    assert (await world.runner.run(SCRIPT, PLAN)).completed


@pytest.mark.asyncio
async def test_missing_anchor_stops_with_the_step_and_reason() -> None:
    world = World([CLIENTES, NIT_LABEL, NOMBRE_LABEL])  # no Guardar anywhere
    outcome = await world.runner.run(SCRIPT, PLAN)
    assert not outcome.completed
    assert outcome.steps_done == 5
    assert "Guardar" in outcome.reason
    assert world.guard.commits == ()


@pytest.mark.asyncio
async def test_moved_field_label_moves_the_typing_target() -> None:
    nit = TextBox(text="NIT *", x=540, y=270, w=40, h=14)
    world = World([CLIENTES, nit, NOMBRE_LABEL, GUARDAR])
    assert (await world.runner.run(SCRIPT, PLAN)).completed
    assert (700, 300) in world.clicks


@pytest.mark.asyncio
async def test_missing_field_label_stops() -> None:
    world = World([CLIENTES, NOMBRE_LABEL, GUARDAR])
    outcome = await world.runner.run(SCRIPT, PLAN)
    assert not outcome.completed and "NIT" in outcome.reason
    assert world.guard.commits == ()


@pytest.mark.asyncio
async def test_reread_mismatch_stops() -> None:
    world = World([CLIENTES, NIT_LABEL, NOMBRE_LABEL, GUARDAR])
    async def garbled(img: object, region: tuple[int, int, int, int] | None = None) -> list[TextBox]:
        if region is None:
            return list(world.reader.boxes)
        return [TextBox(text="9O1Z", x=region[0], y=region[1], w=5, h=5)]

    world.reader.read = garbled  # type: ignore[method-assign]
    outcome = await world.runner.run(SCRIPT, PLAN)
    assert not outcome.completed
    assert world.guard.commits == ()


@pytest.mark.asyncio
async def test_second_commit_during_replay_is_blocked_by_the_guard() -> None:
    script = (*SCRIPT, ScriptStep(kind="click", anchor="Guardar", x=640, y=710, commit=True))
    world = World([CLIENTES, NIT_LABEL, NOMBRE_LABEL, GUARDAR], planned_commits=1)
    outcome = await world.runner.run(script, PLAN)
    assert not outcome.completed
    assert outcome.steps_done == len(SCRIPT)
    assert "bloqueado" in outcome.reason.lower()
    assert world.clicks.count((640, 710)) == 1
    assert world.guard.commits == ("Guardar",)


@pytest.mark.asyncio
async def test_script_without_the_planned_commit_is_not_complete() -> None:
    world = World([CLIENTES, NIT_LABEL, NOMBRE_LABEL, GUARDAR])
    outcome = await world.runner.run(SCRIPT[:-1], PLAN)
    assert not outcome.completed


@pytest.mark.asyncio
async def test_deadline_stops_the_replay() -> None:
    world = World([CLIENTES, NIT_LABEL, NOMBRE_LABEL, GUARDAR], budget=1.0)
    outcome = await world.runner.run(SCRIPT, PLAN)
    assert not outcome.completed
    assert world.screen.write_actions == []


def test_replayable_requires_successes_matching_fields_and_few_failures() -> None:
    good = Procedure("app", "crear:cliente", ("x",), script=SCRIPT, exitos=1)
    assert replayable([good], PLAN, min_successes=1) is good
    assert replayable([good], PLAN, min_successes=2) is None
    assert replayable([Procedure("app", "crear:cliente", ("x",), exitos=3)], PLAN, 1) is None
    tired = Procedure("app", "crear:cliente", ("x",), script=SCRIPT, exitos=5, fallos_replay=MAX_REPLAY_FAILURES)
    assert replayable([tired], PLAN, 1) is None
    other_fields = Plan(kind="crear", entity="cliente", values=(("nombre", "A"), ("telefono", "3")))
    assert replayable([good], other_fields, 1) is None  # 'nit' is not in this plan
    more_fields = Plan(kind="crear", entity="cliente", values=(*VALUES, ("correo", "a@b.co")))
    assert replayable([good], more_fields, 1) is None  # the script would leave 'correo' empty
