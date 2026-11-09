"""Bounded HTTP reads for source adapters."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import requests


# What one source's answer was, as this collector could tell. ``empty`` is a
# well-formed answer that listed nothing; ``failed`` is an error, a refused
# redirect, or an answer that was not what the source serves. Neither is
# evidence that the publisher published nothing.
FETCH_OK = "ok"
FETCH_EMPTY = "empty"
FETCH_FAILED = "failed"


class RedirectRefused(ValueError):
    """A 3xx answer. Redirects are refused, never followed."""

    def __init__(self, status: int):
        super().__init__("HTTP redirects are not followed")
        self.status = status


class ResponseTooLarge(ValueError):
    """A body over the adapter's byte bound."""

    def __init__(self, max_bytes: int):
        super().__init__(f"HTTP response exceeds {max_bytes} bytes")
        self.max_bytes = max_bytes


class FetchRefusal(ValueError):
    """An answer that arrived but cannot be read as the source's records.

    ``reason`` is written by this code from counts and the declared media
    type, never copied from the body.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class LimitedResponse:
    body: bytes
    status: int | None
    # The media type alone (no parameters), lowercased; None when the server
    # sent none, or sent something not shaped like one.
    content_type: str | None


_MEDIA_TYPE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
)


def media_type(header) -> str | None:
    """Reduce a Content-Type header to a bounded, printable media type."""
    if not isinstance(header, str):
        return None
    value = header.split(";", 1)[0].strip().lower()
    return value if _MEDIA_TYPE.match(value) else None


def fetch_limited_response(
    url: str,
    *,
    request_get: Callable,
    max_bytes: int,
    timeout,
    total_timeout: float,
    headers: dict | None = None,
    params: dict | None = None,
) -> LimitedResponse:
    """Fetch one response without redirects and stop after ``max_bytes``.

    Connect/read timeouts are enforced by Requests. They are per socket
    operation, not a universal wall-clock deadline.
    """
    response = request_get(
        url,
        headers=headers,
        params=params,
        timeout=timeout,
        allow_redirects=False,
        stream=True,
    )
    deadline = time.monotonic() + total_timeout
    try:
        response.raise_for_status()
        status = getattr(response, "status_code", 200)
        if isinstance(status, int) and 300 <= status < 400:
            raise RedirectRefused(status)

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_size = int(content_length)
            except (TypeError, ValueError):
                declared_size = None
            if declared_size is not None and declared_size > max_bytes:
                raise ResponseTooLarge(max_bytes)

        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if time.monotonic() > deadline:
                raise TimeoutError(f"HTTP response exceeded {total_timeout:g} seconds")
            if not chunk:
                continue
            if len(body) + len(chunk) > max_bytes:
                raise ResponseTooLarge(max_bytes)
            body.extend(chunk)
        return LimitedResponse(
            body=bytes(body),
            status=status if isinstance(status, int) else None,
            content_type=media_type(response.headers.get("Content-Type")),
        )
    finally:
        response.close()


def fetch_limited(
    url: str,
    *,
    request_get: Callable,
    max_bytes: int,
    timeout,
    total_timeout: float,
    headers: dict | None = None,
    params: dict | None = None,
) -> bytes:
    """Return only the body of :func:`fetch_limited_response`."""
    return fetch_limited_response(
        url,
        request_get=request_get,
        max_bytes=max_bytes,
        timeout=timeout,
        total_timeout=total_timeout,
        headers=headers,
        params=params,
    ).body


@dataclass(frozen=True)
class FetchResult:
    """One source's answer in one run: a registered feed, USGS or GDACS."""

    source: str
    status: str
    records: list = field(default_factory=list)
    # Written by this code (a status, a bound, a media type, a class name),
    # never a publisher's text. None unless the fetch failed.
    reason: str | None = None
    # The record bound the source was read under, so an answer that reaches
    # it can be told apart from one that was read in full.
    limit: int | None = None
    # For a source read as several bounded pages (GDACS), the names of the
    # pages that reached ``limit``, which is then each page's bound: a merged
    # record count can neither show nor rule out a full page. None for a
    # single bounded read, judged by its record count.
    full_pages: tuple[str, ...] | None = None


def fetch_result(source: str, fetch: Callable[[], list], *, limit: int) -> FetchResult:
    """Run one bounded fetch and classify what came back."""
    try:
        records = list(fetch())
    except Exception as exc:
        return FetchResult(
            source, FETCH_FAILED, reason=describe_failure(exc), limit=limit,
        )
    return FetchResult(
        source, FETCH_OK if records else FETCH_EMPTY, records, limit=limit,
    )


def describe_failure(exc: BaseException) -> str:
    """Name a fetch failure in a few words of this code's own.

    Exception messages can carry a URL, a header or a fragment of a body, so
    they are never printed. A status code, a bound or a class name is.
    """
    if isinstance(exc, FetchRefusal):
        return exc.reason
    if isinstance(exc, RedirectRefused):
        return f"HTTP {exc.status} redirect refused"
    if isinstance(exc, ResponseTooLarge):
        return f"response over {exc.max_bytes} bytes"
    if isinstance(exc, requests.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(status, int):
            return f"HTTP {status}"
    return type(exc).__name__
