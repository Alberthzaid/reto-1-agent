# VNC Test Harness Specification

## Purpose
Offline mock RFB server and screen recorder so tests never touch the real desktop.

## Requirements

### Requirement: Mock RFB server
`dev_tools/mock_vnc_server.py` MUST perform real DES verification, MUST support `chunk_size` splitting of frames, MUST record received pointer/key events, MUST repaint a square 600 ms after each click, and MUST draw on each key event.

#### Scenario: Wrong password rejected
- GIVEN the mock server
- WHEN a client sends a wrong DES response
- THEN the server replies auth-failed

#### Scenario: Chunked frames
- GIVEN `chunk_size=64`
- WHEN a frame is served
- THEN it arrives in messages of at most 64 bytes

#### Scenario: Delayed repaint on click
- GIVEN a connected VncScreen on the mock server
- WHEN a click is issued
- THEN a screenshot taken right after is unchanged, and one taken after the 600 ms delay differs

#### Scenario: Draw per key
- GIVEN a connected VncScreen on the mock server
- WHEN a key is typed
- THEN a later screenshot differs from the previous one

### Requirement: Screen recorder
`dev_tools/record_screens.py` MUST save screenshots to `data/screens/`, reading `DESKTOP_URL` and `DESKTOP_PASSWORD` from the environment only.

#### Scenario: Record
- GIVEN the mock server URL and password in env
- WHEN the recorder runs
- THEN a PNG file exists in `data/screens/`

#### Scenario: Missing env
- GIVEN `DESKTOP_URL` is unset
- WHEN the recorder runs
- THEN it fails with a clear error naming the variable

### Requirement: Offline test suite
`uv run pytest` MUST pass with no network access.

#### Scenario: No network
- GIVEN no external connectivity
- WHEN the suite runs
- THEN all tests pass using only localhost mocks
