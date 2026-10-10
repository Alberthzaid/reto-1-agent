import sqlite3
from pathlib import Path

import pytest

from agent.domain.models import Procedure, TextBox
from agent.infra.sqlite_memory import SqliteMemory
from agent.ports.interfaces import Memory

APP = "abc123"
INTENT = "crear:cliente"


def header(menu: list[str], date_text: str = "Hoy: 14/10/2026") -> list[TextBox]:
    boxes = [TextBox(text="SIGECOM", x=10, y=10, w=100, h=20), TextBox(text=date_text, x=1000, y=10, w=150, h=20)]
    boxes += [TextBox(text=item, x=20, y=120 + 30 * i, w=120, h=20) for i, item in enumerate(menu)]
    return boxes


def body(*texts: str) -> list[TextBox]:
    return [TextBox(text=text, x=400, y=300 + 30 * i, w=200, h=20) for i, text in enumerate(texts)]


@pytest.fixture
def memory(tmp_path: Path) -> SqliteMemory:
    return SqliteMemory(tmp_path / "agent.db")


def test_satisfies_the_memory_port(memory: SqliteMemory) -> None:
    assert isinstance(memory, Memory)


def test_fingerprint_ignores_body_and_header_date(memory: SqliteMemory) -> None:
    menu = ["Clientes", "Facturas", "Inventario"]
    first = memory.fingerprint(header(menu, "Hoy: 14/10/2026") + body("Papelería Los Andes", "NIT 901234567"))
    second = memory.fingerprint(header(menu, "Hoy: 15/10/2026") + body("Ferretería El Sol", "Total 45.000"))
    assert first == second
    assert len(first) == 16


def test_fingerprint_without_header_or_menu_words_is_empty(memory: SqliteMemory) -> None:
    # A blank or half-received first frame must not put every app under the same key.
    assert memory.fingerprint([]) == ""
    assert memory.fingerprint(body("Papelería Los Andes", "901234567")) == ""
    assert memory.fingerprint([TextBox(text="14/10/2026", x=1000, y=10, w=100, h=20)]) == ""


def test_fingerprint_differs_between_apps(memory: SqliteMemory) -> None:
    assert memory.fingerprint(header(["Clientes", "Facturas"])) != memory.fingerprint(header(["Pacientes", "Citas"]))


def test_fingerprint_is_accent_and_case_insensitive(memory: SqliteMemory) -> None:
    assert memory.fingerprint(header(["Facturación"])) == memory.fingerprint(header(["FACTURACION"]))


@pytest.mark.asyncio
async def test_save_upserts_and_counts_successes(memory: SqliteMemory) -> None:
    steps = ("click 'Clientes'", "click 'Nuevo'", "click 'Guardar'")
    await memory.save_procedure(APP, Procedure(APP, INTENT, steps, "primero"))
    await memory.save_procedure(APP, Procedure(APP, INTENT, steps, "segundo"))
    procs = await memory.procedures(APP, INTENT)
    assert len(procs) == 1
    assert procs[0].exitos == 2 and procs[0].aprendizaje == "segundo" and procs[0].steps == steps


@pytest.mark.asyncio
async def test_procedures_returns_top_three_by_successes(memory: SqliteMemory) -> None:
    for i, count in enumerate([1, 3, 2, 4]):
        proc = Procedure(APP, INTENT, (f"click 'Menu{'x' * i}'",))
        for _ in range(count):
            await memory.save_procedure(APP, proc)
    await memory.save_procedure(APP, Procedure(APP, "eliminar:cliente", ("click 'Otro'",)))
    procs = await memory.procedures(APP, INTENT)
    assert [p.exitos for p in procs] == [4, 3, 2]
    assert all(p.intent == INTENT and p.app == APP for p in procs)
    assert await memory.procedures("other-app", INTENT) == []


@pytest.mark.asyncio
async def test_penalize_decrements_and_hides_exhausted(memory: SqliteMemory) -> None:
    good = Procedure(APP, INTENT, ("click 'Clientes'",))
    weak = Procedure(APP, INTENT, ("click 'Ventas'",))
    await memory.save_procedure(APP, good)
    await memory.save_procedure(APP, good)
    await memory.save_procedure(APP, weak)
    await memory.penalize(APP, INTENT, await memory.procedures(APP, INTENT))
    procs = await memory.procedures(APP, INTENT)
    assert [(p.steps, p.exitos) for p in procs] == [(("click 'Clientes'",), 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "procedure",
    [
        Procedure(APP, INTENT, ("click 'NIT 901234567'",)),
        Procedure(APP, INTENT, ("click 'Clientes'",), "Escribí el NIT 901234567"),
        Procedure(APP, INTENT, ("click 'ventas@andes.co'",)),
        Procedure(APP, INTENT, ("click 'Clientes'",), "avisar a ventas@andes.co"),
    ],
)
async def test_rejects_digits_and_emails(memory: SqliteMemory, procedure: Procedure, tmp_path: Path) -> None:
    await memory.save_procedure(APP, procedure)
    with sqlite3.connect(tmp_path / "agent.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM procedures").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_tasks_table_still_works_alongside_procedures(memory: SqliteMemory) -> None:
    assert await memory.load_task("missing") is None
    assert await memory.procedures(APP, INTENT) == []


# --- FR-0153: procedures remember the app version they were learned on -----------------------------


@pytest.mark.asyncio
async def test_procedures_keep_their_app_version(memory: SqliteMemory) -> None:
    await memory.save_procedure(APP, Procedure(APP, INTENT, ("click 'Crear cliente'",), app_version="4.2"))
    await memory.save_procedure(APP, Procedure(APP, INTENT, ("click 'Nuevo cliente'",)))
    versions = {p.steps[0]: p.app_version for p in await memory.procedures(APP, INTENT)}
    assert versions == {"click 'Crear cliente'": "4.2", "click 'Nuevo cliente'": None}


@pytest.mark.asyncio
async def test_same_steps_on_a_new_version_restart_their_count(memory: SqliteMemory) -> None:
    steps = ("click 'Clientes'",)
    for _ in range(3):
        await memory.save_procedure(APP, Procedure(APP, INTENT, steps, app_version="3.9"))
    await memory.save_procedure(APP, Procedure(APP, INTENT, steps, app_version="4.2"))
    [procedure] = await memory.procedures(APP, INTENT)
    assert (procedure.app_version, procedure.exitos) == ("4.2", 1)


@pytest.mark.asyncio
async def test_old_database_without_version_column_is_migrated(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE procedures (app TEXT NOT NULL, intent TEXT NOT NULL, steps TEXT NOT NULL, "
            "aprendizaje TEXT NOT NULL, exitos INTEGER NOT NULL, updated_at TEXT NOT NULL, "
            "PRIMARY KEY (app, intent, steps))"
        )
        connection.execute(
            "INSERT INTO procedures VALUES (?, ?, ?, ?, ?, ?)",
            (APP, INTENT, '["click \'Guardar\'"]', "Guardar abajo.", 2, "2026-10-01T00:00:00+00:00"),
        )
    memory = SqliteMemory(db)
    SqliteMemory(db)  # opening twice must not fail
    [legacy] = await memory.procedures(APP, INTENT)
    assert (legacy.steps, legacy.exitos, legacy.app_version) == (("click 'Guardar'",), 2, None)
    await memory.save_procedure(APP, Procedure(APP, INTENT, ("click 'Aplicar'",), app_version="4.2"))
    assert {p.app_version for p in await memory.procedures(APP, INTENT)} == {None, "4.2"}
