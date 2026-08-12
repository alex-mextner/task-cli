"""Tiny stdlib ``urllib`` JSON-HTTP helper shared by the backends.

No ``requests``, no per-call subprocess — just ``urllib.request`` with JSON bodies and a
bearer/header auth scheme the caller supplies. Kept minimal: a single ``request_json`` that
returns parsed JSON or raises :class:`HttpError` with the status + a (token-free) snippet.
"""

from __future__ import annotations

import http.client
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


# The read/parse-time failures that can happen AFTER ``urlopen`` already returned a 2xx status.
# Broad ON PURPOSE (review finding: an earlier version enumerated ConnectionResetError/
# TimeoutError/IncompleteRead individually and missed the MOST common real-world case — both
# providers are HTTPS-only, and a connection dropped mid-TLS-read typically surfaces as
# ``ssl.SSLError``/``ssl.SSLEOFError``, not a bare ``ConnectionResetError``). Any exception in
# this window means the same thing — "the status already landed, something after that failed" —
# so the net is cast at the CATEGORY level instead of an enumerated allowlist that a new failure
# mode can silently fall outside of:
#   - OSError: every OS/TLS-level read failure (ConnectionResetError, TimeoutError/
#     socket.timeout — the same class since Python 3.10 — ssl.SSLError/SSLEOFError,
#     ConnectionAbortedError, BrokenPipeError, ...).
#   - http.client.HTTPException: httplib-native read failures (IncompleteRead and friends).
#   - UnicodeDecodeError / json.JSONDecodeError: the body arrived but is truncated/malformed —
#     a connection that closes early without tripping IncompleteRead can still hand back a body
#     that doesn't decode/parse; that's the identical ambiguous window, just caught one step
#     later, so it must raise the SAME error, not a bare traceback.
_AMBIGUOUS_READ_EXCEPTIONS = (OSError, http.client.HTTPException, UnicodeDecodeError, json.JSONDecodeError)


class AmbiguousHttpError(RuntimeError):
    """The connection dropped WHILE reading the response body, after a 2xx status already
    landed — the request may have succeeded server-side even though this process never saw the
    body (e.g. GitHub returned 201 and created the issue, then the read itself failed).

    Deliberately NOT a subclass of :class:`HttpError`: ``HttpError`` means "a clean, known
    outcome" (a real non-2xx, or a failure before the server ever accepted the request) — a
    caller catching ``HttpError`` should be able to assume nothing happened server-side. Folding
    this ambiguous case into that same bucket would make a genuine side effect indistinguishable
    from a clean no-op, which is exactly what let a naive retry double-create a ticket.
    """


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
            try:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
            except _AMBIGUOUS_READ_EXCEPTIONS as exc:
                # the status line already arrived (2xx — urlopen raises HTTPError itself for a
                # non-2xx, so we never reach here otherwise) but reading/decoding/parsing the
                # body then failed: the request may have already completed server-side. See
                # AmbiguousHttpError.
                raise AmbiguousHttpError(
                    f"connection interrupted while reading the response for {method} {url} "
                    f"(status {resp.status} was already returned) — the request may have "
                    "completed server-side even though this process never saw a usable body"
                ) from exc
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")[:500]
        except Exception:  # noqa: BLE001
            pass
        raise HttpError(exc.code, f"HTTP {exc.code} for {method} {url}", body) from exc
    except urllib.error.URLError as exc:
        raise HttpError(0, f"connection failed for {method} {url}: {exc.reason}") from exc
