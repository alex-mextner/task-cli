"""Structured JSONL logging in the ``agenttools_log`` shape, with secret redaction.

If the shared ``agenttools_log`` library is importable it is used directly (one shape across
the ecosystem). Otherwise this is a local stdlib-only equivalent: one JSON object per line,
``ts``/``level``/``logger``/``msg`` plus structured fields. The file sink is forced to ``0600``
and every field value is run through a redactor so a token can never land in a log line —
task-cli handles GitHub/Linear credentials and must never log them (§1).

Enable a file sink with ``$TASK_LOG_FILE`` (or ``$AGENTTOOLS_LOG_FILE`` when the shared lib is
present); otherwise records go to stderr only when ``$TASK_LOG=json``. Default is quiet.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

# patterns whose VALUE must be redacted before it can reach a log line.
_SECRET_KEY_RE = re.compile(r"(token|key|secret|password|authorization|api[_-]?key|bearer)", re.IGNORECASE)
# token-shaped values (gh ghp_/gho_, linear lin_api_, generic long opaque strings)
_SECRET_VALUE_RE = re.compile(
    r"(gh[opsureODU]_[A-Za-z0-9]{16,}|lin_api_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._-]{16,})"
)

_REDACTED = "<redacted>"


def redact(value: Any) -> Any:
    """Redact a token-shaped value. Strings are scrubbed; other types pass through."""
    if not isinstance(value, str):
        return value
    return _SECRET_VALUE_RE.sub(_REDACTED, value)


def _redact_fields(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in fields.items():
        if _SECRET_KEY_RE.search(key):
            out[key] = _REDACTED
        else:
            out[key] = redact(val)
    return out


def _emit_local(event: str, level: str, fields: dict[str, Any]) -> None:
    log_file = os.environ.get("TASK_LOG_FILE") or os.environ.get("AGENTTOOLS_LOG_FILE")
    want_json = os.environ.get("TASK_LOG", "").lower() == "json" or bool(log_file)
    if not want_json:
        return
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "level": level,
        "logger": "task",
        "msg": event,
    }
    payload.update(_redact_fields(fields))
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if log_file:
        try:
            # create 0600 if new, then append
            fd = os.open(log_file, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            return
    else:
        print(line, file=sys.stderr)


def log_event(event: str, level: str = "INFO", **fields: Any) -> None:
    """Emit a structured event. Field values are redacted; secret-named keys are masked.

    Prefers the shared ``agenttools_log`` library when present (same shape, ecosystem-wide);
    otherwise uses the local stdlib sink. Never raises into the caller.
    """
    safe = _redact_fields(fields)
    try:
        from agenttools_log import get_logger  # type: ignore[import-not-found]

        get_logger("task").log(level, event, **safe)  # type: ignore[attr-defined]
        return
    except Exception:  # noqa: BLE001 - the shared lib is optional; fall through to local
        pass
    try:
        _emit_local(event, level, fields)
    except Exception:  # noqa: BLE001 - logging must never break a ticket operation
        return
