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
from urllib.parse import urlparse


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

    review finding: a malformed authority (e.g. a bad port in an ambient ``GITHUB_API_URL``
    override, or any other caller-/config-supplied url this process didn't fully validate)
    raises a bare ``http.client.InvalidURL`` — neither ``HTTPError`` nor ``URLError`` — the
    moment ``http.client`` tries to open the connection, which used to propagate past every
    caller's ``HttpError``-only ``except`` clause as a raw traceback instead of a clean
    :class:`BackendError`. Wrapped as a clean :class:`HttpError`: ``InvalidURL`` fires from
    constructing/validating the request's host/port BEFORE anything is sent, so nothing has
    reached the server yet — a caller is free to fix the url and retry, though retrying
    UNCHANGED will never succeed (this is a permanent input error, not a transient network one;
    status ``0`` here means "nothing was sent," not "safe to blindly retry").

    Deliberately narrow: this does NOT extend to other pre-send failures (e.g. a ``ValueError``
    from ``http.client.putheader`` for a header value containing a stray newline) or to a
    failure raised from reading back the response (see :data:`_AMBIGUOUS_READ_EXCEPTIONS` above,
    which this function already handles) — classifying THOSE cleanly hits a materially harder
    problem (CPython/urllib-internals-sensitive exception inheritance — e.g.
    ``RemoteDisconnected`` is simultaneously an ``OSError`` and an ``http.client.HTTPException``)
    that was scoped OUT of this fix after review discussion; only the narrow, unambiguous
    ``InvalidURL`` case is handled here.

    review finding (round 5): ``urllib.request.Request(url, ...)`` itself raises a bare
    ``ValueError`` for a bracket-malformed authority (``"https://[bad"``) at CONSTRUCTION time —
    a parse-time failure, distinct from the connect-time ``InvalidURL`` above but in the exact
    same "nothing was sent yet" bucket. Caught in its OWN narrow ``try`` around just the
    construction call (not folded into the ``urlopen`` exception handling below) so this doesn't
    quietly widen to catch a ``ValueError`` from deeper inside ``urlopen`` too — e.g. the
    ``http.client.putheader`` illegal-header-value case, which stays deliberately out of scope
    (see above). Closes the gap for a caller (e.g. ``LinearBackend._upload_and_attach``'s signed
    upload URL, or a bracket-malformed ``GITHUB_API_URL``) that hands this function a url from a
    source outside its own control.
    """
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    hdrs.setdefault("Accept", "application/json")
    hdrs.setdefault("User-Agent", "task-cli")

    try:
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    except ValueError as exc:
        raise HttpError(0, f"invalid URL for {method} {url}: {exc}") from exc
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
    except http.client.InvalidURL as exc:
        raise HttpError(0, f"invalid URL for {method} {url}: {exc}") from exc


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses to follow a redirect to a different host, OR a scheme downgrade (https -> http),
    than the request that was actually made — used whenever the request carries a
    caller-supplied auth header (review finding: urllib follows redirects by default, so a
    malicious/compromised server could either 302 the request to an attacker-controlled host, or
    302 it to a PLAINTEXT `http://` URL on the SAME host, and urllib would happily replay every
    header, including ``Authorization``, onto it either way — a same-host-only check misses the
    downgrade case)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 - stdlib override name
        old, new = urlparse(req.full_url), urlparse(newurl)
        if new.netloc != old.netloc or (old.scheme == "https" and new.scheme != "https"):
            raise urllib.error.HTTPError(
                newurl, code, f"refused unsafe redirect from {req.full_url} to {newurl}", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SAME_HOST_OPENER = urllib.request.build_opener(_SameHostRedirectHandler)


def request_bytes(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = 60,
    same_host_redirects_only: bool = False,
) -> bytes:
    """Raw binary request (no JSON encode/decode) — a signed-URL file PUT, or a GET that reads
    the response body as bytes rather than parsing it. Same error contract as
    :func:`request_json`: a non-2xx status raises :class:`HttpError`; a connection dropping mid
    read after a 2xx status raises :class:`AmbiguousHttpError`. Auth headers are passed by the
    caller and are NEVER logged here.

    ``same_host_redirects_only=True`` MUST be set whenever ``headers`` carries a credential
    (e.g. an ``Authorization`` header) and the URL is not a request this process itself
    constructed end-to-end — otherwise a redirect can exfiltrate the credential to any host
    (see :class:`_SameHostRedirectHandler`).

    review finding: ``url`` doesn't have to be a value this process constructed end-to-end — a
    malformed authority parses fine at the ``urlparse()`` level, but ``http.client`` raises a
    bare ``http.client.InvalidURL`` — neither an ``HTTPError`` nor a ``URLError`` — the moment it
    actually tries to open the connection. Left uncaught, that propagates past every caller's
    ``HttpError``-only ``except`` clause and crashes the whole command instead of failing just
    the one request. The original motivating case was a tracker-controlled attachment url in
    :meth:`LinearBackend.fetch_attachment_bytes`; that call site is now guarded earlier, by
    :func:`~tasklib.backends.linear._safe_urlparse` validating the port before this function is
    ever reached — the live case today is the signed upload PUT url
    (:meth:`LinearBackend._upload_and_attach`), which is Linear's OWN API response, not
    something this process fully controls either. Wrapped as a clean, permanent
    :class:`HttpError`: ``InvalidURL`` fires from constructing/validating the request BEFORE
    anything is sent, so nothing reached the server (status ``0`` means "nothing was sent," not
    "safe to blindly retry" — the url itself won't become valid on a bare retry).

    Same scope note as :func:`request_json`: this does NOT attempt to reclassify a failure raised
    from reading back the response as clean-vs-ambiguous (that's :data:`_AMBIGUOUS_READ_EXCEPTIONS`
    above, already handled) or as a distinct third bucket — only the narrow, always-pre-send
    ``InvalidURL`` case is handled here.

    review finding (round 5): ``urllib.request.Request(url, ...)`` itself raises a bare
    ``ValueError`` for a bracket-malformed authority at CONSTRUCTION time, same bug class as
    ``InvalidURL`` but a step earlier. This is the gap that made ``_upload_and_attach``'s signed
    upload URL (Linear's OWN API response, not validated by ``_safe_urlparse`` before reaching
    here) still crash raw on a bracket-malformed ``uploadUrl`` even after the ``InvalidURL`` fix
    above — caught in its own narrow ``try`` around just construction, not folded into the
    ``opener(...)`` exception handling below, so it doesn't widen to catch an unrelated
    ``ValueError`` raised deeper inside the actual request.
    """
    hdrs = dict(headers or {})
    hdrs.setdefault("User-Agent", "task-cli")

    try:
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    except ValueError as exc:
        raise HttpError(0, f"invalid URL for {method} {url}: {exc}") from exc
    opener = _SAME_HOST_OPENER.open if same_host_redirects_only else urllib.request.urlopen
    try:
        with opener(req, timeout=timeout) as resp:  # noqa: S310 - trusted provider hosts
            try:
                return resp.read()
            except _AMBIGUOUS_READ_EXCEPTIONS as exc:
                raise AmbiguousHttpError(
                    f"connection interrupted while reading the response for {method} {url} "
                    f"(status {resp.status} was already returned) — the request may have "
                    "completed server-side even though this process never saw a usable body"
                ) from exc
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        raise HttpError(exc.code, f"HTTP {exc.code} for {method} {url}", body) from exc
    except urllib.error.URLError as exc:
        raise HttpError(0, f"connection failed for {method} {url}: {exc.reason}") from exc
    except http.client.InvalidURL as exc:
        raise HttpError(0, f"invalid URL for {method} {url}: {exc}") from exc
