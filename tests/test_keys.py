"""Unit tests for keysym mapping, combo parsing and the blocked-combo list."""

import pytest

from agent.infra.vnc.keys import BLOCKED_COMBOS, BlockedKeyError, keysym, parse_combo

KEY_RETURN = 0xFF0D
KEY_TAB = 0xFF09
KEY_ESCAPE = 0xFF1B
KEY_CTRL = 0xFFE3
KEY_SHIFT = 0xFFE1
KEY_ALT = 0xFFE9
KEY_F2 = 0xFFBF


@pytest.mark.parametrize(
    ("char", "expected"),
    [("a", 0x61), ("ñ", 0xF1), ("#", 0x23), ("\n", KEY_RETURN), ("\t", KEY_TAB), ("€", 0x010020AC)],
)
def test_keysym_spec_mapping(char: str, expected: int) -> None:
    assert keysym(char) == expected


@pytest.mark.parametrize("char", list("áéíóúÑü"))
def test_keysym_accented_latin1_is_codepoint(char: str) -> None:
    assert keysym(char) == ord(char)


def test_keysym_non_latin1_uses_unicode_offset() -> None:
    assert keysym("中") == 0x01000000 + ord("中")


def test_parse_combo_modifiers_first() -> None:
    assert parse_combo("ctrl+a") == [KEY_CTRL, 0x61]
    assert parse_combo("shift+tab") == [KEY_SHIFT, KEY_TAB]
    assert parse_combo("ctrl+shift+a") == [KEY_CTRL, KEY_SHIFT, 0x61]


@pytest.mark.parametrize(
    ("combo", "expected"),
    [("Enter", KEY_RETURN), ("Escape", KEY_ESCAPE), ("F2", KEY_F2), ("Tab", KEY_TAB)],
)
def test_parse_combo_single_keys(combo: str, expected: int) -> None:
    assert parse_combo(combo) == [expected]


def test_parse_combo_case_insensitive_names() -> None:
    assert parse_combo("CTRL+A") == [KEY_CTRL, 0x61]
    assert parse_combo("ENTER") == [KEY_RETURN]


def test_parse_combo_rejects_unknown_key() -> None:
    with pytest.raises(ValueError):
        parse_combo("ctrl+nonsense")


def test_blocked_set_is_exact() -> None:
    assert {c.lower() for c in BLOCKED_COMBOS} == {
        "f5", "ctrl+r", "ctrl+w", "ctrl+q", "alt+f4",
        "ctrl+l", "ctrl+t", "ctrl+n", "f11", "f12",
    }
    assert len(BLOCKED_COMBOS) == 10


@pytest.mark.parametrize("combo", ["F5", "ctrl+r", "ctrl+w", "ctrl+q", "alt+F4",
                                   "ctrl+l", "ctrl+t", "ctrl+n", "F11", "F12"])
def test_every_blocked_combo_raises(combo: str) -> None:
    with pytest.raises(BlockedKeyError):
        parse_combo(combo)


@pytest.mark.parametrize("combo", ["f5", "Ctrl+R", "CTRL+W", "Alt+F4", "ALT+f4", "Ctrl+N", "f12"])
def test_blocked_combo_case_variants_raise(combo: str) -> None:
    with pytest.raises(BlockedKeyError):
        parse_combo(combo)


def test_non_blocked_neighbours_are_allowed() -> None:
    assert parse_combo("ctrl+a") == [KEY_CTRL, 0x61]
    assert parse_combo("alt+F3") == [KEY_ALT, 0xFFC0]
    assert parse_combo("F6") == [0xFFC3]
