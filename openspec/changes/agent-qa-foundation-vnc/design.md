# Design: Agent-QA Foundation + VNC Adapter

## Technical Approach

Two stages with a one-way seam. Stage 1 builds dependency-free contracts (`config`, `ports`, `domain/models`, `observability/log_setup`, `/health`). Stage 2 adds `infra/vnc/*` and `dev_tools/*`. Stage 2 only imports Stage 1's public API and never edits Stage 1 files, so each stage can ship as its own PR. `RFBClient` is a typed port of `prueba_rapida.py`; pure codec functions are separated from websocket I/O so most tests run without sockets. Settling (wait for change + 300 ms quiet, cap 2 s) belongs to the domain `Asentador` and is out of scope; the adapter only exposes `last_change_ts()`.

## Architecture Decisions

| Topic | Options | Tradeoff | Decision |
|---|---|---|---|
| Settings | pydantic-settings / frozen dataclass | new dependency vs. zero deps | `@dataclass(frozen=True)` + `from_env(env: Mapping[str, str] \| None)`; custom `__repr__` masks `anthropic_api_key`, `team_token`, `desktop_password` as `***` |
| Missing secrets / desktop config | fail in `from_env` / fail on use | `/health` must boot without keys or VNC | Secrets and `DESKTOP_URL`/`DESKTOP_PASSWORD` are `str \| None`; `require_desktop() -> tuple[str, str]` raises `ConfigError` (names the variable, never the value) |
| Ports | ABC / `typing.Protocol` | inheritance coupling vs. structural | `@runtime_checkable` Protocols; I/O methods async |
| Screenshot type | PIL / ndarray | user spec fixes PIL | `PIL.Image.Image`, RGB 1280x800; BGRX→RGB (`[2,1,0]`) inside the adapter, always a copy |
| Redaction point | `logging.Filter` / `Formatter` | filters miss `exc_text`/tracebacks | `RedactingFormatter` redacts the fully formatted string |
| Log format | text / JSON lines | grep-ability vs. structure | JSON lines; stream reconfigured to UTF-8 (`errors="replace"`) |
| DES | hand-rolled / pycryptodome | already a dependency | pycryptodome `DES.MODE_ECB`, pure `vnc_auth_response(password, challenge)` |
| Partial frames | no lock / `asyncio.Lock` per update | half-applied rects across awaits | lock held per FramebufferUpdate; snapshot acquires it |
| Failure handling | `RuntimeError` / typed hierarchy | callers distinguish causes | `RFBError` → `RFBConnectionError` (refused), `RFBAuthError` (auth failed), `RFBProtocolError` → `RFBUnsupportedSecurityError`; websocket closed on any handshake error |
| Out-of-bounds coordinates | clamp / raise | clamping hides LLM mistakes | every coordinate (click, scroll) validated → `ValueError` |
| Dangerous combos | domain guard only / adapter too | defense in depth | `keys.BLOCKED_COMBOS` (user list, below); raises `BlockedKeyError` |
| Async tests | `asyncio_mode=auto` / marker | auto needs `pyproject` edits | explicit `@pytest.mark.asyncio` |

## Data Flow

    Settings.from_env ──→ require_desktop() ──→ VncScreen.connect(url, password)
                                                   │ register_secret(password)
                                                   ▼
    caller ─click/type_text/key/scroll─→ VncScreen ──→ RFBClient ──ws("binary")──→ server
                                                   ▲      │ read_loop (task)
                                                   │      ▼
                                 screenshot() ← framebuffer + lock
                                 last_change_ts() ← time.monotonic of last update

If `read_loop` has died, the next call re-raises its exception.

## File Changes

| File | Action | Stage |
|---|---|---|
| `agent/config.py` | Fill: `Settings`, `ConfigError` | 1 |
| `agent/ports/interfaces.py` | Fill: `Screen`, `Reader`, `Model`, `Memory`, `Classifier` | 1 |
| `agent/domain/models.py` | Create: `Task`, `TaskStatus(str, Enum)`, `Observation`, `Action`, `Commit`, `Procedure`, `Decision`, `Classification`, `TextBox` (frozen, slots) | 1 |
| `agent/observability/log_setup.py` | Fill: `setup_logging`, `register_secret`, `RedactingFormatter` | 1 |
| `agent/main.py` | Fill: FastAPI `app`, `GET /health` | 1 |
| `.env.example` | Fill: ALL vars (table below) | 1 |
| `tests/test_config.py`, `test_models.py`, `test_log_setup.py`, `test_api.py` | Create/Fill | 1 |
| `agent/infra/vnc/keys.py` | Fill: `keysym`, `NAMED_KEYS`, `parse_combo`, `BLOCKED_COMBOS`, `BlockedKeyError` | 2 |
| `agent/infra/vnc/rfb.py` | Replace stub: codec + `RFBClient` | 2 |
| `agent/infra/vnc/vnc_screen.py` | Fill: `VncScreen` (implements `Screen`) | 2 |
| `dev_tools/mock_vnc_server.py` | Create: `MockVncServer` | 2 |
| `dev_tools/record_screens.py` | Fill: periodic PNGs to `data/screens/`; password only from env | 2 |
| `tests/test_keys.py`, `test_rfb.py`, `test_vnc_screen.py` | Fill/Create | 2 |

`prueba_rapida.py`, root `main.py`, `pyproject.toml` unchanged.

## Interfaces / Contracts

```python
class Screen(Protocol):
    async def connect(self, url: str, password: str) -> None: ...
    async def screenshot(self) -> PIL.Image.Image: ...       # RGB 1280x800
    async def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None: ...
    async def type_text(self, text: str) -> None: ...
    async def key(self, combo: str) -> None: ...              # "ctrl+a", "enter"
    async def scroll(self, x: int, y: int, steps: int) -> None: ...  # +up(btn4)/-down(btn5), 1 press/release per step
    def last_change_ts(self) -> float: ...
    async def close(self) -> None: ...

class Reader(Protocol):
    async def read(self, img: PIL.Image.Image, region: tuple[int, int, int, int] | None = None) -> list[TextBox]: ...
class Model(Protocol):
    async def decide(self, system: str, messages: list, tools: list) -> Decision: ...
class Memory(Protocol):
    def fingerprint(self, boxes: list[TextBox]) -> str: ...
    async def procedures(self, app: str, intent: str) -> list[Procedure]: ...
    async def save_procedure(self, app: str, procedure: Procedure) -> None: ...
    async def save_task(self, task: Task) -> None: ...
    async def load_task(self, task_id: str) -> Task | None: ...
    async def mark_stale_running_as_failed(self) -> int: ...
class Classifier(Protocol):
    async def classify(self, instruction: str) -> Classification | None: ...

class RFBClient:
    async def connect(self) -> None: ...   # 3.8, falls back to 3.3; subprotocols=["binary"], max_size=None
    async def pointer(self, x: int, y: int, mask: int) -> None: ...
    async def key(self, keysym: int, down: bool) -> None: ...
    last_change: float                     # time.monotonic()
    async def close(self) -> None: ...
```

**keys.py**: `keysym(char)` (ASCII/Latin-1 direct, else `0x01000000+cp`; `\n`→Return, `\t`→Tab). `parse_combo(combo) -> list[int]`, modifiers first. `BLOCKED_COMBOS` = `F5, ctrl+r, ctrl+w, ctrl+q, alt+F4, ctrl+l, ctrl+t, ctrl+n, F11, F12`, matched after case-insensitive normalization.

**VncScreen timings**: click = move, 50 ms, press, hold 150 ms, release; double = two clicks 80 ms apart; masks left 1 / right 4. `type_text`: 15–20 ms between events. `key()`: press modifiers, key, release in reverse.

**Settings env vars**: `ANTHROPIC_API_KEY`, `TEAM_TOKEN`, `MODEL_FAST="claude-sonnet-5-5"`, `MODEL_STRONG="claude-opus-5-5"`, `TIME_LIMIT_SEC=255`, `DATA_DIR="data"`, `TESSERACT_CMD` (optional), `FALCO_ENABLED=false`, `FALCO_MODEL_DIR="model_s1"`, `MAX_CONCURRENT_TASKS=8`, optional `DESKTOP_URL`/`DESKTOP_PASSWORD` (for `record_screens`), `LOG_LEVEL`.

**MockVncServer** (async context manager, `127.0.0.1:0`; `__init__(password, width=1280, height=800, chunk_size=None, version=b"RFB 003.008\n")`, exposes `url`, `events`): real DES check; repaints a square 600 ms AFTER each click (delayed task); draws a mark per key; `chunk_size` splits every message across websocket frames.

## Testing Strategy

| Layer | What | Approach |
|---|---|---|
| Unit | Settings defaults/parse/repr masking; models; redaction; keysyms; combos + blocked list (case variants); DES vector; packing; BGRX→RGB | pure functions |
| Integration | 3.3/3.8, auth OK/fail, refused, unsupported security, split frames (`chunk_size=1,7`), CopyRect, delayed click repaint advances `last_change_ts`, key draw, double/right click masks, scroll buttons/steps, out-of-bounds, loop error propagation | `MockVncServer`, `@pytest.mark.asyncio` |
| API | `/health` 200 | `TestClient` |
| Manual | real desktop | `record_screens.py`, then `POST /__reset` |

Autouse fixture clears the secret registry; log-capturing tests assert secrets are absent.

## Threat Matrix

N/A: no routing, shell, subprocess, VCS/PR automation, executable-file classification, or process-integration boundary. Secret handling is covered by redaction tests.

## Migration / Rollout

No migration. PR 1 = Stage 1 (~600 lines). Stage 2 (~1000 lines) splits: PR 2a = keys + codec + `RFBClient` (~500), PR 2b = `VncScreen` + mock server + `record_screens` + tests (~500). Rollback is a revert; `prueba_rapida.py` stays as fallback.

## Open Questions

- [ ] Full `TaskStatus` value set beyond `terminada`/`fallida`?
