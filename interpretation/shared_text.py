"""Deterministic measurement of shared visible text between captured records.

Syndication is the question a reader usually arrives with, and it is not what
this measures. Shared wording is visible and countable; a shared origin is an
inference about the world, and naming the file for the conclusion would put a
claim in the import line that every string below refuses to make.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations

from interpretation.rhetorlint import Passage


# The contract names an arithmetic, not a conclusion. Two records answering
# high here contain much the same visible characters, which is an observation
# about the captures. Copying, a shared wire or origin, a direction of reuse,
# and an order of publication are not visible in a character overlap and are
# not emitted anywhere below.
SHARED_TEXT_VERSION = "ww3.shared-text/v1"
SHARED_TEXT_METHOD = "normalised-token-shingle-set-overlap"

# Every parameter is emitted with the result. A reader who disagrees with one
# must be able to recompute the number under their own value, and a number
# whose method is not in the record cannot be contested.
SHINGLE_TOKENS = 5
MIN_COMPARABLE_SHINGLES = 8
REPORT_THRESHOLD = 0.3
DECISION_MEASURE = "containment"
MEASURE_DECIMALS = 4
MAX_COMPARED_OBSERVATIONS = 8
MAX_TOKENS_PER_OBSERVATION = 4_096
MAX_SHARED_RUN_TOKENS = 24

CAP_OBSERVATIONS = "observation_cap"
CAP_TOKENS = "token_cap"
REASON_BELOW_MINIMUM = "fewer_than_minimum_shingles"

SHARED_TEXT_SCOPE = (
    "Reports how much normalised visible text two captured records share. A high "
    "measure is an observation about characters: it is not a finding that either "
    "record copied the other, that they share an origin, a wire, or an author, or "
    "which was published first. A low measure is not evidence of independence — "
    "two newsrooms can report one event independently in different words, and a "
    "translation of one wire story shares almost none of its original's tokens, so "
    "both read as no shared text here. Pairs below the reporting threshold and "
    "pairs too short to measure are counted rather than dropped, because a pair "
    "that measured low and a pair that was never measured are different claims. "
    "longest_shared_run.normalised_text is comparison output — folded, "
    "punctuation-free, space-joined — and not a quotation from either record."
)

# This table duplicates the one in `interpretation/actors.py`. Importing that
# module's private ranges would bind this contract's arithmetic to another
# contract's guard decisions, and the two must be free to move apart; unifying
# them is a deliberate act, not a side effect of editing either.
_UNSPACED_RANGES = (
    (0x0E00, 0x0EFF),    # Thai, Lao
    (0x0F00, 0x0FFF),    # Tibetan
    (0x1000, 0x109F),    # Myanmar
    (0x1780, 0x17FF),    # Khmer
    (0x19E0, 0x19FF),    # Khmer symbols
    (0x2E80, 0x2FDF),    # CJK and Kangxi radicals
    (0x3005, 0x3007),    # ideographic iteration and closing marks, 〇
    (0x3021, 0x303B),    # Suzhou numerals and ideographic marks
    (0x3040, 0x30FF),    # Hiragana, Katakana
    (0x31F0, 0x31FF),    # Katakana phonetic extensions
    (0x3400, 0x4DBF),    # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0xA9E0, 0xA9FF),    # Myanmar Extended-B
    (0xAA60, 0xAA7F),    # Myanmar Extended-A
    (0xF900, 0xFAFF),    # CJK Compatibility Ideographs
    (0xFF66, 0xFF9F),    # halfwidth Katakana
    (0x1B000, 0x1B16F),  # Kana supplement and extended
    (0x20000, 0x3FFFF),  # CJK ideograph planes 2 and 3
)

_TOKEN_RUN = re.compile(r"\w+")


def _unspaced(character: str) -> bool:
    """Report whether one character belongs to a script written without spaces."""
    point = ord(character)
    return any(low <= point <= high for low, high in _UNSPACED_RANGES)


def comparison_tokens(text: str) -> list[str]:
    """Fold one visible text into tokens used only for comparison."""
    # Folding is a comparison step, never a storage step: the casefile keeps the
    # record's exact visible passage, and nothing produced here replaces it.
    folded = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    for match in _TOKEN_RUN.finditer(folded):
        buffer: list[str] = []
        # A run of unspaced script is one `\w+` match however many words it
        # holds, so a whole Han sentence would otherwise be a single token and
        # answer with the structural zero that hides every shared passage in it.
        for character in match.group(0):
            if _unspaced(character):
                if buffer:
                    tokens.append("".join(buffer))
                    buffer = []
                tokens.append(character)
            else:
                buffer.append(character)
        if buffer:
            tokens.append("".join(buffer))
    return tokens


def _shingles(tokens: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """Cut one token run into overlapping fixed-length shingles."""
    return tuple(
        tokens[index:index + SHINGLE_TOKENS]
        for index in range(len(tokens) - SHINGLE_TOKENS + 1)
    )


def _round(value: float) -> float:
    """Round one measure to the precision the method statement declares."""
    return round(value, MEASURE_DECIMALS)


@dataclass(frozen=True, slots=True)
class TextStream:
    observation_id: int
    passage_ids: tuple[str, ...]
    segments: tuple[tuple[str, ...], ...]
    shingles: tuple[tuple[tuple[str, ...], ...], ...]
    distinct: frozenset[tuple[str, ...]]
    token_limit_hit: bool

    @property
    def token_count(self) -> int:
        return sum(len(segment) for segment in self.segments)

    @property
    def comparable(self) -> bool:
        return len(self.distinct) >= MIN_COMPARABLE_SHINGLES

    def as_dict(self) -> dict:
        return {
            "observation_id": self.observation_id,
            "passage_ids": list(self.passage_ids),
            "token_count": self.token_count,
            "distinct_shingles": len(self.distinct),
            "comparable": self.comparable,
            "token_limit_hit": self.token_limit_hit,
        }


def build_stream(observation_id: int, passages: Iterable[Passage]) -> TextStream:
    """Fold one record's visible passages into a bounded comparison stream."""
    passage_ids: list[str] = []
    segments: list[tuple[str, ...]] = []
    remaining = MAX_TOKENS_PER_OBSERVATION
    limit_hit = False
    for passage in passages:
        if remaining <= 0:
            limit_hit = True
            break
        tokens = comparison_tokens(passage.text)
        if len(tokens) > remaining:
            tokens = tokens[:remaining]
            limit_hit = True
        # A passage id is recorded only where its text entered the stream, so
        # the emitted ids name exactly what was compared.
        passage_ids.append(passage.passage_id)
        segments.append(tuple(tokens))
        remaining -= len(tokens)
    shingles = tuple(_shingles(segment) for segment in segments)
    return TextStream(
        observation_id=observation_id,
        passage_ids=tuple(passage_ids),
        segments=tuple(segments),
        shingles=shingles,
        distinct=frozenset(
            shingle for segment in shingles for shingle in segment
        ),
        token_limit_hit=limit_hit,
    )


def _longest_shared_run(
    stream: TextStream, shared: frozenset[tuple[str, ...]]
) -> dict | None:
    """Locate the longest unbroken run of shared shingles in one stream."""
    best = (0, 0, 0)
    for index, segment in enumerate(stream.shingles):
        start = None
        length = 0
        for position, shingle in enumerate(segment):
            if shingle not in shared:
                start, length = None, 0
                continue
            if start is None:
                start = position
            length += 1
            # Strictly greater keeps the first run of a tied length, so the
            # emitted sample does not depend on iteration luck.
            if length > best[0]:
                best = (length, index, start)
    length, index, start = best
    if not length:
        return None
    run = stream.segments[index][start:start + length + SHINGLE_TOKENS - 1]
    return {
        "observation_id": stream.observation_id,
        "passage_id": stream.passage_ids[index],
        "token_count": len(run),
        "truncated": len(run) > MAX_SHARED_RUN_TOKENS,
        "normalised_text": " ".join(run[:MAX_SHARED_RUN_TOKENS]),
    }


def compare_streams(first: TextStream, second: TextStream) -> dict:
    """Measure the shingle overlap between two comparison streams."""
    counts = [len(first.distinct), len(second.distinct)]
    pair = {
        "observation_ids": [first.observation_id, second.observation_id],
        "distinct_shingles": counts,
    }
    # Below the minimum, one boilerplate shingle would carry a whole measure.
    # Refusing to compute is the honest answer; reporting the refusal keeps it
    # from reading as a measured zero.
    if not (first.comparable and second.comparable):
        return {**pair, "comparable": False, "reason": REASON_BELOW_MINIMUM}
    shared = first.distinct & second.distinct
    # The threshold is applied to the emitted number rather than to fuller
    # precision behind it, so a reader recomputing from the record reaches the
    # same verdict this did.
    containment = _round(len(shared) / min(counts))
    union = counts[0] + counts[1] - len(shared)
    return {
        **pair,
        "comparable": True,
        "shared_shingles": len(shared),
        "containment": containment,
        "jaccard": _round(len(shared) / union),
        "reaches_threshold": containment >= REPORT_THRESHOLD,
        "token_limit_hit": first.token_limit_hit or second.token_limit_hit,
        "longest_shared_run": _longest_shared_run(first, shared),
    }


def _method() -> dict:
    """State everything a reader needs to recompute or contest a measure."""
    return {
        "name": SHARED_TEXT_METHOD,
        "shingle_tokens": SHINGLE_TOKENS,
        "decision_measure": DECISION_MEASURE,
        "report_threshold": REPORT_THRESHOLD,
        "minimum_comparable_shingles": MIN_COMPARABLE_SHINGLES,
        "normalisation": (
            "Compared text is NFKC-normalised, case-folded, and split at non-word "
            "characters; each character of an unspaced script is one token. This "
            "folding exists for the comparison only and replaces nothing: the "
            "record's exact visible passages are stored and emitted unchanged."
        ),
        "arithmetic": (
            "A shingle is shingle_tokens consecutive tokens taken inside a single "
            "passage; no shingle spans two passages, and each distinct shingle "
            "counts once. containment = shared / smaller distinct set; jaccard = "
            "shared / union. distinct_shingles is aligned with observation_ids, so "
            "both measures are recomputable from the emitted integers, which are "
            "exact where the measures are rounded."
        ),
        "measure_decimals": MEASURE_DECIMALS,
    }


def shared_visible_text(
    records: Iterable[tuple[int, tuple[Passage, ...]]],
) -> dict:
    """Report shared visible text across a bounded set of captured records."""
    # Ordering by observation id, not by caller order, is what makes the same
    # set of records answer the same way however they were listed — including
    # which records the observation cap leaves out.
    ordered = sorted(records, key=lambda record: record[0])
    compared = ordered[:MAX_COMPARED_OBSERVATIONS]
    deferred = [record[0] for record in ordered[MAX_COMPARED_OBSERVATIONS:]]
    streams = [
        build_stream(observation_id, passages) for observation_id, passages in compared
    ]
    results = [compare_streams(*pair) for pair in combinations(streams, 2)]

    comparable = [item for item in results if item["comparable"]]
    reported = [item for item in comparable if item["reaches_threshold"]]
    below = [item for item in comparable if not item["reaches_threshold"]]
    reported.sort(key=lambda item: (
        -item["containment"], -item["jaccard"], item["observation_ids"],
    ))
    caps = []
    if deferred:
        caps.append(CAP_OBSERVATIONS)
    if any(stream.token_limit_hit for stream in streams):
        caps.append(CAP_TOKENS)
    return {
        "contract": SHARED_TEXT_VERSION,
        "method": _method(),
        "records": [stream.as_dict() for stream in streams],
        "pairs": reported,
        "pairs_compared": len(results),
        "pairs_reported": len(reported),
        "pairs_below_threshold": len(below),
        "highest_containment_below_threshold": (
            max(item["containment"] for item in below) if below else None
        ),
        "pairs_not_comparable": [
            item for item in results if not item["comparable"]
        ],
        "observations_not_compared": deferred,
        "bounds": {
            "max_observations": MAX_COMPARED_OBSERVATIONS,
            "max_tokens_per_observation": MAX_TOKENS_PER_OBSERVATION,
            "max_shared_run_tokens": MAX_SHARED_RUN_TOKENS,
            "caps_hit": caps,
        },
        "status": "bounded" if caps else "complete",
        "scope": SHARED_TEXT_SCOPE,
    }


__all__ = [
    "CAP_OBSERVATIONS", "CAP_TOKENS", "DECISION_MEASURE",
    "MAX_COMPARED_OBSERVATIONS", "MAX_SHARED_RUN_TOKENS",
    "MAX_TOKENS_PER_OBSERVATION", "MEASURE_DECIMALS", "MIN_COMPARABLE_SHINGLES",
    "REASON_BELOW_MINIMUM", "REPORT_THRESHOLD", "SHARED_TEXT_METHOD",
    "SHARED_TEXT_SCOPE", "SHARED_TEXT_VERSION", "SHINGLE_TOKENS", "TextStream",
    "build_stream", "compare_streams", "comparison_tokens", "shared_visible_text",
]
