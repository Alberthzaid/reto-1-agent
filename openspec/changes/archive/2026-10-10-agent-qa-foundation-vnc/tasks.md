# Tasks: Agent-QA Foundation + VNC Adapter

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~1600 (PR1 ~600, PR2a ~500, PR2b ~500) |
| 400-line budget risk | High (800-line review budget also exceeded if single PR) |
| Chained PRs recommended | Yes |
| Suggested split | PR 1 -> PR 2a -> PR 2b |
| Delivery strategy | single-pr |
| Chain strategy | pending |

Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: pending
400-line budget risk: High

Note: strategy is `single-pr`, so a single ~1600-line PR requires `size:exception`; otherwise the user must choose a chain strategy (stacked-to-main or feature-branch-chain). Each unit is independently revertible.

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | Settings, ports, models, logging, /health | PR 1 | `uv run pytest tests/test_config.py tests/test_models.py tests/test_log_setup.py tests/test_api.py` | `uv run uvicorn agent.main:app` then GET /health | Stage 1 files only |
| 2a | keys, RFB codec, RFBClient | PR 2a | `uv run pytest tests/test_keys.py tests/test_rfb.py` | N/A: needs MockVncServer (2b) for sockets; codec tests are pure | keys.py, rfb.py + tests |
| 2b | VncScreen, MockVncServer, record_screens | PR 2b | `uv run pytest tests/test_vnc_screen.py` | `uv run python -m dev_tools.record_screens` (real desktop, then `POST /__reset`) | vnc_screen.py, dev_tools/*, tests |

## PR 1: Foundation (Stage 1)

- [x] 1.1 Run `uv sync` (.venv incomplete); confirm `uv run pytest` collects.
- [x] 1.2 RED `tests/test_config.py`: 12 vars + defaults, `from_env(Mapping)`, repr masks 3 secrets, `require_desktop()` raises `ConfigError` naming variable only.
- [x] 1.3 GREEN `agent/config.py`: frozen `Settings`, `ConfigError`.
- [x] 1.4 RED `tests/test_models.py`: frozen/slots models, `TaskStatus`, `TextBox.center`.
- [x] 1.5 GREEN `agent/domain/models.py` (resolve open question: TaskStatus values).
- [x] 1.6 GREEN `agent/ports/interfaces.py`: `Screen`, `Reader`, `Model`, `Memory`, `Classifier` runtime-checkable Protocols (conformance asserted in tests/test_models.py).
- [x] 1.7 RED `tests/test_log_setup.py` (autouse registry-clear fixture): secret becomes `***` in msg, args, traceback; JSON lines.
- [x] 1.8 GREEN `agent/observability/log_setup.py`: `RedactingFormatter`, `register_secret`, `setup_logging` (UTF-8).
- [x] 1.9 RED `tests/test_api.py`: GET /health 200 with no env keys.
- [x] 1.10 GREEN `agent/main.py` FastAPI `/health`; fill `.env.example` with all vars.

## PR 2a: Keys, Codec, RFBClient (Stage 2)

- [x] 2.1 RED `tests/test_keys.py`: keysym ASCII/Latin-1/Unicode, `\n`/`\t`, `parse_combo` modifiers first, all 10 `BLOCKED_COMBOS` (case variants) -> `BlockedKeyError`.
- [x] 2.2 GREEN `agent/infra/vnc/keys.py`.
- [x] 2.3 RED `tests/test_rfb.py` pure: DES `vnc_auth_response` vector (8-byte truncation, bit reversal), pointer/key packing, BGRX->RGB.
- [x] 2.4 GREEN `agent/infra/vnc/rfb.py` codec functions and error hierarchy.
- [x] 2.5 GREEN `RFBClient` (connect 3.8/3.3 fallback, subprotocol `binary`, `max_size=None`, lock per update, `last_change` monotonic, `close`, loop error re-raise); socket tests land in 2b with mock.
- [x] 2.6 REFACTOR: confirm no password in logs/exceptions.

## PR 2b: VncScreen, Mock, Recorder (Stage 2)

- [x] 3.1 RED+GREEN `dev_tools/mock_vnc_server.py`: real DES, 600ms delayed square repaint, per-key mark, `chunk_size`, `events`, versions 3.3/3.8.
- [x] 3.2 RED `tests/test_rfb.py` integration: auth OK/fail, refused, unsupported security, `chunk_size=1,7`, CopyRect, loop error propagation.
- [x] 3.3 RED `tests/test_vnc_screen.py`: screenshot RGB 1280x800 copy; click timings (50/150ms), double (80ms), right mask 4; `last_change_ts` advances after delayed repaint.
- [x] 3.4 RED same file: type_text spacing 15-20ms, key combo press/release order, blocked key, scroll (+ btn4 mask 8, - btn5 mask 16, one press/release per step), out-of-bounds `ValueError`.
- [x] 3.5 GREEN `agent/infra/vnc/vnc_screen.py`: `VncScreen` implementing `Screen`; `register_secret(password)`.
- [x] 3.6 GREEN `dev_tools/record_screens.py`: PNGs to `data/screens/`, creds from env only.
- [x] 3.7 Run `uv run pytest` (150 passed); manual smoke on real desktop via `record_screens` saved a correct 1280x800 capture (read-only, no `POST /__reset` needed).
