"""What one ingest run read from each source.

Rows carry counts, source IDs and this code's own reasons only, never a
publisher's text. A count says what this collector read; a failed or empty
row is not evidence that nothing was published.
"""

from __future__ import annotations

from dataclasses import dataclass

from ingestion.http import FETCH_FAILED, FetchResult


@dataclass
class SourceRow:
    """A fetch result plus, outside a dry run, what capture made of it."""

    source: str
    status: str
    reason: str | None = None
    limit: int | None = None
    received: int | None = None
    new: int | None = None
    revision: int | None = None
    known: int | None = None
    invalid: int | None = None

    @classmethod
    def from_fetch(cls, result: FetchResult) -> "SourceRow":
        row = cls(result.source, result.status, result.reason, result.limit)
        if result.status != FETCH_FAILED:
            row.received = len(result.records)
        return row

    @property
    def at_limit(self) -> bool:
        """The answer filled its bound, so the source may hold more."""
        return (
            self.limit is not None
            and self.received is not None
            and self.received >= self.limit
        )

    @property
    def captured(self) -> int | None:
        if self.new is None or self.revision is None:
            return None
        return self.new + self.revision

    def status_text(self) -> str:
        if self.status == FETCH_FAILED and self.reason:
            return f"{self.status} ({self.reason})"
        if self.at_limit:
            return f"{self.status}, at the limit of {self.limit}"
        return self.status
