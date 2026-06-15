"""task-cli — the enforced interface to the ticket system.

Every request becomes a durable, well-formed ticket the moment it arrives; ticket
*quality* (acceptance criteria, motivation, user-impact, cost-of-inaction, screenshots,
formatting) is enforced by the tool itself, not by convention. Backends: GitHub Issues
(default) and Linear (per-repo). Stdlib-only at import time; heavy work is lazy.
"""

from __future__ import annotations

__version__ = "0.1.0"
