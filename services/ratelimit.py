"""How long an API that answered "too many requests" (HTTP 429) wants us to wait."""

import math
import time
from collections.abc import Mapping
from email.utils import parsedate_to_datetime

import httpx2


def retry_after(response: httpx2.Response | None, error: object = None, now: float | None = None) -> float | None:
    """Seconds until a rate limit lifts, or None if the answer doesn't say.

    Read from Retry-After (seconds or a date), retry-after-ms or X-RateLimit-Reset (a Unix
    time, in milliseconds at OpenRouter): in the response headers or, as OpenRouter sends
    them for its own limits, in the error body's metadata.headers.
    """
    now = time.time() if now is None else now
    sources = [response.headers] if response is not None else []
    metadata = error.get("metadata") if isinstance(error, Mapping) else None
    if isinstance(metadata, Mapping) and isinstance(metadata.get("headers"), Mapping):
        sources.append(httpx2.Headers({str(k): str(v) for k, v in metadata["headers"].items()}))
    for headers in sources:
        wait = _wait_from(headers, now)
        if wait is not None:
            return wait
    return None


def _wait_from(headers: httpx2.Headers, now: float) -> float | None:
    if (ms := _number(headers.get("retry-after-ms"))) is not None:
        return max(0.0, ms / 1000)
    if (value := headers.get("retry-after")) is not None:
        if (seconds := _number(value)) is not None:
            return max(0.0, seconds)
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError):
            pass
    if (reset := _number(headers.get("x-ratelimit-reset"))) is not None:
        if reset > 1e11:  # a Unix time in milliseconds
            reset /= 1000
        return max(0.0, reset - now if reset > 1e9 else reset)  # a Unix time, or seconds left
    return None


def _number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
