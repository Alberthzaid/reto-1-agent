# Core Contracts Specification

## Purpose
Async port Protocols and domain dataclasses; domain depends only on ports.

## Requirements

### Requirement: Screen protocol
`ports/interfaces.py` MUST define Screen with exactly: `connect(url, password)`, `screenshot() -> PIL.Image`, `click(x, y, button="left", double=False)`, `type_text(text)`, `key(combo)`, `scroll(x, y, steps)`, `last_change_ts() -> float`, `close()`.

#### Scenario: Structural conformance
- GIVEN a fake class implementing all Screen methods
- WHEN it is used where Screen is expected
- THEN it type-checks and runs without inheritance

#### Scenario: Signature check
- GIVEN the Screen protocol
- WHEN its members are inspected
- THEN names, parameters and defaults match the list above

### Requirement: Other ports
The module MUST define `Reader.read(img, region=None) -> list[TextBox]`; `Model.decide(system, messages, tools) -> Decision`; `Memory` with `fingerprint`, `procedures`, `save_procedure`, `save_task`, `load_task`, `mark_stale_running_as_failed`; and `Classifier.classify(...) -> Classification | None`.

#### Scenario: Port members
- GIVEN each protocol
- WHEN its members are inspected
- THEN all listed methods exist with the stated return types

#### Scenario: No classification
- GIVEN a fake Classifier with no match
- WHEN `classify` is awaited
- THEN it returns None

### Requirement: Domain models
`domain/models.py` MUST define Task (with `estado`, `resumen`, no password field), Observation, Action, Commit, Procedure, Decision, Classification, TextBox. TextBox MUST expose a `center` property.

#### Scenario: Task has no secret
- GIVEN the Task dataclass
- WHEN its fields are listed
- THEN none is named or typed as a password

#### Scenario: Importable models
- GIVEN the package
- WHEN each model is imported
- THEN all eight import without infra/api imports

#### Scenario: TextBox center
- GIVEN a TextBox at x=10, y=20, w=100, h=40
- WHEN `center` is read
- THEN it returns (60, 40)

### Requirement: Dependency direction
`domain` and `ports` MUST NOT import `infra`, `api` or `application`.

#### Scenario: Import check
- GIVEN modules under `agent/domain` and `agent/ports`
- WHEN their imports are inspected
- THEN no forbidden package appears
