from agent.domain.models import Plan, Procedure
from agent.domain.procedures import describe_hints, intent_key, sanitize_steps, sanitize_text

PLAN = Plan(
    kind="crear",
    entity="Cliente",
    target="Papelería Los Andes",
    values=(("nombre", "Papelería Los Andes"), ("nit", "901234567"), ("correo", "ventas@andes.co")),
)
SECRETS = ["redaction-fixture-value", "wss://desktop.example.test/websockify", "desktop.example.test"]


def test_intent_key_normalizes_entity() -> None:
    assert intent_key(PLAN) == "crear:cliente"
    assert intent_key(Plan(kind="modificar", entity="  Artículo ")) == "modificar:articulo"
    assert intent_key(Plan(kind="otro")) == "otro:"


def test_sanitize_steps_drops_steps_with_task_data() -> None:
    steps = [
        "click 'Clientes'",
        "click 'Papelería Los Andes'",
        "click 'Papeleria'",
        "click 'NIT 901234567'",
        "click 'ventas@andes.co'",
        "click 'Fila 12'",
        "type into a field",
        "click 'desktop.example.test'",
        "click 'Guardar'",
    ]
    assert sanitize_steps(steps, PLAN, SECRETS) == ("click 'Clientes'", "type into a field", "click 'Guardar'")


def test_sanitize_steps_keeps_neutral_counts() -> None:
    assert sanitize_steps(["fill form (3 fields)"], PLAN, SECRETS) == ("fill form (3 fields)",)


def test_sanitize_text_masks_task_data_secrets_digits_and_emails() -> None:
    text = (
        "Menú Clientes > Nuevo. Escribí Papelería Los Andes (papeleria) y NIT 901234567, correo "
        "ventas@andes.co, clave redaction-fixture-value en desktop.example.test, luego Guardar."
    )
    clean = sanitize_text(text, PLAN, SECRETS)
    for leaked in ("Papelería", "Papeleria", "papeleria", "Andes", "901234567", "ventas@", "redaction-fixture-value",
                   "desktop.example.test"):
        assert leaked not in clean
    assert "Clientes" in clean and "Guardar" in clean


def test_describe_hints_lists_steps_and_learning() -> None:
    procs = [Procedure(app="a", intent="crear:cliente", steps=("click 'Clientes'", "click 'Nuevo'"),
                       aprendizaje="El botón Nuevo está arriba a la derecha.", exitos=2)]
    text = describe_hints(procs)
    assert "Hints from previous tasks" in text and "verify" in text
    assert "click 'Clientes' -> click 'Nuevo'" in text
    assert "El botón Nuevo está arriba a la derecha." in text


def test_describe_hints_empty() -> None:
    assert describe_hints([]) == ""
