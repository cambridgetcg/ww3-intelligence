"""Shared Unicode control detection for stored metadata and human output."""

from __future__ import annotations

import unicodedata


_BIDI_CONTROLS = {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
_DIRECTIONAL_MARKS = {"\u061c", "\u200e", "\u200f"}


def is_unsafe_control(character: str, *, allow_layout: bool = False) -> bool:
    if character in _DIRECTIONAL_MARKS:
        return True
    if unicodedata.bidirectional(character) in _BIDI_CONTROLS:
        return True
    if unicodedata.category(character) != "Cc":
        return False
    return not (allow_layout and character in {"\n", "\t"})


def contains_unsafe_controls(value: str) -> bool:
    return any(is_unsafe_control(character) for character in value)


__all__ = ["contains_unsafe_controls", "is_unsafe_control"]
