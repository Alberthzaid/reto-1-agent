# VNC Client Specification

## Purpose
RFB-over-websocket client with VNC auth, framebuffer, input events and keysym handling.

## Requirements

### Requirement: Connection and handshake
`RFBClient` MUST open the websocket with subprotocol "binary", negotiate RFB 3.8 with fallback to 3.3, and complete VNC DES auth (password truncated to 8 Latin-1 bytes, bit-reversed). It MUST raise typed errors for auth failure, connection refused and unsupported security type. The password MUST NOT appear in logs or exceptions.

#### Scenario: Auth success
- GIVEN the mock server with password P
- WHEN the client connects with P
- THEN the session is established using subprotocol "binary"

#### Scenario: Version fallback
- GIVEN a mock server announcing RFB 003.003
- WHEN the client connects
- THEN the handshake completes using 3.3

#### Scenario: Auth failure
- GIVEN the mock server
- WHEN the client connects with a wrong password
- THEN a typed auth error is raised, its text lacks the password, and the socket is closed

#### Scenario: Connection refused
- GIVEN no server listening on the URL
- WHEN the client connects
- THEN a typed connection-refused error is raised

#### Scenario: Unsupported security type
- GIVEN a server offering only an unsupported security type
- WHEN the client connects
- THEN a typed unsupported-security error is raised

### Requirement: Framebuffer reassembly
The client MUST support Raw and CopyRect updates, MUST reassemble RFB messages split across websocket frames, MUST guard the framebuffer with a lock, and MUST update `last_change` (monotonic clock) on each framebuffer update.

#### Scenario: Split frame
- GIVEN a mock server with small `chunk_size`
- WHEN a full frame is sent
- THEN the framebuffer equals the original image

#### Scenario: last_change
- GIVEN a connected client with recorded `last_change` T0
- WHEN a framebuffer update arrives
- THEN `last_change` is greater than T0

### Requirement: Input events and close
The client MUST send pointer and key events, MUST raise an error for coordinates outside the framebuffer (no clamping), and `close()` MUST cancel the reader task and close the socket.

#### Scenario: Click
- GIVEN a connected client
- WHEN a pointer press and release at (640,400) are sent with a 150 ms hold
- THEN the server receives press then release >=100 ms apart

#### Scenario: Out of bounds
- GIVEN a 1280x800 framebuffer
- WHEN a pointer event at (1300,10) is requested
- THEN an error is raised and no event is sent

#### Scenario: Close
- GIVEN a connected client
- WHEN `close()` is called
- THEN the reader task is cancelled and the socket is closed

### Requirement: Keysyms
`keys.py` MUST map ASCII/Latin-1 characters directly (including á é í ó ú ñ Ñ ü #), "\n" to Return, "\t" to Tab, and any non-Latin-1 character to `0x01000000 + codepoint`.

#### Scenario: Mapping
- GIVEN "a", "ñ", "#", "\n", "\t" and "€"
- WHEN keysyms are resolved
- THEN results are 0x61, 0xF1, 0x23, Return (0xFF0D), Tab (0xFF09) and 0x010020AC

#### Scenario: Accented Latin-1
- GIVEN "á é í ó ú Ñ ü"
- WHEN keysyms are resolved
- THEN each equals its Latin-1 code point

### Requirement: Combo parsing and blocked combos
`parse_combo` MUST return modifiers first, then the key (e.g. "ctrl+a", "shift+tab", "Enter", "Escape", "F2"). `BLOCKED_COMBOS` MUST be exactly F5, ctrl+r, ctrl+w, ctrl+q, alt+F4, ctrl+l, ctrl+t, ctrl+n, F11, F12; parsing any of them MUST raise `BlockedKeyError`.

#### Scenario: Parse with modifiers
- GIVEN "ctrl+a" and "shift+tab"
- WHEN parsed
- THEN ctrl/shift keysyms precede the key keysym

#### Scenario: Parse single keys
- GIVEN "Enter", "Escape" and "F2"
- WHEN parsed
- THEN each yields one keysym with no modifiers

#### Scenario: Blocked combo
- GIVEN each of the ten blocked combos (case-insensitive, e.g. "Alt+F4")
- WHEN the combo is parsed
- THEN `BlockedKeyError` is raised

#### Scenario: Blocked set is exact
- GIVEN `BLOCKED_COMBOS`
- WHEN inspected
- THEN it equals the ten listed combos and nothing else
