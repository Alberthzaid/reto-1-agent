# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Desktop automation for QA. A Python client connects to a remote desktop over VNC (RFB protocol tunneled through a websocket), authenticates with `DESKTOP_PASSWORD`, clicks at X,Y coordinates, optionally sends Ctrl+A and types text, and saves screenshots to an output folder.

The long-term goal (see `agent/`) is an LLM-driven QA agent (Anthropic SDK) that reads the screen via framebuffer + Tesseract OCR and acts through mouse/keyboard events.

## Current state

Only `prueba_rapida.py` contains working code. Almost every file under `agent/`, `dev_tools/` and `tests/`, plus `README.md`, `Dockerfile`, `Caddyfile` and `.env.example`, is an empty placeholder. `main.py` / `test_main.http` are the PyCharm FastAPI template, not the real app. `agent/infra/vnc/rfb.py` is a stub. Always check a file's contents before assuming behavior exists.

## Running

```bash
uv sync                                                    # install deps (uv.lock is the source of truth)
uv run python prueba_rapida.py                             # connect + save capturas_prueba/01_inicial.png
uv run python prueba_rapida.py --click 640 400             # click, save 02_despues_clic.png
uv run python prueba_rapida.py --click 640 400 --type "x"  # click, Ctrl+A, type, save 03_despues_escribir.png
uv run python prueba_rapida.py --out otra_carpeta          # custom output folder
```

Configuration comes from `.env` (gitignored, loaded with `python-dotenv`):
- `DESKTOP_URL` — `wss://<desktop>/websockify`. Required (env or `--url`); there is no hardcoded fallback.
- `DESKTOP_PASSWORD` — VNC password.

Tesseract OCR must be installed at the OS level for `pytesseract`. After any mutating run (`--click`/`--type`), reset the target app with `POST /__reset`.

## Structure

- `prueba_rapida.py` — standalone RFB client: handshake, VNC DES auth, framebuffer read loop, pointer/key events, PNG capture.
- `agent/` — hexagonal layout (placeholders):
  - `domain/` — models, policy, LLM tools, guard, verifier, reporter; prompts in `domain/prompts/` (orient → plan → verify loop).
  - `ports/interfaces.py` — abstract ports; domain depends only on these.
  - `infra/` — adapters: `vnc/` (RFB, keysyms, screen), Tesseract OCR, Anthropic LLM, SQLite memory.
  - `application/` — job manager/worker. `api/` — FastAPI. `observability/` — logging and traces.
- `dev_tools/` — fake screen, screen recorder, trace viewer, test runner (placeholders).
- `tests/` — pytest suites (placeholders).

Dependencies point inward: `domain` never imports `infra`, `api` or `application`.

## Conventions

- Code must run on Python 3.10+ (`X | None`, built-in generics). Note: `pyproject.toml` currently declares `requires-python >= 3.13`; do not change it without asking.
- Type hints on every function signature and public attribute.
- Use the `logging` module, never `print` (the existing `log()` helper in `prueba_rapida.py` predates this rule).
- Never print, log, include in exceptions, or write to traces the password or any value derived from it (including the DES key/response). Prefer env vars over the `--password` CLI flag, which leaks into shell history and process lists.
- Screenshots may contain sensitive data; do not commit capture folders.
- Existing user-facing strings (logs, CLI help) are in Spanish; new code identifiers and comments in English.

## VNC gotchas

- Only Raw + CopyRect encodings are requested. A full 1280x800 Raw frame is ~4 MB and arrives slowly over the hosted websocket; a black capture means the first frame never completed.
- VNC auth: password truncated to 8 Latin-1 bytes, each byte bit-reversed, DES-ECB over the 16-byte challenge.
- A button press shorter than ~100 ms is ignored; clicks hold ~150 ms.
- Keysyms: ASCII/Latin-1 map directly; other Unicode is `0x01000000 + codepoint`.
- Coordinates are absolute framebuffer pixels from top-left. Framebuffer is BGRX; swap `[2, 1, 0]` when saving.

## Tests

```bash
uv run pytest                                  # all tests
uv run pytest tests/test_rfb.py                # one file
uv run pytest tests/test_rfb.py::test_name     # one test
uv run pytest -k keysym                        # by name
```

Framework: pytest + pytest-asyncio. No `[tool.pytest.ini_options]` yet, so async tests need `@pytest.mark.asyncio`. Tests must not hit the real desktop; use a fake screen/server (`dev_tools/fake_screen.py`, planned).
