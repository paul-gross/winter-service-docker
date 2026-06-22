"""Shared compose ps parsing and docker state/health mapping helpers.

These primitives are used by ``status``, ``lifecycle``, and other modules.
Extracted here so they can be imported without creating circular dependencies.

Docker → winter state/health mapping (per ``ai/provider-contract.md``):
  running + health=healthy   → state=running / health=healthy
  running + no healthcheck   → state=running / health=unknown
  running + health=unhealthy → state=running / health=unhealthy
  running + health=starting  → state=running / health=unknown  (coerce)
  exited / created / dead    → state=stopped / health=unknown

The compose ``ps --format json`` output is either:
  - One JSON object per line (compose v2 default)
  - A single top-level JSON array (some compose versions / flags)

Both encodings are handled: try line-delimited objects first, fall back to a
top-level array.
"""

from __future__ import annotations

import json

# ──────────────────────────────────────────────────────────────────────────────
# Docker → winter mapping helpers
# ──────────────────────────────────────────────────────────────────────────────

_RUNNING_STATES = frozenset({"running"})
_STOPPED_STATES = frozenset({"exited", "created", "dead", "removing", "paused"})


def map_docker_state(docker_state: str) -> str:
    """Map a docker container State value to a winter state string."""
    if docker_state in _RUNNING_STATES:
        return "running"
    if docker_state in _STOPPED_STATES:
        return "stopped"
    return "unknown"


def extract_health(container: dict) -> str | None:  # type: ignore[type-arg]
    """Extract the raw docker health string from a compose ps container dict.

    ``docker compose ps --format json`` emits ``Health`` as a plain STRING
    (``""`` when no healthcheck is declared, else ``"starting"`` /
    ``"healthy"`` / ``"unhealthy"``).  Some tooling/encodings nest it as
    ``{"Status": "..."}``; both shapes are handled defensively.

    Returns the lower-level health string, or ``None`` when no health is
    present (no healthcheck declared) — callers treat ``None`` as
    "no healthcheck".
    """
    health = container.get("Health")
    if isinstance(health, dict):
        health = health.get("Status")
    if not health or not isinstance(health, str):
        return None
    return health


def map_docker_health(docker_state: str, docker_health: str | None) -> str:
    """Map docker State + Health.Status to a winter health string.

    For non-running containers, health is always unknown.
    For running containers, map healthy/unhealthy/starting; absent or empty
    health (no healthcheck declared) → unknown.
    """
    if docker_state not in _RUNNING_STATES:
        return "unknown"
    if docker_health == "healthy":
        return "healthy"
    if docker_health == "unhealthy":
        return "unhealthy"
    # starting, unknown, absent, or empty → coerce to unknown
    return "unknown"


# ──────────────────────────────────────────────────────────────────────────────
# Compose ps JSON parsing
# ──────────────────────────────────────────────────────────────────────────────


def parse_compose_ps_output(text: str) -> list[dict]:  # type: ignore[type-arg]
    """Parse ``docker compose ps --format json`` output.

    Handles two encodings:
    1. One JSON object per line (compose v2 default).
    2. A single top-level JSON array (some compose versions).

    Returns a list of dicts (one per container).  Malformed lines are skipped.
    """
    stripped = text.strip()
    if not stripped:
        return []

    # Try top-level array first (wrap around complete output)
    if stripped.startswith("["):
        try:
            result = json.loads(stripped)
            if isinstance(result, list):
                return [item for item in result if isinstance(item, dict)]
        except json.JSONDecodeError:
            pass  # fall through to line-delimited

    # Try line-delimited objects
    containers: list[dict] = []  # type: ignore[type-arg]
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                containers.append(obj)
        except json.JSONDecodeError:
            continue
    return containers
