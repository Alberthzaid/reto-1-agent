"""Procedure memory helpers: intent keys, sanitization of learned navigation and prompt hints."""

import re
import unicodedata
from collections.abc import Iterable, Sequence

from agent.domain.models import Plan, Procedure, ScriptStep

MIN_TOKEN_CHARS = 3
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
QUOTED_RE = re.compile(r"'([^']*)'")
DIGITS_RE = re.compile(r"\d+")
WORD_RE = re.compile(r"\w+")
DATA_MASK = "<dato>"
SECRET_MASK = "<secreto>"
EMAIL_MASK = "<email>"
NUMBER_MASK = "<n>"


def normalize(text: str) -> str:
    """Lowercase, accents removed, surrounding spaces stripped ("Artículo " -> "articulo")."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower().strip()


def intent_key(plan: Plan) -> str:
    """Stable key for 'the same kind of task', e.g. 'crear:cliente'."""
    return f"{plan.kind}:{normalize(plan.entity)}"


def _data_tokens(plan: Plan) -> set[str]:
    texts = [plan.target, *(value for _, value in plan.values)]
    return {
        token for text in texts for token in re.findall(r"[a-z0-9]+", normalize(text))
        if len(token) >= MIN_TOKEN_CHARS
    }


def _normalized_secrets(secrets: Iterable[str]) -> list[str]:
    return [normalize(secret) for secret in secrets if secret and secret.strip()]


def _has_data(step: str, tokens: set[str], secrets: Sequence[str]) -> bool:
    normalized = normalize(step)
    if EMAIL_RE.search(step) or any(secret in normalized for secret in secrets):
        return True
    if any(DIGITS_RE.search(label) for label in QUOTED_RE.findall(step)):
        return True
    return bool(set(re.findall(r"[a-z0-9]+", normalized)) & tokens)


def sanitize_steps(steps: Iterable[str], plan: Plan, secrets: Iterable[str]) -> tuple[str, ...]:
    """Navigation steps without task data; a step that mentions any data is dropped, not masked."""
    tokens = _data_tokens(plan)
    normalized_secrets = _normalized_secrets(secrets)
    return tuple(step for step in steps if not _has_data(step, tokens, normalized_secrets))


def sanitize_script(script: Iterable[ScriptStep], plan: Plan, secrets: Iterable[str]) -> tuple[ScriptStep, ...]:
    """The script unchanged, or empty when any label or field name mentions task data (all or nothing:
    a script with a step removed would not reproduce the route)."""
    steps = tuple(script)
    tokens = _data_tokens(plan)
    normalized_secrets = _normalized_secrets(secrets)
    for step in steps:
        for text in (step.anchor, step.field_label, step.field):
            if text and _has_data(f"'{text}'", tokens, normalized_secrets):
                return ()
    return steps


def sanitize_text(text: str, plan: Plan, secrets: Iterable[str]) -> str:
    """Masks emails, secrets, words of the task's target/values and every number."""
    tokens = _data_tokens(plan)
    clean = EMAIL_RE.sub(EMAIL_MASK, text)
    for secret in sorted((s for s in secrets if s and s.strip()), key=len, reverse=True):
        clean = re.sub(re.escape(secret), SECRET_MASK, clean, flags=re.IGNORECASE)

    def mask_word(match: re.Match[str]) -> str:
        word = match.group(0)
        parts = re.findall(r"[a-z0-9]+", normalize(word))
        return DATA_MASK if any(part in tokens for part in parts) else word

    clean = WORD_RE.sub(mask_word, clean)
    return DIGITS_RE.sub(NUMBER_MASK, clean)


def describe_hints(procedures: Sequence[Procedure]) -> str:
    """Prompt block with the routes that worked before; empty when there are none."""
    if not procedures:
        return ""
    lines = [
        "Hints from previous tasks of this kind in this app (suggestions only; verify each menu and button "
        "on screen before following them):"
    ]
    for procedure in procedures:
        steps = " -> ".join(procedure.steps) or "(no steps recorded)"
        line = f"- Steps: {steps} (worked {procedure.exitos} time(s))."
        if procedure.aprendizaje:
            line += f" Learned: {procedure.aprendizaje}"
        lines.append(line)
    return "\n".join(lines)
