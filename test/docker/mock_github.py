#!/usr/bin/env python3
"""mock_github.py — a hermetic, in-memory stand-in for the GitHub Issues REST API.

Why this exists: the Docker integration test must prove ``task new``/``task list`` work
*against the real backend code path* (``tasklib/backends/github_issues.py`` → stdlib
``urllib`` → REST) WITHOUT a real token or network. The backend honors ``GITHUB_API_URL``
(the same env ``gh``/octokit read), so pointing it at this server exercises the actual
create/list/get/patch/comment requests against a controlled endpoint — deterministic,
offline, no credentials.

It implements only the endpoints ``GitHubIssuesBackend`` calls:
  POST   /repos/{owner}/{repo}/issues                  create
  GET    /repos/{owner}/{repo}/issues?...              list (per_page/state/labels)
  GET    /repos/{owner}/{repo}/issues/{n}              get
  PATCH  /repos/{owner}/{repo}/issues/{n}              update
  POST   /repos/{owner}/{repo}/issues/{n}/comments     comment
  GET    /search/issues?q=...                          search

Stdlib-only; no task-cli import (kept independent so a backend refactor can't make the mock
silently lie). State is process-local; restarting the server resets the store.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

# ── in-memory store ──────────────────────────────────────────────────────────────────
_LOCK = Lock()
_ISSUES: list[dict] = []
_NEXT = [1]

_ISSUE_RE = re.compile(r"^/repos/([^/]+)/([^/]+)/issues$")
_ISSUE_N_RE = re.compile(r"^/repos/[^/]+/[^/]+/issues/(\d+)$")
_COMMENTS_RE = re.compile(r"^/repos/[^/]+/[^/]+/issues/(\d+)/comments$")


def _normalize_labels(raw) -> list[dict]:
    """GitHub returns labels as objects ({name: ...}); accept str-or-object on input."""
    out = []
    for lbl in raw or []:
        name = lbl["name"] if isinstance(lbl, dict) else str(lbl)
        out.append({"name": name})
    return out


def _new_issue(payload: dict, owner: str = "mock", repo: str = "mock") -> dict:
    with _LOCK:
        number = _NEXT[0]
        _NEXT[0] += 1
        # Build html_url from the request path's owner/repo (not a hardcoded mock/mock), so the
        # mock stays faithful if pointed at a differently-named test repo.
        issue = {
            "number": number,
            "title": payload.get("title", ""),
            "body": payload.get("body", ""),
            "labels": _normalize_labels(payload.get("labels")),
            "state": payload.get("state", "open"),
            "html_url": f"https://github.com/{owner}/{repo}/issues/{number}",
            "comments_log": [],
        }
        _ISSUES.append(issue)
    return issue


def _public(issue: dict) -> dict:
    """The view GitHub returns — without our internal comments_log bookkeeping."""
    return {k: v for k, v in issue.items() if k != "comments_log"}


def _find(number: int) -> dict | None:
    for issue in _ISSUES:
        if issue["number"] == number:
            return issue
    return None


def _filter_list(query: dict) -> list[dict]:
    state = (query.get("state") or ["all"])[0]
    want_labels = set()
    if "labels" in query:
        for chunk in query["labels"]:
            want_labels.update(p for p in chunk.split(",") if p)
    out = []
    for issue in _ISSUES:
        if state != "all" and issue["state"] != state:
            continue
        names = {lbl["name"] for lbl in issue["labels"]}
        if want_labels and not want_labels.issubset(names):
            continue
        out.append(_public(issue))
    return out


def _search(query: dict) -> dict:
    # Honor the `q=...` text loosely: match on title/body substring of the bare terms,
    # skipping the qualifier tokens (repo:/is:) the backend prepends.
    q = (query.get("q") or [""])[0]
    terms = [t for t in q.split() if ":" not in t]
    items = []
    for issue in _ISSUES:
        hay = (issue["title"] + " " + issue["body"]).lower()
        if all(t.lower() in hay for t in terms):
            items.append(_public(issue))
    return {"total_count": len(items), "items": items}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # silence per-request noise
        pass

    def _send(self, code: int, body) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_payload(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # ── GET ──────────────────────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802 (http.server contract)
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path
        if path == "/search/issues":
            return self._send(200, _search(query))
        if _ISSUE_RE.match(path):
            return self._send(200, _filter_list(query))
        m = _ISSUE_N_RE.match(path)
        if m:
            issue = _find(int(m.group(1)))
            if issue is None:
                return self._send(404, {"message": "Not Found"})
            return self._send(200, _public(issue))
        return self._send(404, {"message": f"no route {path}"})

    # ── POST ─────────────────────────────────────────────────────────────────────────
    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        payload = self._read_payload()
        m_issue = _ISSUE_RE.match(path)
        if m_issue:
            owner, repo = m_issue.group(1), m_issue.group(2)
            return self._send(201, _public(_new_issue(payload, owner, repo)))
        m = _COMMENTS_RE.match(path)
        if m:
            issue = _find(int(m.group(1)))
            if issue is None:
                return self._send(404, {"message": "Not Found"})
            issue["comments_log"].append(payload.get("body", ""))
            return self._send(201, {"id": len(issue["comments_log"]), "body": payload.get("body", "")})
        return self._send(404, {"message": f"no route {path}"})

    # ── PATCH ────────────────────────────────────────────────────────────────────────
    def do_PATCH(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        m = _ISSUE_N_RE.match(path)
        if not m:
            return self._send(404, {"message": f"no route {path}"})
        issue = _find(int(m.group(1)))
        if issue is None:
            return self._send(404, {"message": "Not Found"})
        payload = self._read_payload()
        for key in ("title", "body", "state"):
            if key in payload:
                issue[key] = payload[key]
        if "labels" in payload:
            issue["labels"] = _normalize_labels(payload["labels"])
        return self._send(200, _public(issue))


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8771
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock-github: listening on http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
