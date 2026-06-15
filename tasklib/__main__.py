"""``python -m tasklib`` entry — same dispatch as the ``task`` console script."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
