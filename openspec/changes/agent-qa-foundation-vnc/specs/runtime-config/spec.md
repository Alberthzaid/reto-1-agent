# Runtime Config Specification

## Purpose
Typed, immutable, env-driven settings that never expose secrets.

## Requirements

### Requirement: Settings variables and defaults
`Settings.from_env` MUST read these variables, MUST be frozen, and MUST NOT contain a hardcoded desktop URL:

| Variable | Default |
|---|---|
| ANTHROPIC_API_KEY | none (secret) |
| TEAM_TOKEN | none (secret) |
| MODEL_FAST | "claude-sonnet-5-5" |
| MODEL_STRONG | "claude-opus-5-5" |
| TIME_LIMIT_SEC | 255 |
| DATA_DIR | "data" |
| TESSERACT_CMD | optional (None) |
| FALCO_ENABLED | false |
| FALCO_MODEL_DIR | "model_s1" |
| MAX_CONCURRENT_TASKS | 8 |
| DESKTOP_URL | optional (None) |
| DESKTOP_PASSWORD | optional (None, secret) |

#### Scenario: Defaults
- GIVEN only ANTHROPIC_API_KEY and TEAM_TOKEN are set
- WHEN `Settings.from_env()` is called
- THEN every other field has its table default and numeric fields are typed int/bool

#### Scenario: Overrides
- GIVEN `TIME_LIMIT_SEC=100` and `FALCO_ENABLED=true`
- WHEN `Settings.from_env()` is called
- THEN those fields are 100 and True

#### Scenario: Desktop optional
- GIVEN DESKTOP_URL and DESKTOP_PASSWORD are unset
- WHEN `Settings.from_env()` is called
- THEN both are None and no error is raised

#### Scenario: Secrets optional at load time
- GIVEN `ANTHROPIC_API_KEY` and/or `TEAM_TOKEN` are unset
- WHEN `Settings.from_env()` is called
- THEN no error is raised and the fields are None (so `/health` boots without keys); consumers MUST check the secrets they need and fail at point of use, naming the variable only

#### Scenario: Immutability
- GIVEN a Settings instance
- WHEN a field is assigned
- THEN an error is raised

### Requirement: Masked representation
`repr`/`str` of Settings MUST NOT contain ANTHROPIC_API_KEY, TEAM_TOKEN or the desktop password.

#### Scenario: Repr masks secrets
- GIVEN api key "k-123", team token "t-456", desktop password "s3cret"
- WHEN `repr(settings)` and `str(settings)` are taken
- THEN neither contains "k-123", "t-456" or "s3cret"
