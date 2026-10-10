"""X11 keysym mapping and key-combo parsing for the VNC adapter."""

KEY_RETURN = 0xFF0D
KEY_TAB = 0xFF09
UNICODE_KEYSYM_OFFSET = 0x01000000

MODIFIERS: dict[str, int] = {
    "ctrl": 0xFFE3,
    "control": 0xFFE3,
    "shift": 0xFFE1,
    "alt": 0xFFE9,
    "meta": 0xFFE7,
    "super": 0xFFEB,
}

NAMED_KEYS: dict[str, int] = {
    "enter": KEY_RETURN,
    "return": KEY_RETURN,
    "tab": KEY_TAB,
    "escape": 0xFF1B,
    "esc": 0xFF1B,
    "backspace": 0xFF08,
    "delete": 0xFFFF,
    "space": 0x20,
    "home": 0xFF50,
    "left": 0xFF51,
    "up": 0xFF52,
    "right": 0xFF53,
    "down": 0xFF54,
    "pageup": 0xFF55,
    "pagedown": 0xFF56,
    "end": 0xFF57,
    **{f"f{n}": 0xFFBE + n - 1 for n in range(1, 13)},
}

BLOCKED_COMBOS: frozenset[str] = frozenset(
    {"F5", "ctrl+r", "ctrl+w", "ctrl+q", "alt+F4", "ctrl+l", "ctrl+t", "ctrl+n", "F11", "F12"}
)


class BlockedKeyError(ValueError):
    """Raised when a key combo is on the dangerous-combo blocklist."""


def keysym(char: str) -> int:
    """Return the X11 keysym for a single character."""
    if char == "\n":
        return KEY_RETURN
    if char == "\t":
        return KEY_TAB
    cp = ord(char)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    return UNICODE_KEYSYM_OFFSET + cp


def _canonical(combo: str) -> str:
    """Normalize a combo for blocklist matching: lowercase, 'control' -> 'ctrl'."""
    parts = [p.strip().lower() for p in combo.split("+")]
    return "+".join("ctrl" if p == "control" else p for p in parts)


_BLOCKED_CANONICAL = frozenset(_canonical(c) for c in BLOCKED_COMBOS)


def parse_combo(combo: str) -> list[int]:
    """Parse e.g. 'ctrl+a' into keysyms, modifiers first, then the key."""
    if _canonical(combo) in _BLOCKED_CANONICAL:
        raise BlockedKeyError(f"Combinación bloqueada: {combo!r}")
    *mods, key = [p.strip().lower() for p in combo.split("+")]
    syms: list[int] = []
    for mod in mods:
        if mod not in MODIFIERS:
            raise ValueError(f"Modificador desconocido: {mod!r}")
        syms.append(MODIFIERS[mod])
    if key in NAMED_KEYS:
        syms.append(NAMED_KEYS[key])
    elif key in MODIFIERS:
        syms.append(MODIFIERS[key])
    elif len(key) == 1:
        syms.append(keysym(key))
    else:
        raise ValueError(f"Tecla desconocida: {key!r}")
    return syms
