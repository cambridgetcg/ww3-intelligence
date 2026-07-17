"""Conservative actor identity mentions over deterministic visible passages."""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass

from core.registry import ActorProfile, actor_profiles
from interpretation.rhetorlint import Passage


ACTOR_MENTION_VERSION = "ww3.actor-mentions/v1"
MAX_MENTION_CANDIDATES_PER_PASSAGE = 4_096
MAX_MENTIONS_PER_PASSAGE = 256
MAX_MENTIONS_PER_OBSERVATION = 512


@dataclass(frozen=True, slots=True)
class ActorMention:
    actor_id: str
    canonical_name: str
    passage_id: str
    field: str
    actual: str
    start: int
    end: int

    def as_dict(self) -> dict:
        return {
            "actor_id": self.actor_id,
            "canonical_name": self.canonical_name,
            "passage_id": self.passage_id,
            "field": self.field,
            "actual": self.actual,
            "span": {
                "start": self.start,
                "end": self.end,
                "offset_encoding": "unicode-code-point",
            },
        }


@dataclass(frozen=True, slots=True)
class ActorMentionScan:
    mentions: tuple[ActorMention, ...]
    candidates_examined: int
    passages_scanned: int
    truncated: bool

    def metadata(self) -> dict:
        return {
            "contract": ACTOR_MENTION_VERSION,
            "mentions_emitted": len(self.mentions),
            "candidates_examined": self.candidates_examined,
            "passages_scanned": self.passages_scanned,
            "truncated": self.truncated,
            "limits": {
                "candidates_per_passage": MAX_MENTION_CANDIDATES_PER_PASSAGE,
                "mentions_per_passage": MAX_MENTIONS_PER_PASSAGE,
                "mentions_per_observation": MAX_MENTIONS_PER_OBSERVATION,
            },
        }


def _matches(profile: ActorProfile, passage: Passage):
    candidates = []
    for alias, case_sensitive in profile.aliases:
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", flags)
        for match in pattern.finditer(passage.text):
            candidates.append((match.start(), match.end(), match.group(0)))
    # Prefer the longest alias at a shared start and remove duplicate spans.
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[1]))
    seen = set()
    for start, end, actual in candidates:
        if (start, end) in seen:
            continue
        seen.add((start, end))
        yield ActorMention(
            actor_id=profile.actor_id,
            canonical_name=profile.canonical_name,
            passage_id=passage.passage_id,
            field=passage.field,
            actual=actual,
            start=start,
            end=end,
        )


def text_mentions_actor(actor_id: str, text: str) -> bool:
    """Use the same overlap-resolved aliases as casefile visibility."""
    if not isinstance(text, str) or not text:
        return False
    encoded = text.encode("utf-8")
    passage = Passage(
        passage_id="actor-support-check",
        field="claim.actual",
        text=text,
        truncated=False,
        utf8_bytes=len(encoded),
        input_sha256="not-persisted",
    )
    mentions, _, _ = _scan_passage(passage, actor_profiles())
    return any(item.actor_id == actor_id for item in mentions)


def _scan_passage(
    passage: Passage,
    profiles: tuple[ActorProfile, ...],
) -> tuple[list[ActorMention], int, bool]:
    candidates = []
    candidate_limit_hit = False
    for profile in profiles:
        for mention in _matches(profile, passage):
            if len(candidates) >= MAX_MENTION_CANDIDATES_PER_PASSAGE:
                candidate_limit_hit = True
                break
            candidates.append(mention)
        if candidate_limit_hit:
            break

    # Prefer the longest identity over a nested alias. Accepted intervals stay
    # start-sorted so each overlap test touches only its two neighbours.
    candidates.sort(key=lambda item: (
        -(item.end - item.start), item.start, item.end, item.actor_id,
    ))
    accepted: list[ActorMention] = []
    starts: list[int] = []
    mention_limit_hit = False
    for candidate in candidates:
        position = bisect_left(starts, candidate.start)
        overlaps_previous = (
            position > 0 and accepted[position - 1].end > candidate.start
        )
        overlaps_next = (
            position < len(accepted) and candidate.end > accepted[position].start
        )
        if overlaps_previous or overlaps_next:
            continue
        if len(accepted) >= MAX_MENTIONS_PER_PASSAGE:
            mention_limit_hit = True
            break
        starts.insert(position, candidate.start)
        accepted.insert(position, candidate)
    return accepted, len(candidates), candidate_limit_hit or mention_limit_hit


def scan_actor_mentions(passages: tuple[Passage, ...]) -> ActorMentionScan:
    mentions: list[ActorMention] = []
    profiles = actor_profiles()
    candidates_examined = 0
    passages_scanned = 0
    truncated = False
    for passage in passages:
        accepted, candidate_count, passage_truncated = _scan_passage(passage, profiles)
        passages_scanned += 1
        candidates_examined += candidate_count
        remaining = MAX_MENTIONS_PER_OBSERVATION - len(mentions)
        mentions.extend(accepted[:remaining])
        if passage_truncated or len(accepted) > remaining:
            truncated = True
        if len(mentions) >= MAX_MENTIONS_PER_OBSERVATION:
            if passages_scanned < len(passages):
                truncated = True
            break
    mentions.sort(key=lambda item: (item.passage_id, item.start, item.end, item.actor_id))
    return ActorMentionScan(
        mentions=tuple(mentions),
        candidates_examined=candidates_examined,
        passages_scanned=passages_scanned,
        truncated=truncated,
    )


def find_actor_mentions(passages: tuple[Passage, ...]) -> tuple[ActorMention, ...]:
    return scan_actor_mentions(passages).mentions


__all__ = [
    "ACTOR_MENTION_VERSION", "MAX_MENTION_CANDIDATES_PER_PASSAGE",
    "MAX_MENTIONS_PER_OBSERVATION", "MAX_MENTIONS_PER_PASSAGE", "ActorMention",
    "ActorMentionScan", "find_actor_mentions", "scan_actor_mentions",
    "text_mentions_actor",
]
