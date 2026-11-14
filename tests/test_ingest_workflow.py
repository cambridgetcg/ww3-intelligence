"""The scheduled ingest workflow's time limits, read from the file itself.

A fetch's own timeouts do not bound a whole answer, so an endpoint that keeps
sending slowly can hold its step until some limit stops it.  If that is the
job's limit, the job is cancelled before the guard and the save, and every
row the run captured is lost.  So each source's step carries its own limit
and may fail alone, and the job's limit leaves the guard and the save room
after all three have used theirs.  The runner stops a step by signalling its
shell alone, so each step's command replaces that shell (exec); otherwise the
collector would keep writing to the ledger while the later steps run.

No YAML parser is installed here, so this reads only the ingest job's own
keys and each step's top-level keys, by indentation, skipping comments,
blank lines and the bodies of block scalars.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ww3-ingest.yml"
INGEST_RUN = "python ww3.py ingest "
EXEC = "exec "
# The guard and the save need seconds; this is room, not a measurement.
HEADROOM_MINUTES = 5


def _scalar(text: str) -> str:
    value = re.sub(r"\s+#.*$", "", text).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _ingest_job() -> tuple[dict[str, str], list[dict[str, str]]]:
    """The ingest job's scalar keys, and each of its steps' scalar keys, in order."""
    job: dict[str, str] = {}
    steps: list[dict[str, str]] = []
    in_job = in_steps = False
    block_indent: int | None = None
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if block_indent is not None:
            if not stripped or indent > block_indent:
                continue
            block_indent = None
        if not stripped or stripped.startswith("#"):
            continue
        if indent <= 2:
            in_job, in_steps = (indent == 2 and stripped == "ingest:"), False
            continue
        if not in_job:
            continue
        if indent == 4:
            in_steps = stripped == "steps:"
            key, _, value = stripped.partition(":")
            job[key] = _scalar(value)
        elif in_steps and indent == 6 and stripped.startswith("- "):
            steps.append({})
            stripped, indent = stripped[2:].lstrip(), 8
        if in_steps and indent == 8 and steps:
            key, _, value = stripped.partition(":")
            steps[-1][key] = _scalar(value)
        if re.search(r":\s*[|>][-+]?\d*\s*(#.*)?$", stripped):
            block_indent = indent
    return job, steps


def _ingest_steps(steps: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        step for step in steps
        if step.get("run", "").removeprefix(EXEC).startswith(INGEST_RUN)
    ]


def test_each_ingest_step_has_its_own_limit_and_may_fail_alone():
    _, steps = _ingest_job()
    ingest = _ingest_steps(steps)

    assert [step.get("id") for step in ingest] == ["usgs", "news", "gdacs"]
    for step in ingest:
        assert step.get("continue-on-error") == "true", step["id"]
        assert step.get("timeout-minutes", "").isdigit(), (
            f"{step['id']} has no timeout-minutes of its own"
        )
        assert int(step["timeout-minutes"]) > 0, step["id"]
        # Plain words only after exec: a pipe or a background job would leave the
        # collector in a child of the shell, which the runner's signal misses.
        assert re.fullmatch(re.escape(EXEC + INGEST_RUN) + r"[\w .=-]+", step["run"]), (
            f"{step['id']} does not replace its shell with the collector (exec)"
        )


def test_the_job_limit_leaves_the_guard_and_the_save_room_after_every_source():
    job, steps = _ingest_job()
    ingest = _ingest_steps(steps)
    limits = [step.get("timeout-minutes", "") for step in ingest]
    assert len(limits) == 3 and all(limit.isdigit() for limit in limits), limits

    assert job.get("timeout-minutes", "").isdigit()
    assert int(job["timeout-minutes"]) >= sum(map(int, limits)) + HEADROOM_MINUTES

    last_source = max(steps.index(step) for step in ingest)
    guard = next(i for i, step in enumerate(steps) if step.get("id") == "guard")
    save = next(
        i for i, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/cache/save@")
    )
    assert last_source < guard < save
