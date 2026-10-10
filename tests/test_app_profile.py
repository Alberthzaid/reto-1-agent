import pytest

from agent.domain.app_profile import AppProfile, interface_change, read_profile, select_current
from agent.domain.models import Procedure, TextBox

HEADER_V42 = [
    TextBox(text="SIGECOM · Distribuidora Andina", x=12, y=10, w=248, h=14),
    TextBox(text="v4.2", x=277, y=10, w=28, h=14),
    TextBox(text="Hoy: 14/10/2026 · Usuario: operador2", x=1040, y=10, w=227, h=14),
    TextBox(text="Archivo", x=18, y=42, w=48, h=14),
    TextBox(text="Comercial", x=90, y=42, w=65, h=14),
    TextBox(text="Cartera", x=180, y=42, w=48, h=14),
    TextBox(text="Bienvenido a SIGECOM", x=14, y=76, w=205, h=17),
    TextBox(text="Novedades de la versión 4.2", x=390, y=303, w=185, h=14),
]


def proc(version: str | None, steps: tuple[str, ...] = ("click 'Guardar'",)) -> Procedure:
    return Procedure(app="app", intent="crear:cliente", steps=steps, app_version=version)


def test_reads_version_name_and_menu_from_the_header() -> None:
    profile = read_profile(HEADER_V42)
    assert profile.version == "4.2"
    assert profile.name == "SIGECOM · Distribuidora Andina"
    assert profile.labels == ("Archivo", "Comercial", "Cartera")


@pytest.mark.parametrize("text,version", [("v3.9", "3.9"), ("SIGECOM v10.2.1", "10.2.1"), ("Versión 4.2", "4.2")])
def test_version_formats(text: str, version: str) -> None:
    assert read_profile([TextBox(text=text, x=10, y=10, w=80, h=14)]).version == version


def test_version_outside_the_header_is_ignored() -> None:
    body = [TextBox(text="Novedades de la versión 4.2", x=390, y=303, w=185, h=14)]
    assert read_profile(body).version is None


def test_dates_are_not_versions() -> None:
    assert read_profile([TextBox(text="Hoy: 14/10/2026", x=1000, y=10, w=150, h=14)]).version is None


def test_select_current_drops_procedures_from_another_version() -> None:
    old = proc("3.9")
    legacy = proc(None, ("click 'Nuevo cliente'",))
    current = proc("4.2", ("click 'Crear cliente'",))
    kept, change = select_current([old, legacy, current], "4.2")
    assert kept == [current]
    assert change == "cambio de interfaz detectado: v3.9 → v4.2"


def test_legacy_procedures_without_version_are_stale_when_the_version_is_known() -> None:
    kept, change = select_current([proc(None)], "4.2")
    assert kept == []
    assert change == "cambio de interfaz detectado: versión desconocida → v4.2"


def test_unknown_current_version_keeps_everything() -> None:
    procedures = [proc("3.9"), proc(None)]
    assert select_current(procedures, None) == (procedures, None)


def test_same_version_is_not_a_change() -> None:
    assert select_current([proc("4.2")], "4.2") == ([proc("4.2")], None)


def test_interface_change_message() -> None:
    assert interface_change("3.9", "4.2") == "cambio de interfaz detectado: v3.9 → v4.2"
    assert AppProfile().version is None
