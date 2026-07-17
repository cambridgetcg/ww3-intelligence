# tests/test_cli.py
import os
import stat
import json
import builtins
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock
from click.testing import CliRunner
from cli.commands import cli


def test_cli_list_empty():
    runner = CliRunner()
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0


def test_cli_add_event():
    runner = CliRunner()
    result = runner.invoke(cli, [
        "add-event",
        "--title", "Synthetic Seismic M4.3",
        "--date", "2026-03-03",
        "--category", "seismic",
        "--location", "Test Region",
        "--significance", "8",
        "--summary", "Synthetic seismic event used for a CLI test",
    ])
    assert result.exit_code == 0
    assert "Created" in result.output


def test_cli_show_event():
    runner = CliRunner()
    runner.invoke(cli, [
        "add-event", "--title", "Test Event", "--category", "seismic"
    ])
    result = runner.invoke(cli, ["show", "1"])
    assert result.exit_code == 0
    assert "Test Event" in result.output
    assert "NO PROVENANCE" in result.output


def test_cli_show_missing_event():
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "999"])
    assert result.exit_code == 0
    assert "not found" in result.output.lower()


def test_cli_show_labels_synthetic_event_before_scores():
    from core.db import AnalysisRun, Event
    from core.observations import get_or_create_observation

    event = Event.create(
        title="Synthetic Event", date="2026-03-03", category="other",
        classification="reported", significance=9,
    )
    observation, _ = get_or_create_observation({
        "source": "fixture",
        "source_record_id": "synthetic-1",
        "type": "test",
        "raw_text": "synthetic fixture",
        "synthetic": True,
    })
    AnalysisRun.create(observation=observation, event=event, status="completed")

    result = CliRunner().invoke(cli, ["show", str(event.id)])

    assert result.exit_code == 0
    assert "SYNTHETIC FIXTURE" in result.output
    assert result.output.index("SYNTHETIC FIXTURE") < result.output.index("Significance")


def test_cli_predict():
    runner = CliRunner()
    runner.invoke(cli, ["add-event", "--title", "Test", "--category", "seismic"])
    result = runner.invoke(cli, [
        "predict", "1",
        "--probability", "35",
        "--reasoning", "Shallow depth near facility",
    ])
    assert result.exit_code == 0
    assert "35" in result.output


def test_cli_resolve():
    runner = CliRunner()
    runner.invoke(cli, ["add-event", "--title", "Test", "--category", "seismic"])
    runner.invoke(cli, [
        "predict", "1",
        "--probability", "35",
        "--reasoning", "Test reasoning",
    ])
    result = runner.invoke(cli, [
        "resolve", "1",
        "--outcome", "Confirmed natural earthquake",
        "--did-not-occur",
    ])
    assert result.exit_code == 0
    assert "Brier" in result.output or "brier" in result.output.lower()


def test_cli_list_shows_events():
    runner = CliRunner()
    runner.invoke(cli, ["add-event", "--title", "Event A", "--category", "seismic"])
    runner.invoke(cli, ["add-event", "--title", "Event B", "--category", "military"])
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0
    assert "Event A" in result.output
    assert "Event B" in result.output
    assert "NO-PROVENANCE" in result.output


def test_cli_ingest_usgs_collects_without_analysis():
    """Default source ingest stops after immutable observation capture."""
    fake_leaves = [
        {
            "source": "usgs",
            "type": "seismic",
            "raw_text": '{"mag": 5.1, "place": "Test Location"}',
            "geo_lat": 35.0,
            "geo_lon": 51.0,
            "location_name": "Test Location",
            "confidence": "high",
        }
    ]
    runner = CliRunner()
    real_import = builtins.__import__

    def no_analysis_import(name, *args, **kwargs):
        if name == "core.pipeline":
            raise AssertionError("default ingest imported the analysis pipeline")
        return real_import(name, *args, **kwargs)

    with runner.isolated_filesystem():
        with patch("ingestion.seismic_feed.fetch_seismic_events", return_value=fake_leaves) as mock_fetch, \
             patch.object(builtins, "__import__", side_effect=no_analysis_import), \
             patch("core.stream.StreamWriter.write") as stream_write:
            result = runner.invoke(cli, ["ingest", "usgs"])
        assert result.exit_code == 0, result.output
        mock_fetch.assert_called_once_with(limit=200)
        assert "Received 1 source record" in result.output
        assert "Captured 1 new observation" in result.output
        assert "No model analysis was requested" in result.output
        stream_write.assert_not_called()

    from core.db import AnalysisRun, Event, Observation
    assert Observation.select().count() == 1
    assert AnalysisRun.select().count() == 0
    assert Event.select().count() == 0
    assert not Path(os.environ["WW3_STREAM_PATH"]).exists()


def test_cli_ingest_dry_run_does_not_initialize_database():
    fake_leaves = [{"source": "usgs", "type": "seismic", "raw_text": "{}"}]
    runner = CliRunner()

    with patch("cli.commands.init_db") as init, \
         patch("ingestion.seismic_feed.fetch_seismic_events", return_value=fake_leaves):
        result = runner.invoke(cli, ["ingest", "usgs", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Fetched 1 source record" in result.output
    assert "No database open, storage write, or model analysis" in result.output
    init.assert_not_called()


def test_cli_ingest_gdacs_collects_bounded_hazard_observation():
    from core.db import AnalysisRun, Event, Observation

    leaf = {
        "source": "gdacs",
        "source_record_id": "VO:1:1",
        "type": "hazard",
        "raw_text": json.dumps({"title": "Source alert", "alert_level": "Orange"}),
    }
    with patch("ingestion.gdacs_feed.fetch_gdacs_events", return_value=[leaf]) as fetch:
        result = CliRunner().invoke(cli, ["ingest", "gdacs"])

    assert result.exit_code == 0, result.output
    fetch.assert_called_once_with(limit=100)
    assert Observation.select().count() == 1
    assert AnalysisRun.select().count() == 0
    assert Event.select().count() == 0
    assert "No model analysis was requested" in result.output


def test_cli_news_ingest_reports_partial_and_total_source_failure():
    from core.db import Observation

    feeds = {
        "one": "https://example.test/one.xml",
        "two": "https://example.test/two.xml",
    }
    leaf = {
        "source": "one", "source_record_id": "story-1",
        "type": "news", "raw_text": "{}",
    }
    with patch("ingestion.news_feed.RSS_FEEDS", feeds), \
         patch(
             "ingestion.news_feed.iter_feed_batches",
             return_value=iter([("one", [leaf])]),
         ):
        partial = CliRunner().invoke(cli, ["ingest", "news", "--limit", "2"])

    assert partial.exit_code == 0, partial.output
    assert "News feed fetches succeeded: 1/2" in partial.output
    assert Observation.select().count() == 1

    with patch("ingestion.news_feed.RSS_FEEDS", feeds), \
         patch("ingestion.news_feed.iter_feed_batches", return_value=iter([])):
        failed = CliRunner().invoke(cli, ["ingest", "news", "--limit", "2"])

    assert failed.exit_code == 1
    assert "All configured news feed fetches failed" in failed.output


def test_cli_ingest_dry_run_creates_no_database_in_fresh_process(tmp_path):
    db_path = tmp_path / "must-not-exist.db"
    stream_path = tmp_path / "must-not-exist.jsonl"
    project_root = Path(__file__).resolve().parent.parent
    script = "\n".join([
        "import os",
        "from pathlib import Path",
        f"os.environ['DB_PATH'] = {str(db_path)!r}",
        f"os.environ['WW3_STREAM_PATH'] = {str(stream_path)!r}",
        "os.environ['WW3_DISABLE_LLM'] = '1'",
        "from ingestion import seismic_feed",
        "seismic_feed.fetch_seismic_events = lambda **kwargs: "
        "[{'source':'usgs','type':'seismic','raw_text':'{}'}]",
        "from click.testing import CliRunner",
        "from cli.commands import cli",
        "result = CliRunner().invoke(cli, ['ingest','usgs','--dry-run'])",
        "assert result.exit_code == 0, result.output",
        "assert 'No database open' in result.output",
        "assert not Path(os.environ['DB_PATH']).exists()",
        "assert not Path(os.environ['WW3_STREAM_PATH']).exists()",
    ])
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(project_root),
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
    }

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not db_path.exists()
    assert not stream_path.exists()


def test_cli_ingest_analyze_disabled_collects_but_claims_no_run():
    from core.db import AnalysisRun, Observation

    fake_leaves = [{
        "source": "usgs", "source_record_id": "disabled-1",
        "type": "seismic", "raw_text": "{}",
    }]
    with patch("ingestion.seismic_feed.fetch_seismic_events", return_value=fake_leaves):
        result = CliRunner().invoke(cli, ["ingest", "usgs", "--analyze"])

    assert result.exit_code == 1
    assert "No AnalysisRun was claimed" in result.output
    assert Observation.select().count() == 1
    assert AnalysisRun.select().count() == 0


def test_cli_ingest_analyze_success_is_explicit_and_audited():
    from analysis.llm_processor import LLMResult
    from core.db import AnalysisRun, Event, Observation

    leaf = {
        "source": "usgs", "source_record_id": "explicit-success",
        "type": "seismic", "raw_text": "{}",
    }
    model_result = LLMResult(
        event_title="Explicit derived event",
        event_category="seismic",
        classification="reported",
        significance=3,
        summary="A model-derived test fixture.",
    )
    with patch("ingestion.seismic_feed.fetch_seismic_events", return_value=[leaf]), \
         patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("core.pipeline.claude_available", return_value=True), \
         patch("core.pipeline.process_leaf", return_value=model_result):
        result = CliRunner().invoke(cli, ["ingest", "usgs", "--analyze"])

    assert result.exit_code == 0, result.output
    assert "Created 1 new model-derived Event" in result.output
    assert Observation.select().count() == 1
    assert AnalysisRun.get().status == "completed"
    assert Event.get().title == "Explicit derived event"


def test_cli_ingest_analyze_failure_returns_nonzero_and_retains_observation():
    from core.db import AnalysisRun, Event, Observation

    leaf = {
        "source": "usgs", "source_record_id": "explicit-failure",
        "type": "seismic", "raw_text": "{}",
    }
    with patch("ingestion.seismic_feed.fetch_seismic_events", return_value=[leaf]), \
         patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("core.pipeline.claude_available", return_value=True), \
         patch("core.pipeline.process_leaf", side_effect=RuntimeError("private body")):
        result = CliRunner().invoke(cli, ["ingest", "usgs", "--analyze"])

    assert result.exit_code == 1
    assert "explicit analysis request(s) failed" in result.output
    assert "private body" not in result.output
    assert Observation.select().count() == 1
    assert AnalysisRun.get().status == "failed"
    assert Event.select().count() == 0


def test_cli_capture_and_interpret_agent_json():
    record = {
        "source": "agent-input",
        "source_record_id": "agent-1",
        "type": "news",
        "raw_text": {"title": "Source title", "summary": "Source summary"},
        "canonical_url": "https://example.test/agent-1",
        "fetched_at": "2026-07-17T12:00:00Z",
    }
    runner = CliRunner()

    captured = runner.invoke(cli, ["capture", "--json"], input=json.dumps(record))
    assert captured.exit_code == 0, captured.output
    capture_payload = json.loads(captured.output)
    assert capture_payload["collection"]["new"] == 1
    observation_id = capture_payload["collection"]["outcomes"][0]["observation_id"]

    inbox = runner.invoke(cli, ["observations", "--json"])
    assert inbox.exit_code == 0, inbox.output
    inbox_payload = json.loads(inbox.output)
    assert inbox_payload["observations"][0]["analysis"]["status"] == "not-requested"
    assert "raw_text" not in inbox.output

    interpreted = runner.invoke(cli, ["interpret", str(observation_id), "--json"])
    assert interpreted.exit_code == 0, interpreted.output
    projection = json.loads(interpreted.output)
    assert projection["title"] == "Source title"
    assert projection["summary"] == "Source summary"
    assert projection["projection_version"] == "observation-projection-v2"
    assert "significance" not in interpreted.output


def test_cli_rhetoric_is_explicit_json_and_failure_does_not_leak_source():
    from core.observations import get_or_create_observation
    from interpretation.rhetorlint import RhetorLintInvalidOutput

    observation, _ = get_or_create_observation({
        "source": "un_press",
        "source_record_id": "rhetoric-cli",
        "type": "statement",
        "raw_text": json.dumps({"summary": "private source phrase"}),
    })
    annotation = {
        "status": "completed",
        "contract_version": "ww3-rhetorlint/0.1",
        "observation": {"id": observation.id},
        "runtime": {"core_sha256": "c", "rules_sha256": "r"},
        "summary": {"passages": 1, "words": 3, "matched_tells": 0, "per_100_words": 0},
        "passages": [],
        "scope": "language only",
    }
    with patch(
        "interpretation.rhetorlint.analyze_observation_rhetoric",
        return_value=annotation,
    ):
        result = CliRunner().invoke(cli, ["rhetoric", str(observation.id), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["contract_version"] == "ww3-rhetorlint/0.1"

    with patch(
        "interpretation.rhetorlint.analyze_observation_rhetoric",
        side_effect=RhetorLintInvalidOutput("private source phrase"),
    ):
        failed = CliRunner().invoke(cli, ["rhetoric", str(observation.id), "--json"])

    assert failed.exit_code == 1
    assert json.loads(failed.output)["error"]["code"] == "rhetorlint_invalid_output"
    assert "private source phrase" not in failed.output


def test_cli_casefile_and_assessment_validation_are_agent_readable():
    from core.observations import get_or_create_observation

    observation, _ = get_or_create_observation({
        "source": "un_press",
        "source_record_id": "case-cli",
        "type": "statement",
        "raw_text": json.dumps({
            "title": "United Nations statement",
            "summary": "The United States responded.",
        }),
    })
    runner = CliRunner()
    case_result = runner.invoke(cli, ["casefile", str(observation.id), "--json"])

    assert case_result.exit_code == 0, case_result.output
    case = json.loads(case_result.output)
    assert case["epistemic_status"]["intent"] == "not-inferred"
    assert case["actor_visibility"][0]["actor"]["actor_id"] in {"org:un", "state:us"}

    assessment = {
        "contract": "ww3.assessment/v1",
        "casefile_fingerprint_sha256": case["fingerprint_sha256"],
        "observation_ids": [observation.id],
        "producer": {"kind": "agent", "name": "cli-test", "version": "1"},
        "claims": [],
        "power_bases": [],
        "intent_hypotheses": [],
        "contradictions": [],
        "unknowns": ["No claim assessment supplied."],
    }
    validated = runner.invoke(
        cli,
        ["validate-assessment", "--json"],
        input=json.dumps(assessment),
    )

    assert validated.exit_code == 0, validated.output
    assert json.loads(validated.output)["contract"] == "ww3.assessment/v1"

    human = runner.invoke(
        cli,
        ["validate-assessment"],
        input=json.dumps(assessment),
    )
    assert human.exit_code == 0, human.output
    assert "0 power basis/bases" in human.output


def test_cli_capture_rejects_unbounded_or_invalid_input_without_echoing_it():
    runner = CliRunner()
    secret = "private-invalid-source-body"

    invalid_json = runner.invoke(cli, ["capture"], input=f"not-json-{secret}")
    assert invalid_json.exit_code == 1
    assert "not valid JSON" in invalid_json.output
    assert secret not in invalid_json.output

    too_many = runner.invoke(
        cli,
        ["capture"],
        input=json.dumps([None] * 1_001),
    )
    assert too_many.exit_code == 1
    assert "1,000-record batch limit" in too_many.output

    mixed = runner.invoke(cli, ["capture", "--json"], input=json.dumps([
        {"source": "valid", "type": "news", "raw_text": "{}"},
        {"type": "news", "raw_text": secret},
    ]))
    assert mixed.exit_code == 0, mixed.output
    assert secret not in mixed.output
    assert json.loads(mixed.output)["collection"]["invalid"] == 1


def test_cli_analyze_observation_disabled_fails_before_run_claim():
    from core.db import AnalysisRun
    from core.observations import get_or_create_observation

    observation, _ = get_or_create_observation({
        "source": "test-feed",
        "source_record_id": "explicit-disabled",
        "type": "news",
        "raw_text": "source body must not print",
    })

    result = CliRunner().invoke(cli, ["analyze-observation", str(observation.id)])

    assert result.exit_code == 1
    assert "No AnalysisRun was claimed" in result.output
    assert "source body" not in result.output
    assert AnalysisRun.select().count() == 0


def test_agent_commands_return_nonzero_structured_not_found_errors():
    runner = CliRunner()

    interpreted = runner.invoke(cli, ["interpret", "999", "--json"])
    provenance_result = runner.invoke(cli, ["provenance", "999", "--json"])
    analyzed = runner.invoke(cli, ["analyze-observation", "999"])

    assert interpreted.exit_code == 1
    assert json.loads(interpreted.output)["error"] == {
        "code": "not_found", "resource": "observation", "id": 999,
    }
    assert provenance_result.exit_code == 1
    assert json.loads(provenance_result.output)["error"]["resource"] == "event"
    assert analyzed.exit_code == 1
    assert "Observation 999 not found" in analyzed.output


def test_cli_analyze_observation_failure_returns_nonzero():
    from core.db import AnalysisRun
    from core.observations import get_or_create_observation

    observation, _ = get_or_create_observation({
        "source": "test-feed",
        "source_record_id": "explicit-failure",
        "type": "news",
        "raw_text": "private source body",
    })
    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("core.pipeline.claude_available", return_value=True), \
         patch("core.pipeline.process_leaf", side_effect=RuntimeError("private source body")):
        result = CliRunner().invoke(
            cli, ["analyze-observation", str(observation.id)]
        )

    assert result.exit_code == 1
    assert "Explicit analysis failed" in result.output
    assert "private source body" not in result.output
    assert AnalysisRun.get().status == "failed"


def test_cli_text_view_neutralizes_source_terminal_controls():
    from core.observations import get_or_create_observation

    observation, _ = get_or_create_observation({
        "source": "control-feed",
        "source_record_id": "control-1",
        "type": "news",
        "raw_text": json.dumps({
            "title": "\x1b[31mFake\nHeading\u202espoof",
            "summary": "line one\x1b]0;spoof\x07line two",
        }),
    })

    result = CliRunner().invoke(cli, ["interpret", str(observation.id)])

    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output
    assert "\x07" not in result.output
    assert "\u202e" not in result.output
    assert "Fake Heading" in result.output


def test_cli_provenance_shows_lineage_without_raw_content():
    from core.db import AnalysisRun, Event
    from core.observations import get_or_create_observation

    event = Event.create(
        title="Provenance Test",
        date="2026-03-03",
        category="political",
        classification="reported",
        significance=4,
    )
    observation, _ = get_or_create_observation({
        "source": "example-feed",
        "source_record_id": "record-7",
        "type": "news",
        "raw_text": "private source body must not print",
        "canonical_url": "https://example.test/record-7",
    })
    AnalysisRun.create(
        observation=observation,
        event=event,
        status="completed",
        model_name="mock-model",
        prompt_version="prompt-hash",
        code_version="test-code",
    )

    result = CliRunner().invoke(cli, ["provenance", str(event.id)])

    assert result.exit_code == 0, result.output
    assert "record-7" in result.output
    assert "https://example.test/record-7" in result.output
    assert observation.content_hash in result.output
    assert "private source body" not in result.output

    json_result = CliRunner().invoke(
        cli, ["provenance", str(event.id), "--json"]
    )
    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.output)
    assert payload["event"]["id"] == event.id
    assert payload["analysis_runs"][0]["observation"]["content_sha256"] == (
        observation.content_hash
    )
    assert "private source body" not in json_result.output


def test_cli_provenance_sanitizes_legacy_metadata_controls():
    from core.db import AnalysisRun, Event, Observation

    event = Event.create(
        title="Legacy\u202e title", date="2026-03-03", category="other",
        classification="reported", significance=2,
    )
    observation = Observation.create(
        dedupe_key="legacy-provenance-control",
        source="feed\u202eexe",
        source_record_id="record\u202eexe",
        type="news",
        raw_text="legacy body",
        content_hash="hash",
        canonical_url="https://example.test/story\u202eexe",
        fetched_at=datetime(2026, 7, 17, 12, 0),
    )
    AnalysisRun.create(observation=observation, event=event, status="completed")

    result = CliRunner().invoke(cli, ["provenance", str(event.id)])

    assert result.exit_code == 0, result.output
    assert "\u202e" not in result.output
    assert "Legacy� title" in result.output
    assert "feed�exe" in result.output


def test_cli_runs_exposes_failed_run_without_source_body():
    from core.db import AnalysisRun
    from core.observations import get_or_create_observation

    observation, _ = get_or_create_observation({
        "source": "failed-feed",
        "source_record_id": "failed-1",
        "type": "news",
        "raw_text": "private failed source body",
    })
    run = AnalysisRun.create(
        observation=observation,
        status="failed",
        attempt=2,
        error_stage="analysis",
        error="LLMValidationError",
    )

    result = CliRunner().invoke(cli, ["runs", "--status", "failed"])
    assert result.exit_code == 0, result.output
    assert f"[{run.id}] failed" in result.output
    assert "analysis/LLMValidationError" in result.output
    assert "source=failed-feed | failure=" in result.output
    assert "private failed source body" not in result.output

    json_result = CliRunner().invoke(
        cli, ["runs", "--status", "failed", "--json"]
    )
    payload = json.loads(json_result.output)
    assert payload["analysis_runs"][0]["event_id"] is None
    assert payload["analysis_runs"][0]["observation_id"] == observation.id


def test_cli_analyze():
    """Legacy RawLeaf migration routes through the versioned pipeline."""
    from core.db import Event, RawLeaf, init_db

    runner = CliRunner()
    # Create an event and attach a raw leaf
    create_result = runner.invoke(cli, [
        "add-event", "--title", "Analyze Test Event", "--category", "seismic"
    ])
    assert create_result.exit_code == 0

    # Find the created event and attach a raw leaf
    event = Event.select().where(Event.title == "Analyze Test Event").get()
    RawLeaf = __import__("core.db", fromlist=["RawLeaf"]).RawLeaf
    RawLeaf.create(
        source="usgs",
        type="seismic",
        raw_text='{"mag": 5.1, "place": "Test Location"}',
        location_name="Test Location",
        confidence="high",
        processed=False,
        event=event,
    )

    derived = Event.create(
        title="Derived Event", date="2026-03-03", category="seismic",
        classification="reported", significance=5,
    )
    with patch("cli.commands._analysis_preflight"), \
         patch("core.pipeline.ingest_leaf", return_value=derived) as ingest:
        result = runner.invoke(cli, ["migrate-legacy-event", str(event.id)])

    assert result.exit_code == 0, result.output
    assert "Linked 1 legacy observations" in result.output
    ingest.assert_called_once()
    migrated_leaf = ingest.call_args.args[0]
    assert migrated_leaf["source_record_id"].startswith("legacy-rawleaf:")
    assert RawLeaf.get_by_id(1).processed is True


def test_cli_analyze_retry_heals_marker_after_post_commit_crash():
    from analysis.llm_processor import LLMResult
    from core.db import AnalysisRun, Event, Observation, RawLeaf

    legacy_event = Event.create(
        title="Legacy Event", date="2026-03-03", category="seismic",
        classification="reported", significance=4,
    )
    raw_leaf = RawLeaf.create(
        source="legacy-feed",
        type="news",
        raw_text="legacy source body",
        processed=False,
        event=legacy_event,
    )
    model_result = LLMResult(
        event_title="Auditable Event",
        event_category="political",
        classification="reported",
        significance=4,
        summary="A legacy source report migrated through the ledger.",
    )
    runner = CliRunner()

    with patch("analysis.claude_cli.claude_available", return_value=True), \
         patch("core.pipeline.claude_available", return_value=True), \
         patch("core.pipeline.process_leaf", return_value=model_result), \
         patch.object(RawLeaf, "save", side_effect=RuntimeError("marker write failed")):
        first = runner.invoke(cli, ["migrate-legacy-event", str(legacy_event.id)])

    assert first.exit_code != 0
    assert RawLeaf.get_by_id(raw_leaf.id).processed is False
    assert AnalysisRun.get().status == "completed"
    observation = Observation.get()
    assert observation.published_at is None
    assert observation.fetched_at == raw_leaf.ts

    with patch("core.pipeline.process_leaf") as process:
        retry = runner.invoke(cli, ["migrate-legacy-event", str(legacy_event.id)])

    assert retry.exit_code == 0, retry.output
    assert "Linked 1 legacy observations" in retry.output
    assert RawLeaf.get_by_id(raw_leaf.id).processed is True
    process.assert_not_called()


def test_cli_legacy_migration_disabled_fails_before_observation_capture():
    from core.db import AnalysisRun, Event, Observation, RawLeaf

    legacy_event = Event.create(
        title="Disabled Legacy", date="2026-03-03", category="other",
        classification="reported", significance=2,
    )
    raw_leaf = RawLeaf.create(
        source="legacy-feed", type="news", raw_text="legacy body",
        processed=False, event=legacy_event,
    )

    result = CliRunner().invoke(
        cli, ["migrate-legacy-event", str(legacy_event.id)]
    )

    assert result.exit_code == 1
    assert "No AnalysisRun was claimed" in result.output
    assert Observation.select().count() == 0
    assert AnalysisRun.select().count() == 0
    assert RawLeaf.get_by_id(raw_leaf.id).processed is False


def test_cli_report():
    """Create an event, run report, verify markdown file is created at correct path."""
    runner = CliRunner()
    create_result = runner.invoke(cli, [
        "add-event",
        "--title", "Report Test Event",
        "--date", "2026-03-03",
        "--category", "military",
        "--summary", "A test military event",
    ])
    assert create_result.exit_code == 0

    from core.db import Event
    event = Event.select().where(Event.title == "Report Test Event").get()

    with runner.isolated_filesystem() as tmp_dir:
        import os
        # Reports default to ignored local data/reports/<date>-<slug>.md.
        result = runner.invoke(cli, ["report", str(event.id)])
        assert result.exit_code == 0, result.output

        expected_slug = "report-test-event"
        expected_filename = f"2026-03-03-{expected_slug}.md"
        expected_path = os.path.join(tmp_dir, "data", "reports", expected_filename)
        assert os.path.exists(expected_path), (
            f"Expected report file not found at {expected_path}. Output: {result.output}"
        )
        content = open(expected_path).read()
        assert "Report Test Event" in content
        assert "NO PROVENANCE" in content
        assert stat.S_IMODE(os.stat(expected_path).st_mode) == 0o600
