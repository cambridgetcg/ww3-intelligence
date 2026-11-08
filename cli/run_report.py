"""What one ingest run read from each source, for a terminal and for Actions.

Annotations and the step summary carry counts, source IDs and this code's own
reasons only, never a publisher's text. A count says what this collector
read; a failed or empty row is not evidence that nothing was published.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ingestion.http import FETCH_EMPTY, FETCH_FAILED, FetchResult


SUMMARY_COLUMNS = (
    "source", "status", "received", "first-seen", "revisions", "unchanged", "invalid",
)
_LABELS = {"usgs": "USGS", "gdacs": "GDACS"}


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


def in_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def warning_command(title: str, message: str) -> str:
    """One GitHub Actions ``::warning`` workflow command."""
    return f"::warning title={_escape_property(title)}::{_escape_data(message)}"


def warnings_for(kind: str, rows: list[SourceRow]) -> list[str]:
    """A warning per failed or empty feed, or for a failed USGS/GDACS fetch."""
    lines = []
    for row in rows:
        if kind == "news":
            if row.status == FETCH_FAILED:
                lines.append(warning_command(
                    f"News feed failed ({row.source})",
                    f"{row.reason}. Nothing was read from this feed in this run.",
                ))
            elif row.status == FETCH_EMPTY:
                lines.append(warning_command(
                    f"News feed empty ({row.source})",
                    "A well-formed feed with no items. "
                    "Not evidence that nothing was published.",
                ))
        elif row.status == FETCH_FAILED:
            label = _LABELS.get(row.source, row.source)
            lines.append(warning_command(
                f"{label} fetch failed",
                f"{row.reason}. No {label} records were read in this run.",
            ))
    return lines


def _cell(value) -> str:
    if value is None:
        return "–"
    return str(value).replace("|", "\\|")


def step_summary(kind: str, rows: list[SourceRow], *, dry_run: bool) -> str:
    """A markdown table of one run's per-source counts."""
    heading = f"### ingest {kind}"
    if dry_run:
        heading += " (dry run: nothing was stored)"
    lines = [
        heading,
        "",
        "| " + " | ".join(SUMMARY_COLUMNS) + " |",
        "| --- | --- | " + " | ".join(["---:"] * (len(SUMMARY_COLUMNS) - 2)) + " |",
    ]
    for row in rows:
        cells = (
            row.source, row.status_text(), row.received,
            row.new, row.revision, row.known, row.invalid,
        )
        lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
    lines += [
        "",
        "Counts are what this collector read, not what was published. A failed "
        "or empty row is not evidence that nothing was published, and a source "
        "at its limit may hold more than was read.",
        "",
    ]
    return "\n".join(lines) + "\n"


def report_to_actions(
    kind: str, rows: list[SourceRow], *, dry_run: bool, echo,
) -> None:
    """Emit warnings and append the step summary when running under Actions."""
    if in_github_actions():
        for line in warnings_for(kind, rows):
            echo(line)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(step_summary(kind, rows, dry_run=dry_run))
    except OSError as exc:
        # The run's collection stands; only the summary is missing.
        echo(f"Step summary not written ({type(exc).__name__}).")
