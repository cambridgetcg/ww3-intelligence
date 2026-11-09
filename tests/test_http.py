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


def test_fetch_limited_response_keeps_status_and_a_bounded_media_type():
    from ingestion.http import fetch_limited_response

    response = _response(
        [b"<rss/>"], headers={"Content-Type": "Application/RSS+XML; charset=UTF-8"},
    )

    answer = fetch_limited_response(
        "https://example.test/feed",
        request_get=MagicMock(return_value=response),
        max_bytes=10,
        timeout=1,
        total_timeout=10,
    )

    assert (answer.body, answer.status, answer.content_type) == (
        b"<rss/>", 200, "application/rss+xml",
    )


@pytest.mark.parametrize("header", [None, "", "text", "text/html <script>", "x" * 200])
def test_media_type_drops_headers_not_shaped_like_one(header):
    from ingestion.http import media_type

    assert media_type(header) is None


def test_describe_failure_names_status_and_bounds_never_messages():
    import requests

    from ingestion.http import (
        FetchRefusal, RedirectRefused, ResponseTooLarge, describe_failure,
    )

    http_error = requests.HTTPError("private https://example.test/?token=1")
    http_error.response = MagicMock(status_code=429)

    assert describe_failure(RedirectRefused(308)) == "HTTP 308 redirect refused"
    assert describe_failure(ResponseTooLarge(5)) == "response over 5 bytes"
    assert describe_failure(http_error) == "HTTP 429"
    assert describe_failure(FetchRefusal("not a feed: text/html, 3 bytes")) == (
        "not a feed: text/html, 3 bytes"
    )
    assert describe_failure(requests.ReadTimeout("private")) == "ReadTimeout"
    assert describe_failure(ValueError("private")) == "ValueError"


def test_fetch_result_classifies_ok_empty_and_failed():
    from ingestion.http import fetch_result

    def broken():
        raise RuntimeError("private")

    assert fetch_result("a", lambda: [1], limit=5).status == "ok"
    assert fetch_result("b", lambda: [], limit=5).status == "empty"
    failed = fetch_result("c", broken, limit=5)
    assert (failed.status, failed.records, failed.reason, failed.limit) == (
        "failed", [], "RuntimeError", 5,
    )
