"""Tiny stdlib ``urllib`` JSON-HTTP helper shared by the backends.

No ``requests``, no per-call subprocess — just ``urllib.request`` with JSON bodies and a
bearer/header auth scheme the caller supplies. Kept minimal: a single ``request_json`` that
returns parsed JSON or raises :class:`HttpError` with the status + a (token-free) snippet.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class HttpError(RuntimeError):
    """An HTTP request returned a non-2xx status or failed to connect."""

    def __init__(self, status: int, message: str, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: Any = None,
    timeout: int = 30,
) -> Any:
    """Make a JSON request and return parsed JSON (or ``None`` for an empty body).

    ``payload`` is JSON-encoded when present. Auth headers are passed by the caller and are
    NEVER logged here. A non-2xx status raises :class:`HttpError` carrying the status and a
    short response snippet (useful for surfacing GitHub/Linear error messages).
    """
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    hdrs.setdefault("Accept", "application/json")
    hdrs.setdefault("User-Agent", "task-cli")

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - trusted provider hosts
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")[:500]
        except Exception:  # noqa: BLE001
            pass
        raise HttpError(exc.code, f"HTTP {exc.code} for {method} {url}", body) from exc
    except urllib.error.URLError as exc:
        raise HttpError(0, f"connection failed for {method} {url}: {exc.reason}") from exc
