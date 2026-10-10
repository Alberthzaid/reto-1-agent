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
    guard.record_commit("Guardar")
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
    guard.record_commit("Guardar")
    assert not click_center(guard, GUARDAR).allowed
    guard.mark_verified()
    assert click_center(guard, GUARDAR).allowed


def test_commit_counts_on_click_even_if_the_screen_did_not_change_yet() -> None:
    guard = Guard(BLOCKED, planned_commits=1)
    guard.record_commit("Guardar")
    assert guard.commits == ("Guardar",)
    guard.mark_verified()
    verdict = click_center(guard, GUARDAR)
    assert not verdict.allowed
    # The refusal tells the model to look instead of clicking again.
    assert "título" in verdict.reason and "mensaje" in verdict.reason


def test_confirmation_dialog_right_after_commit_is_part_of_it() -> None:
    guard = Guard(BLOCKED, planned_commits=1)
    guard.record_commit("Guardar")
    verdict = click_center(guard, SI)
    assert verdict.allowed and verdict.is_commit
    guard.record_commit(verdict.label)
    assert guard.commits == ("Guardar",)
    # A second confirmation is a new commit and exceeds the plan.
    guard.mark_verified()
    assert not click_center(guard, ACEPTAR).allowed


@pytest.mark.parametrize("label", ["Sí", "SI", "si", "¿Desea continuar? Sí"])
def test_si_matches_as_whole_word(label: str) -> None:
    box = TextBox(text=label, x=10, y=10, w=200, h=20)
    guard = Guard(BLOCKED, 1)
    guard.note_data_entry()
    assert guard.check_click(20, 15, [box]).is_commit


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


# --- FR-0153: dialogs, opening vs saving, assigned codes -------------------------------------------


def box(text: str) -> TextBox:
    return TextBox(text=text, x=10, y=10, w=120, h=20)


def click(guard: Guard, text: str, visible_codes: tuple[str, ...] = ()):  # type: ignore[no-untyped-def]
    return guard.check_click(20, 15, [box(text)], visible_codes=visible_codes)


@pytest.mark.parametrize("label", ["Entendido", "Cerrar", "ENTENDIDO"])
def test_dismissal_words_are_not_commits(label: str) -> None:
    verdict = click(Guard(BLOCKED, 0), label)
    assert verdict.allowed and not verdict.is_commit and verdict.dismissal


@pytest.mark.parametrize("label", ["Aceptar", "Sí", "Confirmar"])
def test_confirm_word_without_commit_or_data_is_a_dismissal(label: str) -> None:
    guard = Guard(BLOCKED, 0)
    verdict = click(guard, label)
    assert verdict.allowed and not verdict.is_commit and verdict.dismissal
    assert guard.commits == ()


@pytest.mark.parametrize("label", ["Aceptar", "Confirmar"])
def test_confirm_word_after_data_entry_is_a_commit(label: str) -> None:
    guard = Guard(BLOCKED, 0)
    guard.note_data_entry()
    verdict = click(guard, label)
    assert verdict.is_commit and not verdict.allowed


def test_confirm_word_right_after_a_commit_is_part_of_it() -> None:
    guard = Guard(BLOCKED, 1)
    guard.note_data_entry()
    guard.record_commit("Eliminar")
    verdict = click(guard, "Aceptar")
    assert verdict.allowed and verdict.is_commit
    guard.record_commit(verdict.label)
    assert guard.commits == ("Eliminar",)


def test_merged_dismiss_and_commit_label_is_treated_as_commit() -> None:
    verdict = click(Guard(BLOCKED, 0), "Cerrar | Aplicar")
    assert verdict.is_commit and not verdict.allowed


@pytest.mark.parametrize("label", ["Crear cliente", "Nuevo cliente", "Registrar"])
def test_opening_a_form_on_the_list_toolbar_is_navigation(label: str) -> None:
    guard = Guard(BLOCKED, 1)
    verdict = click(guard, label)
    assert verdict.allowed and not verdict.is_commit
    assert guard.commits == ()


def test_crear_after_filling_the_form_is_a_commit() -> None:
    guard = Guard(BLOCKED, 1)
    guard.note_data_entry()
    verdict = click(guard, "Crear")
    assert verdict.allowed and verdict.is_commit and verdict.weak


def test_navigation_forgets_the_data_entered() -> None:
    guard = Guard(BLOCKED, 1)
    guard.note_data_entry()
    guard.note_navigation()
    assert not click(guard, "Crear cliente").is_commit


@pytest.mark.parametrize(
    "label", ["Guardar", "Grabar", "Aplicar", "Eliminar", "Borrar", "Anular", "Enviar", "Exportar", "Actualizar"]
)
def test_strong_commit_words_count_without_data_entry(label: str) -> None:
    verdict = click(Guard(BLOCKED, 0), label)
    assert verdict.is_commit and not verdict.allowed


def test_second_apply_on_a_form_that_shows_its_assigned_code_is_blocked() -> None:
    guard = Guard(BLOCKED, 2)
    guard.note_data_entry()
    guard.record_commit("Aplicar")
    guard.note_saved_code("CL-0141")
    guard.mark_verified()
    verdict = click(guard, "Aplicar", visible_codes=("CL-0141",))
    assert not verdict.allowed and verdict.is_commit
    assert "CL-0141" in verdict.reason
    # Away from that form (code no longer visible) the second planned commit is allowed.
    assert click(guard, "Aplicar").allowed


def test_editing_the_saved_form_allows_another_planned_commit() -> None:
    guard = Guard(BLOCKED, 2)
    guard.note_data_entry()
    guard.record_commit("Aplicar")
    guard.note_saved_code("CL-0141")
    guard.mark_verified()
    guard.note_data_entry()
    assert click(guard, "Aplicar", visible_codes=("CL-0141",)).allowed


def test_assigned_codes_are_found_in_text() -> None:
    from agent.domain.guard import assigned_codes

    assert assigned_codes("Cliente CL-0141") == {"CL-0141"}
    assert assigned_codes("Factura FAC-001234 y PR-12") == {"FAC-001234"}
    assert assigned_codes("Crear cliente") == set()


def test_check_commit_applies_the_commit_rules_to_a_known_button() -> None:
    guard = Guard(BLOCKED, 1)
    assert guard.check_commit("aviso de éxito").allowed
    guard.record_commit("aviso de éxito")
    guard.mark_verified()
    verdict = guard.check_commit("aviso de éxito")
    assert not verdict.allowed and verdict.is_commit
