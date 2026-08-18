"""``tasklib.backends.http.request_json`` — the ambiguous-read failure class (task-cli bug).

A connection that drops WHILE reading the response body — after the server already returned a
2xx status — is genuinely ambiguous: the request may have completed server-side (e.g. GitHub
created the issue) even though this process never saw the body. Only ``urllib.error.HTTPError``/
``URLError`` were ever caught here; ``http.client.IncompleteRead``, ``ConnectionResetError``, and
a read-time ``TimeoutError`` (``socket.timeout`` is the same class on 3.10+) are none of those,
so they used to propagate as a bare, unhandled traceback even though the server-side call had
already succeeded. These tests pin the fix: such a failure is caught and re-raised as the new,
distinct :class:`AmbiguousHttpError` — never silently swallowed, never confused with a clean
:class:`HttpError` (a real non-2xx or a connect-time failure).
"""

from __future__ import annotations

import http.client
import ssl

import pytest

from tasklib.backends.http import AmbiguousHttpError, HttpError, request_json


class _FakeResponse:
    """Mimics the context-managed object ``urlopen`` returns: a 2xx status already landed,
    but ``.read()`` blows up mid-body — the exact ambiguous window this module must catch."""

    def __init__(self, status: int, read_exc: Exception) -> None:
        self.status = status
        self._read_exc = read_exc

    def read(self):
        raise self._read_exc

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False


@pytest.mark.parametrize(
    "read_exc",
    [
        http.client.IncompleteRead(partial=b"", expected=10),
        ConnectionResetError("connection reset by peer"),
        TimeoutError("timed out"),
        # review finding: both providers are HTTPS-only, and a connection dropped mid-TLS-read
        # typically surfaces as an SSL error, NOT a bare ConnectionResetError — the enumerated
        # allowlist this replaced missed exactly this, the most common real-world trigger.
        ssl.SSLError("decryption failed or bad record mac"),
        ssl.SSLEOFError("EOF occurred in violation of protocol"),
        ConnectionAbortedError("connection aborted"),
        BrokenPipeError("broken pipe"),
    ],
    ids=[
        "incomplete-read",
        "connection-reset",
        "read-timeout",
        "ssl-error",
        "ssl-eof",
        "connection-aborted",
        "broken-pipe",
    ],
)
def test_request_json_raises_ambiguous_when_read_fails_after_2xx(monkeypatch, read_exc):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(201, read_exc)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(AmbiguousHttpError) as exc:
        request_json("https://api.github.com/repos/o/r/issues", method="POST")
    # the message must be informative enough to act on: the method, the URL, and that a status
    # was already received (so the reader knows this isn't "nothing happened").
    assert "POST" in str(exc.value)
    assert "api.github.com" in str(exc.value)
    assert "201" in str(exc.value)


class _FakeResponseWithBody:
    """Mimics ``urlopen``'s returned object when the read itself succeeds but hands back a
    truncated/undecodable body — a connection that closes early WITHOUT tripping
    ``IncompleteRead`` can still leave the caller holding unusable bytes."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False


def test_request_json_raises_ambiguous_on_malformed_json_after_2xx(monkeypatch):
    # the read succeeded but the body is truncated mid-JSON — the SAME ambiguous window as an
    # IncompleteRead, just caught one step later (json.loads, not .read()); it must raise the
    # same AmbiguousHttpError, not an uncaught json.JSONDecodeError.
    def fake_urlopen(req, timeout=None):
        return _FakeResponseWithBody(201, b'{"id": 42, "title": "trunc')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(AmbiguousHttpError):
        request_json("https://api.github.com/repos/o/r/issues", method="POST")


def test_request_json_raises_ambiguous_on_undecodable_body_after_2xx(monkeypatch):
    # a body truncated mid-multibyte-character fails at .decode("utf-8"), before json.loads even
    # runs — same ambiguous window again.
    def fake_urlopen(req, timeout=None):
        return _FakeResponseWithBody(201, "café".encode("utf-8")[:-1])

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(AmbiguousHttpError):
        request_json("https://api.github.com/repos/o/r/issues", method="POST")


def test_ambiguous_http_error_is_not_an_http_error():
    # a caller's `except HttpError` (the "clean 4xx / connect failure" bucket) must NOT also
    # catch this — that would make a genuine server-side success indistinguishable from a
    # clean, nothing-happened refusal.
    assert not issubclass(AmbiguousHttpError, HttpError)


def test_request_json_still_raises_http_error_for_a_normal_4xx(monkeypatch):
    # unrelated regression guard: the pre-existing clean-failure path must be untouched.
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 422, "Unprocessable", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(HttpError) as exc:
        request_json("https://api.github.com/repos/o/r/issues", method="POST")
    assert exc.value.status == 422


def test_request_json_wraps_invalid_url_port_as_http_error(monkeypatch):
    # review finding: request_bytes's InvalidURL fix (below) applies verbatim to request_json —
    # a malformed authority (e.g. an ambient GITHUB_API_URL override with a bad port) raises the
    # identical bare http.client.InvalidURL here too, escaping the HTTPError/URLError-only
    # except clauses and crashing the caller instead of a clean BackendError. InvalidURL always
    # fires BEFORE anything is sent (constructing/validating the request), so it's unambiguously
    # safe to classify as a clean, retryable HttpError — unlike a failure raised from reading
    # back a response, which is a materially harder classification problem intentionally left
    # out of scope (see the docstring and the "left deliberately unhandled" test below).
    def fake_urlopen(req, timeout=None):
        raise http.client.InvalidURL("nonnumeric port: 'bad'")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(HttpError):
        request_json("https://ghe.corp:bad/api/v3/repos/o/r/issues", method="POST")


def test_request_json_wraps_construction_time_valueerror_as_http_error():
    # review finding (round 5): a bracket-malformed authority ("https://[bad") makes
    # urllib.request.Request() itself raise a bare ValueError at CONSTRUCTION time — a step
    # earlier than the connect-time InvalidURL the test above covers. Left unguarded, this
    # escapes raw before urlopen is even reached (no monkeypatch needed here: construction fails
    # before any network call would happen, so this is a real, hermetic exercise of the actual
    # bug, not a simulation of it).
    with pytest.raises(HttpError):
        request_json("https://[bad/api/v3/repos/o/r/issues", method="POST")


def test_request_json_leaves_bad_status_line_unhandled_by_design(monkeypatch):
    # scope-boundary regression guard (round 2 → round 3 review history): an EARLIER version of
    # this fix widened the catch to the whole http.client.HTTPException family and tried to
    # split it into "clean" (pre-send) vs "ambiguous" (post-send) buckets. Three independent
    # reviewers then found that split itself was unsound — http.client.RemoteDisconnected is
    # simultaneously an OSError (→ urllib wraps it into URLError, a "clean" bucket) AND a
    # BadStatusLine (→ the "ambiguous" bucket), so which bucket it lands in depends on
    # CPython/urllib internals, not on anything this module controls; a raw OSError escaping
    # getresponse() has the identical problem. Rather than chase that classification into
    # CPython version-sensitive territory, the scope was pulled back to ONLY the unambiguous
    # InvalidURL case (see request_json's/request_bytes's docstrings). This test pins that
    # BadStatusLine is a PRE-EXISTING, deliberately-untouched crash mode — not a silent
    # regression — so a future change doesn't "fix" it by accidentally reopening the unsound
    # split.
    def fake_urlopen(req, timeout=None):
        raise http.client.BadStatusLine("garbage status line")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(http.client.BadStatusLine):
        request_json("https://api.github.com/repos/o/r/issues", method="POST")


# ── request_bytes + the same-host redirect guard (credential-exfiltration hazard) ──────────


def test_request_bytes_wraps_invalid_url_port_as_http_error(monkeypatch):
    # review finding (PR #90, task-cli): a tracker-controlled attachment url like
    # "https://uploads.linear.app:bad/asset" parses fine at urlparse()-time — `.hostname` doesn't
    # validate the port — but http.client raises a BARE http.client.InvalidURL the moment it
    # tries to actually open the connection. That's neither urllib.error.HTTPError nor URLError,
    # so left uncaught it propagates past every caller's HttpError-only `except` clause (e.g.
    # LinearBackend.fetch_attachment_bytes) and crashes the whole `task read --save-attachments`
    # command instead of failing just the one malformed attachment. Must be wrapped in HttpError.
    from tasklib.backends.http import request_bytes

    def fake_urlopen(req, timeout=None):
        raise http.client.InvalidURL("nonnumeric port: 'bad'")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(HttpError) as exc:
        request_bytes("https://uploads.linear.app:bad/asset")
    assert "invalid url" in str(exc.value).lower()


def test_request_bytes_wraps_invalid_url_port_through_guarded_opener(monkeypatch):
    # same fix, through the OTHER opener path (same_host_redirects_only=True) — this is the one
    # LinearBackend.fetch_attachment_bytes actually uses for a native asset url (it always passes
    # same_host_redirects_only=is_linear_asset), so the plain-opener test above alone wouldn't
    # catch a fix that only patched one of the two call paths.
    from tasklib.backends import http as http_mod

    class _FakeOpener:
        def open(self, req, timeout=None):
            raise http.client.InvalidURL("nonnumeric port: 'bad'")

    monkeypatch.setattr(http_mod, "_SAME_HOST_OPENER", _FakeOpener())
    with pytest.raises(HttpError):
        http_mod.request_bytes("https://uploads.linear.app:bad/asset", same_host_redirects_only=True)


def test_request_bytes_wraps_construction_time_valueerror_as_http_error():
    # review finding (round 5): same construction-time ValueError as request_json's counterpart
    # test above, through request_bytes — this is the function LinearBackend._upload_and_attach
    # calls with Linear's OWN (not fully trusted) signed uploadUrl, so this gap was live in
    # exactly the case the InvalidURL docstring already called out as "the live case today".
    # Hermetic: construction fails before urlopen is reached, no monkeypatch needed.
    from tasklib.backends.http import request_bytes

    with pytest.raises(HttpError):
        request_bytes("https://[bad/asset")


def test_request_bytes_returns_raw_body(monkeypatch):
    from tasklib.backends.http import request_bytes

    class _FakeResp:
        status = 200

        def read(self):
            return b"\x89PNG raw bytes"

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _FakeResp())
    assert request_bytes("https://uploads.linear.app/asset-1") == b"\x89PNG raw bytes"


def test_same_host_redirect_handler_allows_same_host_redirect():
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("https://uploads.linear.app/a")
    # same host + path change → delegates to the stdlib handler (returns a new Request, doesn't raise)
    result = handler.redirect_request(req, None, 302, "Found", {}, "https://uploads.linear.app/b")
    assert result is not None
    assert result.full_url == "https://uploads.linear.app/b"


def test_same_host_redirect_handler_refuses_cross_host_redirect():
    import urllib.error
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("https://uploads.linear.app/a")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://attacker.example.com/steal")


def test_same_host_redirect_handler_refuses_https_to_http_downgrade_same_host():
    # review finding: a same-HOST check alone misses a same-host SCHEME downgrade — a
    # compromised uploads.linear.app could 302 https -> http on the identical hostname and a
    # host-only guard would wave the Authorization header straight through onto plaintext.
    import urllib.error
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("https://uploads.linear.app/a")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://uploads.linear.app/a")


def test_same_host_redirect_handler_allows_http_to_https_upgrade_same_host():
    # the inverse (http -> https) is a safe upgrade, not a downgrade — must still be allowed.
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("http://uploads.linear.app/a")
    result = handler.redirect_request(req, None, 302, "Found", {}, "https://uploads.linear.app/a")
    assert result is not None


def test_request_bytes_same_host_redirects_only_uses_guarded_opener(monkeypatch):
    # request_bytes must route through the guarded opener (not plain urlopen) when the caller
    # says the request carries a credential — this is the load-bearing wiring the review finding
    # was about; a unit test on the handler alone can't catch a caller forgetting to opt in.
    from tasklib.backends import http as http_mod

    seen = {}

    class _FakeOpener:
        def open(self, req, timeout=None):
            seen["used_guarded_opener"] = True

            class _Resp:
                status = 200

                def read(self):
                    return b"ok"

                def __enter__(self):
                    return self

                def __exit__(self, *exc_info):
                    return False

            return _Resp()

    monkeypatch.setattr(http_mod, "_SAME_HOST_OPENER", _FakeOpener())
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not use the plain opener")),
    )
    result = http_mod.request_bytes("https://uploads.linear.app/asset-1", same_host_redirects_only=True)
    assert result == b"ok"
    assert seen.get("used_guarded_opener") is True
