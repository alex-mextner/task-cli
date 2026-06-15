#!/usr/bin/env bash
# smoke.sh — fast end-to-end check of the task CLI surface, run in CI and locally.
# Proves: --help / --version, every subcommand --help, the pure-core import is stdlib-only,
# zero-config defaults load, and the pytest unit suite. It NEVER hits live GitHub/Linear
# (no token is used) — backend calls are exercised by the FakeBackend in the unit suite.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python3}"
TASK="$PY $ROOT/bin/task"

pass() { printf '  \033[32m✔\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗ %s\033[0m\n' "$1"; exit 1; }

echo "task smoke — $ROOT"

# ── 1. --help / --version ─────────────────────────────────────────────────────
$TASK --help    >/dev/null 2>&1 || fail "task --help"
$TASK --version >/dev/null 2>&1 || fail "task --version"
pass "task --help / --version"

# ── 2. every subcommand --help ────────────────────────────────────────────────
for sub in create list read view find change status classify session install-skill; do
  $TASK "$sub" --help >/dev/null 2>&1 || fail "task $sub --help"
done
pass "every subcommand --help"

# ── 3. import-time stdlib-only (no yaml/urllib pulled in at module load) ───────
$PY - <<'PYEOF' || fail "lazy-import check"
import sys
sys.path.insert(0, ".")
import tasklib.cli  # noqa: F401
assert "yaml" not in sys.modules, "yaml imported at module load"
assert "urllib.request" not in sys.modules, "urllib.request imported at module load"
PYEOF
pass "stdlib-only at import time"

# ── 4. zero-config defaults load (no task.yaml, isolated config home) ──────────
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
XDG_CONFIG_HOME="$TMP/cfg" $PY - "$TMP" <<'PYEOF' || fail "zero-config load"
import sys
sys.path.insert(0, ".")
from pathlib import Path
from tasklib.config import load
cfg = load(Path(sys.argv[1]))
assert cfg.backend == "github-issues", cfg.backend
PYEOF
pass "zero-config defaults (github-issues)"

# ── 5. pytest unit suite ──────────────────────────────────────────────────────
if $PY -c 'import pytest' 2>/dev/null; then
  ( cd "$ROOT" && $PY -m pytest -q ) || fail "pytest"
  pass "pytest unit suite"
else
  printf '  \033[33m○ skip\033[0m pytest (not installed)\n'
fi

echo "task smoke: all checks passed"
