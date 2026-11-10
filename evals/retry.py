"""When to retry an HTTP request, shared by the agent target and the cloud evaluation runner."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_RETRY_AFTER_S = 60.0


def retry_delay(attempt: int, headers=None) -> float:
    """Seconds to wait before retrying after `attempt` (0-based): the server's retry-after-ms or
    Retry-After (capped at 60 s), else exponential backoff of 5, 10, 20, then 30 s."""
    hints = {k.lower(): v for k, v in headers.items()} if headers is not None else {}
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = hints.get(name)
        if value is None:
            continue
        try:
            wait = float(value) * scale
        except ValueError:
            try:
                wait = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                continue
        return min(MAX_RETRY_AFTER_S, max(0.0, wait))
    return min(30.0, 5.0 * 2 ** attempt)
