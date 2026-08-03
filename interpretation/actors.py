r"""Conservative actor identity mentions over deterministic visible passages.

A mention is a visible occurrence of registered characters. It is never a
finding that the actor acted, spoke, or is the subject of the sentence.

The boundary guard around an alias is inferred from the alias's own
characters, so `registry/actors.json` keeps its shape and declares no script.
`\w` is Unicode-aware, which makes `(?<!\w)ALIAS(?!\w)` right for a script
that separates words with spaces and unsatisfiable for one that does not:
every neighbour of 中国 in 中国政府表示 is itself a word character, so the
guarded pattern answers zero for the whole of Han, Kana, Thai, Lao, Khmer,
Myanmar, and Tibetan text. A zero produced by the matcher's own shape reads
in the output exactly like a passage that names nobody, and that silent,
systematic absence is the failure this module exists to refuse.

Guards are therefore decided per side. Korean spaces its words but
agglutinates particles onto the noun (러시아는, 러시아에서), so Hangul keeps
its leading guard and drops the trailing one. An alias mixing scripts takes
the weakest guard of the scripts it contains, because it is read inside the
text of its unspaced part, where 华为 Technologies的声明 offers the Latin edge
no delimiter either.

Dropping a guard trades a silent false negative for a visible false positive:
unguarded 中国 also occurs inside 中国人 and across the seam of 美中国际. Those
occurrences are emitted rather than suppressed, and each mention records the
guards its alias supported plus whether a word character abuts the span. The
adjacency flags are exactly the test a dropped guard would have applied, so
the stricter reading stays reconstructable from the output.

The same silence runs one layer below the guards. A publisher may serve
러시아는 말했다 as eight code points or, from a macOS filesystem or a
decomposing CMS, as eighteen; the bytes differ, every reader sees one
sentence, and an alias compiled from one spelling answers zero for the other
exactly as a passage naming nobody does. A passage cannot be normalized to
meet the alias, because its offsets are the evidence a casefile quotes and a
decomposed copy is a different length. So the alias is expanded instead, into
the canonically equivalent spellings of the same characters, and each is
searched against the text precisely as captured. Every mention names the spelling that matched,
because an alias found in a form the registry does not hold is a fact about
the record and not about the actor.

Reaching decomposed text also puts the guards within reach of the marks it
carries, and `\w` matches no mark, so a guard left to the pattern alone would
read the diacritic in Hà as a word delimiter and hand back Ha. A guard is
therefore a predicate over what abuts the span, applied whether or not the
pattern could express it. The mirror case is not solved: a Hangul mention
keeps no trailing guard, so a decomposed passage can end a span before a
final Jamo that belongs to its syllable, which the adjacency flag records as
the word character it is.

Two limits stay unhandled and named. Arabic and Hebrew space their words and
are guarded here, but attached prefixes (و, ال, ה) hide a bare alias behind an
unspaced particle; handling them would take the mirror of the Hangul
asymmetry, and leaving them costs some occurrences rather than all of them.
Scripts absent from the table below, such as Javanese, Balinese, and Yi, are
treated as spaced and keep the structural zero described above.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left
from dataclasses import dataclass
from functools import lru_cache

from core.registry import ALIAS_FORMS, ActorProfile, actor_profiles
from interpretation.rhetorlint import Passage


# v2 guards an alias by the scripts it is written in, so a passage in an
# unspaced script answers with mentions where v1 could answer only zero, and
# every mention names the guard behind it. v3 searches each canonically
# equivalent spelling of an alias, so decomposed text answers where v2 could
# answer only zero. Both changes move counts upward, and a reader comparing
# two runs needs the contract to say which arithmetic produced them.
ACTOR_MENTION_VERSION = "ww3.actor-mentions/v3"
MAX_MENTION_CANDIDATES_PER_PASSAGE = 4_096
MAX_MENTIONS_PER_PASSAGE = 256
MAX_MENTIONS_PER_OBSERVATION = 512
MAX_CACHED_ALIAS_PATTERNS = 4_096

MATCH_MODE_BOUNDARY_GUARDED = "word-boundary-guarded"
MATCH_MODE_LEADING_GUARDED = "leading-boundary-guarded"
MATCH_MODE_UNGUARDED = "unguarded-substring"

# The mode is the single decision; the guards follow from it, so no alias can
# be compiled behind a pair of guards no mode names.
_GUARDS_BY_MODE = {
    MATCH_MODE_BOUNDARY_GUARDED: (True, True),
    MATCH_MODE_LEADING_GUARDED: (True, False),
    MATCH_MODE_UNGUARDED: (False, False),
}
MATCH_MODES = tuple(_GUARDS_BY_MODE)

# Ranges rather than script names, so a reviewer can check the table against
# the Unicode charts. These scripts run words together, so a word character on
# either side of an alias is the rule and neither guard can hold.
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

# Hangul is space-delimited between words, so its leading guard survives; a
# particle attaches to the noun with no space, so its trailing guard cannot.
_SUFFIX_BOUND_RANGES = (
    (0x1100, 0x11FF),    # Hangul Jamo
    (0x3130, 0x318F),    # Hangul Compatibility Jamo
    (0xA960, 0xA97F),    # Hangul Jamo Extended-A
    (0xAC00, 0xD7FF),    # Hangul syllables and Jamo Extended-B
    (0xFFA0, 0xFFDC),    # halfwidth Jamo
)

_WORD_CHARACTER = re.compile(r"\w")


def _in_ranges(character: str, ranges: tuple[tuple[int, int], ...]) -> bool:
    """Report whether one character falls in an inclusive range table."""
    point = ord(character)
    return any(low <= point <= high for low, high in ranges)


def _alias_match_mode(alias: str) -> str:
    """Choose the strongest guard an alias's own scripts can satisfy."""
    # Every character counts, not only the edges: a mixed alias sits inside the
    # text of its unspaced part, where the delimiter its Latin edge asks for is
    # one the surrounding script never writes.
    if any(_in_ranges(character, _UNSPACED_RANGES) for character in alias):
        return MATCH_MODE_UNGUARDED
    if any(_in_ranges(character, _SUFFIX_BOUND_RANGES) for character in alias):
        return MATCH_MODE_LEADING_GUARDED
    return MATCH_MODE_BOUNDARY_GUARDED


@lru_cache(maxsize=MAX_CACHED_ALIAS_PATTERNS)
def _alias_pattern(alias: str, case_sensitive: bool) -> tuple[re.Pattern[str], str]:
    """Compile one alias spelling behind the guards its characters support."""
    # Keyed on the spelling: the registry bounds how many aliases exist and an
    # expansion is bounded per alias, but nothing bounds how many passages they
    # are scanned against. The mode follows the spelling rather than the
    # registered alias, because decomposing Hangul yields Jamo and decomposing
    # a Latin letter yields a mark its own guard must be chosen for.
    mode = _alias_match_mode(alias)
    leading, trailing = _GUARDS_BY_MODE[mode]
    pattern = re.compile(
        (r"(?<!\w)" if leading else "")
        + re.escape(alias)
        + (r"(?!\w)" if trailing else ""),
        0 if case_sensitive else re.IGNORECASE,
    )
    return pattern, mode


def _abuts_word_character(text: str, index: int) -> bool:
    """Apply, as a record, the exact test a dropped guard would have made."""
    if not 0 <= index < len(text):
        return False
    # A mark exists only because a base character precedes it, so a position
    # holding one is inside a written word. `\w` matches no mark at all, which
    # is why `(?<!\w)Ha(?!\w)` reads decomposed Hà as a delimited word and a
    # guard cannot be left to the pattern alone.
    return (
        _WORD_CHARACTER.match(text, index) is not None
        or unicodedata.category(text[index]).startswith("M")
    )


@dataclass(frozen=True, slots=True)
class ActorMention:
    actor_id: str
    canonical_name: str
    passage_id: str
    field: str
    actual: str
    start: int
    end: int
    match_mode: str
    alias_form: str
    word_character_before: bool
    word_character_after: bool

    def as_dict(self) -> dict:
        leading, trailing = _GUARDS_BY_MODE[self.match_mode]
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
            # A consumer that cannot tell a guarded Latin match from an
            # unguarded Han substring cannot weigh them apart, and a count it
            # cannot interrogate is not evidence. alias_form says whether the
            # span holds the registered characters or an equivalent spelling
            # of them; the span itself indexes the passage as captured.
            "match": {
                "mode": self.match_mode,
                "alias_form": self.alias_form,
                "leading_guard": leading,
                "trailing_guard": trailing,
                "adjacent_word_character": {
                    "before": self.word_character_before,
                    "after": self.word_character_after,
                },
            },
        }


@dataclass(frozen=True, slots=True)
class ActorMentionScan:
    mentions: tuple[ActorMention, ...]
    candidates_examined: int
    passages_scanned: int
    truncated: bool

    def metadata(self) -> dict:
        # Every mode and spelling is listed at zero, because one that matched
        # nothing and one no registered alias could reach are different claims.
        modes = dict.fromkeys(MATCH_MODES, 0)
        forms = dict.fromkeys(ALIAS_FORMS, 0)
        for mention in self.mentions:
            modes[mention.match_mode] += 1
            forms[mention.alias_form] += 1
        return {
            "contract": ACTOR_MENTION_VERSION,
            "mentions_emitted": len(self.mentions),
            "match_modes": modes,
            "alias_forms": forms,
            "candidates_examined": self.candidates_examined,
            "passages_scanned": self.passages_scanned,
            "truncated": self.truncated,
            "limits": {
                "candidates_per_passage": MAX_MENTION_CANDIDATES_PER_PASSAGE,
                "mentions_per_passage": MAX_MENTIONS_PER_PASSAGE,
                "mentions_per_observation": MAX_MENTIONS_PER_OBSERVATION,
            },
            "scope": (
                "Visible alias occurrences under the boundary guards each alias's own "
                "characters can satisfy, searched in every canonically equivalent "
                "spelling of the alias against the passage exactly as captured. An "
                "unguarded-substring mention may sit inside a longer run of unspaced "
                "text; it is a visible occurrence, not a claim that the actor is the "
                "subject of the passage. adjacent_word_character reports what touches "
                "each side of the span, and alias_form which spelling matched."
            ),
        }


def _matches(profile: ActorProfile, passage: Passage):
    candidates = []
    for alias in profile.alias_forms:
        # A zero-width alias would match between every pair of characters; the
        # registry rejects one, a hand-built profile is not checked.
        if not alias.text:
            continue
        pattern, mode = _alias_pattern(alias.text, alias.case_sensitive)
        leading, trailing = _GUARDS_BY_MODE[mode]
        for match in pattern.finditer(passage.text):
            # The guard a mode declares is enforced here as well as compiled,
            # because the pattern's `\w` cannot see the marks a decomposed
            # passage carries. A mode that declares no guard keeps none, and
            # its mentions are labelled rather than suppressed.
            if (leading and _abuts_word_character(passage.text, match.start() - 1)) or (
                trailing and _abuts_word_character(passage.text, match.end())
            ):
                continue
            candidates.append(
                (match.start(), match.end(), match.group(0), mode, alias.form)
            )
    # Prefer the longest alias at a shared start and remove duplicate spans.
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[1]))
    seen = set()
    for start, end, actual, mode, form in candidates:
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
            match_mode=mode,
            alias_form=form,
            word_character_before=_abuts_word_character(passage.text, start - 1),
            word_character_after=_abuts_word_character(passage.text, end),
        )


def text_mentions_actor(actor_id: str, text: str) -> bool:
    """Use the same overlap-resolved aliases as casefile visibility."""
    # An unguarded-substring occurrence counts as visible here. Refusing it
    # would move the structural blind spot from the casefile into assessment
    # validation, where the reader sees no span and no mode at all.
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
    "ACTOR_MENTION_VERSION", "MATCH_MODES", "MATCH_MODE_BOUNDARY_GUARDED",
    "MATCH_MODE_LEADING_GUARDED", "MATCH_MODE_UNGUARDED",
    "MAX_CACHED_ALIAS_PATTERNS", "MAX_MENTION_CANDIDATES_PER_PASSAGE",
    "MAX_MENTIONS_PER_OBSERVATION", "MAX_MENTIONS_PER_PASSAGE", "ActorMention",
    "ActorMentionScan", "find_actor_mentions", "scan_actor_mentions",
    "text_mentions_actor",
]
