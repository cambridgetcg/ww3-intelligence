from unittest.mock import MagicMock, patch

import pytest

from ingestion.http import fetch_limited


def _response(chunks, *, headers=None, status=200):
    response = MagicMock()
    response.status_code = status
    response.headers = headers or {}
    response.iter_content.return_value = iter(chunks)
    return response


def test_fetch_limited_stops_at_streaming_byte_cap_and_closes():
    response = _response([b"abc", b"def"])
    request_get = MagicMock(return_value=response)

    with pytest.raises(ValueError, match="exceeds 5 bytes"):
        fetch_limited(
            "https://example.test/data",
            request_get=request_get,
            max_bytes=5,
            timeout=1,
            total_timeout=10,
        )

    response.close.assert_called_once()
    assert request_get.call_args.kwargs["stream"] is True
    assert request_get.call_args.kwargs["allow_redirects"] is False


def test_fetch_limited_enforces_total_deadline_between_reads():
    response = _response([b"abc"])
    with patch("ingestion.http.time.monotonic", side_effect=[10, 21]):
        with pytest.raises(TimeoutError, match="10 seconds"):
            fetch_limited(
                "https://example.test/data",
                request_get=MagicMock(return_value=response),
                max_bytes=10,
                timeout=1,
                total_timeout=10,
            )

    response.close.assert_called_once()


def test_fetch_limited_rejects_redirect_without_following():
    response = _response([], status=302)
    with pytest.raises(ValueError, match="redirects"):
        fetch_limited(
            "https://example.test/data",
            request_get=MagicMock(return_value=response),
            max_bytes=10,
            timeout=1,
            total_timeout=10,
        )
