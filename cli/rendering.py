"""Small terminal-safety helpers for untrusted source-provided text."""

from __future__ import annotations

from core.text_safety import is_unsafe_control


def terminal_text(value) -> str:
    """Replace terminal controls and bidi overrides while preserving line breaks."""
    output = []
    for character in str(value):
        if is_unsafe_control(character, allow_layout=True):
            output.append("�")
        else:
            output.append(character)
    return "".join(output)


def terminal_line(value) -> str:
    """Return one whitespace-normalized, terminal-safe display line."""
    return " ".join(terminal_text(value).split())


__all__ = ["terminal_line", "terminal_text"]
