"""Bounded HTTP reads for source adapters."""

from __future__ import annotations

import time
from collections.abc import Callable


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
            raise ValueError("HTTP redirects are not followed")

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_size = int(content_length)
            except (TypeError, ValueError):
                declared_size = None
            if declared_size is not None and declared_size > max_bytes:
                raise ValueError(f"HTTP response exceeds {max_bytes} bytes")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if time.monotonic() > deadline:
                raise TimeoutError(f"HTTP response exceeded {total_timeout:g} seconds")
            if not chunk:
                continue
            if len(body) + len(chunk) > max_bytes:
                raise ValueError(f"HTTP response exceeds {max_bytes} bytes")
            body.extend(chunk)
        return bytes(body)
    finally:
        response.close()
