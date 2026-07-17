"""Collection-only capture of immutable source observations.

This module deliberately has no analysis, event, prediction, or stream
dependency.  Collection normalizes and deduplicates source records through
``get_or_create_observation`` and stops there.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from core.db import Observation
from core.observations import get_or_create_observation


CollectionStatus = Literal["new", "known", "revision", "invalid"]


@dataclass(frozen=True)
class CollectionOutcome:
    """Outcome for one received item, without retaining invalid input data."""

    status: CollectionStatus
    observation: Observation | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class CollectionBatch:
    """Exact counts and ordered outcomes for one collection batch.

    ``outcomes`` has one entry per received item. ``observations`` contains one
    entry per valid item in the same order, including repeated references when
    an already-known observation is received again.
    """

    received: int
    new: int
    known: int
    revision: int
    invalid: int
    outcomes: tuple[CollectionOutcome, ...]
    observations: tuple[Observation, ...]


_INPUT_ERROR_TYPES = (TypeError, ValueError, UnicodeError, OverflowError)


def collect_leaf(leaf: Mapping[str, Any]) -> CollectionOutcome:
    """Capture one source item without analysis or derived-output writes.

    Invalid input is represented only by its exception class name. Database and
    other operational failures are allowed to propagate rather than being
    misreported as bad source data.
    """

    if not isinstance(leaf, Mapping):
        return CollectionOutcome(status="invalid", error_type="TypeError")

    try:
        observation, created = get_or_create_observation(leaf)
    except _INPUT_ERROR_TYPES as exc:
        return CollectionOutcome(
            status="invalid",
            error_type=type(exc).__name__,
        )

    if not created:
        status: CollectionStatus = "known"
    elif observation.supersedes_id is not None:
        status = "revision"
    else:
        status = "new"
    return CollectionOutcome(status=status, observation=observation)


def collect_batch(leaves: Iterable[Mapping[str, Any]]) -> CollectionBatch:
    """Capture a mixed iterable and return exact ordered collection outcomes."""

    outcomes: list[CollectionOutcome] = []
    observations: list[Observation] = []
    counts = {"new": 0, "known": 0, "revision": 0, "invalid": 0}

    for leaf in leaves:
        outcome = collect_leaf(leaf)
        outcomes.append(outcome)
        counts[outcome.status] += 1
        if outcome.observation is not None:
            observations.append(outcome.observation)

    return CollectionBatch(
        received=len(outcomes),
        new=counts["new"],
        known=counts["known"],
        revision=counts["revision"],
        invalid=counts["invalid"],
        outcomes=tuple(outcomes),
        observations=tuple(observations),
    )
__all__ = [
    "CollectionBatch",
    "CollectionOutcome",
    "CollectionStatus",
    "collect_batch",
    "collect_leaf",
]
