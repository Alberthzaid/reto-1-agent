# Proposal: Agent-QA Foundation + VNC Adapter

## Intent

Only `prueba_rapida.py` works; the hexagonal skeleton is empty. The agent needs typed config, ports, domain models, secret-safe logging, and a reusable, tested VNC adapter before any LLM logic can be built.

## Scope

### In Scope
- **Stage 1**: `agent/config.py` (frozen dataclass `Settings.from_env`, masked `__repr__`); `ports/interfaces.py` (async Protocols: Screen, Reader, Model, Memory, Classifier); `domain/models.py` (Task with `estado`/`resumen`, no password; Observation, Action, Commit, Procedure, Decision, Classification, TextBox); `observability/log_setup.py` (structured logs, `register_secret`, masks tracebacks); `agent/main.py` `GET /health`; `.env.example`; missing folders; tests.
- **Stage 2**: `infra/vnc/rfb.py` RFBClient (refactor of `prueba_rapida.py`); `infra/vnc/keys.py`; `infra/vnc/vnc_screen.py` VncScreen; `dev_tools/mock_vnc_server.py`; `dev_tools/record_screens.py`; `tests/test_rfb.py`, `tests/test_keys.py`.

### Out of Scope
- Domain logic (policy, guard, verifier, reporter), OCR, LLM, SQLite, Falco adapters, task API.
- `pyproject.toml` changes; touching `api/` or `domain/` in Stage 2; deleting `prueba_rapida.py`/root `main.py`.

## Capabilities

### New Capabilities
- `runtime-config`: env-driven Settings with secret masking.
- `core-contracts`: port Protocols and domain dataclasses.
- `secret-safe-logging`: structured logging with secret redaction.
- `health-endpoint`: FastAPI liveness probe.
- `vnc-client`: RFB-over-websocket protocol client and keysym/combo handling.
- `vnc-screen`: Screen port adapter (pointer/keyboard/screenshot).
- `vnc-test-harness`: mock RFB server and screen recorder.

### Modified Capabilities
- None

## Approach

Stage 1 first (VncScreen depends on ports/models). Strict TDD per module. RFBClient ports the proven handshake/DES/read-loop from `prueba_rapida.py`, adding typed errors, framebuffer lock, and closing on failure. The mock server performs real DES verification and supports `chunk_size` to exercise split-frame reassembly. No hardcoded production URL.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `agent/config.py`, `agent/main.py` | New | Settings, /health |
| `agent/ports/`, `agent/domain/models.py` | New | Contracts |
| `agent/observability/log_setup.py` | New | Redacting logger |
| `agent/infra/vnc/` | New | rfb, keys, vnc_screen |
| `dev_tools/` | New | mock server, recorder |
| `tests/` | New | 4 test modules |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Forecast ~1300-1500 lines exceeds 800 budget | High | Flag to tasks; consider 2 PRs (stage split) or `size:exception` |
| Password leak via tracebacks | Med | Formatter-level masking; tests assert redaction |
| Split-frame/protocol bugs vs real server | Med | Chunked mock tests; manual recorder check |
| Partial `.venv` | Med | `uv sync` before apply |

## Rollback Plan

All files are new or empty placeholders; revert the PR. `prueba_rapida.py` remains as a working fallback.

## Dependencies

- Existing deps only (no new packages). `uv sync` required.

## Success Criteria

- [ ] `uv run pytest` green without network access to the real desktop.
- [ ] `GET /health` returns 200.
- [ ] Mock-server tests cover auth success/failure, split frames, click repaint, key draw.
- [ ] No test or log output contains a registered secret.

## Proposal question round

Auto mode: not asked. Assumptions for review:
1. `BLOCKED_COMBOS` list (e.g. Alt+F4, Ctrl+Alt+Del, Super) — is any legitimately needed by QA tasks?
2. Coordinates outside 1280x800 raise an error (no clamping).
3. Stage split into 2 PRs is acceptable if budget is enforced.
