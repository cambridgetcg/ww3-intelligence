import json
import os
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from analysis.llm_processor import LLMResult
from core.pipeline import STREAM_PATH, ingest_leaf


def test_pipeline_writes_only_to_isolated_stream(test_storage_root):
    stream_path = Path(STREAM_PATH)
    assert stream_path.resolve() == Path(os.environ["WW3_STREAM_PATH"]).resolve()
    assert stream_path.resolve().is_relative_to(test_storage_root.resolve())

    result = LLMResult(
        event_title="Isolated test event",
        event_category="other",
        classification="speculation",
        significance=1,
        summary="Synthetic fixture data.",
    )
    leaf = {
        "source": "test",
        "type": "test",
        "raw_text": "synthetic fixture",
    }

    with patch("core.pipeline.claude_available", return_value=True), \
         patch("core.pipeline.process_leaf", return_value=result), \
         patch("core.pipeline.save_all_branches"):
        event = ingest_leaf(leaf)

    assert event is not None
    assert stream_path.exists()
    records = [json.loads(line) for line in stream_path.read_text().splitlines()]
    assert [record["event_id"] for record in records] == [event.id]


def test_external_network_and_real_claude_are_denied(monkeypatch):
    with pytest.raises(AssertionError, match="network access is disabled"):
        socket.getaddrinfo("example.com", 443)

    from analysis.claude_cli import claude_available
    monkeypatch.setenv("WW3_DISABLE_LLM", "0")
    assert claude_available() is False
