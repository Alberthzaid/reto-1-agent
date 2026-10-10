"""Independent end-of-task check with the strong model on a fresh screenshot plus post-commit evidence."""

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from PIL import Image

from agent.domain.models import Plan, TextBox
from agent.domain.policy import PROMPTS_DIR, describe_plan, first_call, load_prompt, screen_content
from agent.domain.tools import CommitEvidence, image_block
from agent.ports.interfaces import Model

log = logging.getLogger(__name__)

MAX_EVIDENCE_SHOTS = 3

VERDICT: dict[str, Any] = {
    "name": "verdict",
    "description": "Say whether the screen confirms the task was done exactly as asked.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "confirmed": {"type": "boolean"},
            "evidence": {"type": "string", "description": "What you saw, in Spanish."},
        },
        "required": ["confirmed", "evidence"],
        "additionalProperties": False,
    },
}


class Verifier:
    """Without a clear confirmation the task is not reported as done."""

    def __init__(self, model: Model, prompts_dir: Path = PROMPTS_DIR) -> None:
        self._model = model
        self._prompts_dir = prompts_dir

    async def confirm(
        self,
        plan: Plan,
        instruction: str,
        image: Image.Image,
        boxes: Sequence[TextBox],
        claim: str = "",
        evidence: Sequence[CommitEvidence] = (),
    ) -> tuple[bool, str]:
        shots = list(evidence)[-MAX_EVIDENCE_SHOTS:]
        question = (
            "Do this screenshot and the post-commit captures below confirm exactly the expected evidence?"
            if shots
            else "Does this screenshot confirm exactly the expected evidence?"
        )
        text = (
            f"Task: {instruction}\n\nPlan:\n{describe_plan(plan)}\n\n"
            f"Agent claim: {claim or '(none)'}\n\n"
            f"The first image is a fresh screenshot taken now. {question}"
        )
        content = await screen_content(text, image, boxes)
        for shot in shots:
            texts = " | ".join(shot.texts) or "(no text read)"
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"Screenshot taken right after the commit '{shot.label}'. "
                        f"Text read in the regions that changed: {texts}"
                    ),
                }
            )
            content.append(await asyncio.to_thread(image_block, shot.image))
        try:
            decision = await self._model.decide(
                load_prompt("verify", self._prompts_dir), [{"role": "user", "content": content}], [VERDICT]
            )
        except Exception:
            log.exception("falló la verificación con el modelo")
            return False, "No se pudo verificar el resultado en pantalla."
        call = first_call(decision, "verdict")
        if call is None:
            return False, "El verificador no emitió un veredicto."
        confirmed = call.input.get("confirmed") is True
        evidence = str(call.input.get("evidence", "")).strip() or "Sin evidencia."
        return confirmed, evidence
