import pytest
from PIL import Image

from agent.domain.models import Plan, TextBox
from agent.domain.tools import CommitEvidence
from agent.domain.verifier import PROMPTS_DIR, Verifier, load_prompt
from tests.fakes import FakeModel, text_decision, tool_decision

PLAN = Plan(kind="crear", target="Papelería Los Andes", planned_commits=1,
            expected_evidence="La lista muestra Papelería Los Andes")
IMAGE = Image.new("RGB", (1280, 800), "white")
BOXES = [TextBox(text="Papelería Los Andes 901234567", x=100, y=200, w=300, h=20)]


@pytest.mark.asyncio
async def test_confirmed_verdict() -> None:
    model = FakeModel([tool_decision(("verdict", {"confirmed": True, "evidence": "Se ve el cliente"}))])
    ok, evidence = await Verifier(model).confirm(PLAN, "Registra al cliente", IMAGE, BOXES, claim="Cliente creado")
    assert ok is True
    assert evidence == "Se ve el cliente"
    request = model.requests[0]
    assert len(request["messages"]) == 1, "the verifier starts a fresh conversation"
    content = request["messages"][0]["content"]
    assert any(block["type"] == "image" for block in content)
    text = content[0]["text"]
    assert "Registra al cliente" in text and "La lista muestra Papelería Los Andes" in text
    assert "Cliente creado" in text and "Papelería Los Andes 901234567" in text
    assert [tool["name"] for tool in request["tools"]] == ["verdict"]


@pytest.mark.asyncio
async def test_rejected_verdict() -> None:
    model = FakeModel([tool_decision(("verdict", {"confirmed": False, "evidence": "No aparece"}))])
    assert await Verifier(model).confirm(PLAN, "x", IMAGE, BOXES) == (False, "No aparece")


@pytest.mark.asyncio
async def test_no_tool_call_is_not_confirmed() -> None:
    model = FakeModel([text_decision("parece bien")])
    ok, evidence = await Verifier(model).confirm(PLAN, "x", IMAGE, BOXES)
    assert ok is False and evidence


@pytest.mark.asyncio
async def test_model_error_is_not_confirmed() -> None:
    model = FakeModel([RuntimeError("api down")])
    ok, evidence = await Verifier(model).confirm(PLAN, "x", IMAGE, BOXES)
    assert ok is False and evidence


@pytest.mark.asyncio
async def test_non_boolean_confirmation_is_rejected() -> None:
    model = FakeModel([tool_decision(("verdict", {"confirmed": "yes", "evidence": "?"}))])
    ok, _ = await Verifier(model).confirm(PLAN, "x", IMAGE, BOXES)
    assert ok is False


@pytest.mark.asyncio
async def test_post_commit_evidence_is_sent_as_images_and_text() -> None:
    toast_image = Image.new("RGB", (1280, 800), (20, 30, 120))
    evidence = [CommitEvidence(label="Guardar", texts=("Cliente CL-0007 guardado",), image=toast_image)]
    model = FakeModel([tool_decision(("verdict", {"confirmed": True, "evidence": "Toast de guardado"}))])
    ok, _ = await Verifier(model).confirm(PLAN, "Registra al cliente", IMAGE, BOXES, evidence=evidence)
    assert ok is True
    content = model.requests[0]["messages"][0]["content"]
    images = [block for block in content if block["type"] == "image"]
    assert len(images) == 2, "fresh screenshot plus the post-commit screenshot"
    texts = " ".join(block["text"] for block in content if block["type"] == "text")
    assert "Cliente CL-0007 guardado" in texts
    assert "Guardar" in texts
    assert content.index(images[0]) < content.index(images[1])


@pytest.mark.asyncio
async def test_without_evidence_only_the_fresh_screenshot_is_sent() -> None:
    model = FakeModel([tool_decision(("verdict", {"confirmed": False, "evidence": "No aparece"}))])
    await Verifier(model).confirm(PLAN, "x", IMAGE, BOXES, evidence=[])
    content = model.requests[0]["messages"][0]["content"]
    assert len([block for block in content if block["type"] == "image"]) == 1


def test_verify_prompt_accepts_post_commit_success_message() -> None:
    prompt = load_prompt("verify", PROMPTS_DIR).lower()
    assert "right after the commit" in prompt
    assert "error message" in prompt
