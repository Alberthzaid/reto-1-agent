# VNC Screen Specification

## Purpose
`VncScreen` adapts `RFBClient` to the Screen port.

## Requirements

### Requirement: Screenshot
`screenshot()` MUST return a PIL RGB image of the framebuffer size (1280x800), converting BGRX to RGB.

#### Scenario: Screenshot
- GIVEN a connected VncScreen on the mock server
- WHEN a screenshot is requested
- THEN a PIL RGB image of 1280x800 is returned with channels swapped from BGRX

### Requirement: Click
`click` MUST move the pointer, wait 50 ms, press, hold ~150 ms, then release, so the server sees a hold of at least 100 ms. `double=True` MUST perform two clicks ~80 ms apart. Button "right" MUST use mask 4. Coordinates outside the screen MUST raise `ValueError`.

#### Scenario: Left click timing
- GIVEN a connected VncScreen
- WHEN `click(640, 400)` is issued
- THEN the server records move, then press after ~50 ms, then release >=100 ms after press

#### Scenario: Double click
- GIVEN a connected VncScreen
- WHEN `click(640, 400, double=True)` is issued
- THEN two press/release pairs are recorded ~80 ms apart

#### Scenario: Right click
- GIVEN a connected VncScreen
- WHEN `click(10, 10, button="right")` is issued
- THEN the pressed button mask is 4

#### Scenario: Out of screen
- GIVEN a 1280x800 screen
- WHEN `click(1300, 10)` is issued
- THEN `ValueError` is raised and no event is sent

### Requirement: Typing and keys
`type_text` MUST send key down/up per character with 15-20 ms between events. `key(combo)` MUST press modifiers then the key, and release in reverse order.

#### Scenario: Type
- GIVEN a connected VncScreen
- WHEN text "x" is typed
- THEN the server receives matching key down/up events spaced 15-20 ms

#### Scenario: Combo order
- GIVEN a connected VncScreen
- WHEN `key("ctrl+a")` is called
- THEN events are ctrl down, a down, a up, ctrl up

### Requirement: Scroll
`scroll(x, y, steps)` MUST scroll up with button 4 (mask 8) when `steps` is positive and down with button 5 (mask 16) when negative, with one press/release per step.

#### Scenario: Scroll up
- GIVEN a connected VncScreen
- WHEN `scroll(640, 400, 3)` is called
- THEN three button-4 press/release pairs are recorded

#### Scenario: Scroll down
- GIVEN a connected VncScreen
- WHEN `scroll(640, 400, -2)` is called
- THEN two button-5 press/release pairs are recorded

### Requirement: Resource cleanup
`VncScreen` MUST close the connection on failure and on exit.

#### Scenario: Failure closes
- GIVEN a connection error mid-operation
- WHEN the error propagates
- THEN the client is closed
