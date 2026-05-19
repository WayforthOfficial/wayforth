from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx

_BACKOFF = [1, 2, 4]
_MAX_ATTEMPTS = 3


def with_retry(
    fn: Callable[[], httpx.Response],
    sleep: Callable[[float], None] | None = None,
) -> httpx.Response:
    _sleep = sleep if sleep is not None else time.sleep
    last: httpx.Response | None = None
    for attempt in range(_MAX_ATTEMPTS):
        response = fn()
        if response.status_code < 500:
            return response
        last = response
        if attempt < _MAX_ATTEMPTS - 1:
            _sleep(_BACKOFF[attempt])
    assert last is not None
    return last


async def with_retry_async(
    fn: Callable[[], Any],
    sleep: Callable[[float], Any] | None = None,
) -> httpx.Response:
    _sleep = sleep if sleep is not None else asyncio.sleep
    last: httpx.Response | None = None
    for attempt in range(_MAX_ATTEMPTS):
        response = await fn()
        if response.status_code < 500:
            return response
        last = response
        if attempt < _MAX_ATTEMPTS - 1:
            await _sleep(_BACKOFF[attempt])
    assert last is not None
    return last
