import pytest

from agent.domain.guard import Guard
from agent.domain.models import TextBox

BLOCKED = {"F5", "ctrl+r", "ctrl+w", "ctrl+q", "alt+F4", "ctrl+l", "ctrl+t", "ctrl+n", "F11", "F12"}
GUARDAR = TextBox(text="Guardar", x=600, y=700, w=80, h=20)
BUSCAR = TextBox(text="Buscar", x=100, y=100, w=60, h=20)
SI = TextBox(text="Sí", x=500, y=400, w=30, h=20)
ACEPTAR = TextBox(text="Aceptar", x=700, y=400, w=60, h=20)
BOXES = [GUARDAR, BUSCAR, SI, ACEPTAR]


def click_center(guard: Guard, box: TextBox, boxes: list[TextBox] = BOXES):  # type: ignore[no-untyped-def]
    return guard.check_click(*box.center, boxes)


def test_non_commit_click_is_allowed() -> None:
    verdict = click_center(Guard(BLOCKED, planned_commits=0), BUSCAR)
    assert verdict.allowed and not verdict.is_commit
    assert verdict.label == "Buscar"


def test_commit_detected_and_allowed_within_plan() -> None:
    guard = Guard(BLOCKED, planned_commits=1)
    verdict = click_center(guard, GUARDAR)
    assert verdict.allowed and verdict.is_commit


def test_second_guardar_blocked_with_one_planned_commit() -> None:
    guard = Guard(BLOCKED, planned_commits=1)
    guard.record_commit("Guardar", changed=True)
    guard.mark_verified()
    verdict = click_center(guard, GUARDAR)
    assert not verdict.allowed
    assert verdict.is_commit
    assert "Guardar" in verdict.reason
    assert guard.commits == ("Guardar",)


def test_commit_blocked_when_plan_has_no_commits() -> None:
    verdict = click_center(Guard(BLOCKED, planned_commits=0), GUARDAR)
    assert not verdict.allowed


def test_repeat_commit_after_change_without_verification_is_blocked() -> None:
    guard = Guard(BLOCKED, planned_commits=2)
    guard.record_commit("Guardar", changed=True)
    assert not click_center(guard, GUARDAR).allowed
    guard.mark_verified()
    assert click_center(guard, GUARDAR).allowed


def test_commit_that_changed_nothing_does_not_count() -> None:
    guard = Guard(BLOCKED, planned_commits=1)
    guard.record_commit("Guardar", changed=False)
    assert guard.commits == ()
    assert click_center(guard, GUARDAR).allowed


def test_confirmation_dialog_right_after_commit_is_part_of_it() -> None:
    guard = Guard(BLOCKED, planned_commits=1)
    guard.record_commit("Guardar", changed=True)
    verdict = click_center(guard, SI)
    assert verdict.allowed and verdict.is_commit
    guard.record_commit(verdict.label, changed=True)
    assert guard.commits == ("Guardar",)
    # A second confirmation is a new commit and exceeds the plan.
    guard.mark_verified()
    assert not click_center(guard, ACEPTAR).allowed


@pytest.mark.parametrize("label", ["Sí", "SI", "si", "¿Desea continuar? Sí"])
def test_si_matches_as_whole_word(label: str) -> None:
    box = TextBox(text=label, x=10, y=10, w=200, h=20)
    assert Guard(BLOCKED, 1).check_click(20, 15, [box]).is_commit


@pytest.mark.parametrize("label", ["Sitio web", "Análisis", "Casilla", "Registros", "Creado"])
def test_commit_words_do_not_match_inside_other_words(label: str) -> None:
    box = TextBox(text=label, x=10, y=10, w=200, h=20)
    assert not Guard(BLOCKED, 1).check_click(20, 15, [box]).is_commit


def test_accents_and_case_are_ignored() -> None:
    box = TextBox(text="ELIMINAR registro", x=10, y=10, w=200, h=20)
    assert Guard(BLOCKED, 1).check_click(20, 15, [box]).is_commit


def test_nearest_label_within_radius() -> None:
    guard = Guard(BLOCKED, 1)
    verdict = guard.check_click(GUARDAR.x + GUARDAR.w + 20, GUARDAR.y + 5, BOXES)
    assert verdict.is_commit and verdict.label == "Guardar"
    far = guard.check_click(GUARDAR.x + GUARDAR.w + 200, GUARDAR.y + 5, BOXES)
    assert not far.is_commit and far.label == ""


@pytest.mark.parametrize("combo", ["ctrl+w", "CTRL+W", "control+w", "alt+F4", "alt+f4", "F5", "ctrl+q"])
def test_blocked_key_combos(combo: str) -> None:
    verdict = Guard(BLOCKED, 0).check_key(combo)
    assert not verdict.allowed
    assert verdict.reason


def test_modifier_order_does_not_bypass_block() -> None:
    guard = Guard({"ctrl+shift+q"}, 0)
    assert not guard.check_key("shift+ctrl+q").allowed


@pytest.mark.parametrize("combo", ["ctrl+a", "enter", "tab", "escape"])
def test_allowed_key_combos(combo: str) -> None:
    assert Guard(BLOCKED, 0).check_key(combo).allowed


def test_planned_commits_can_be_updated_after_planning() -> None:
    guard = Guard(BLOCKED, 0)
    guard.planned_commits = 1
    assert click_center(guard, GUARDAR).allowed
