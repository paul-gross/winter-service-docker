"""Shared pattern-matching helpers for env/service glob patterns.

These primitives are used by ``status``, ``logs``, ``restart``, and other
modules.  Extracted here so they can be imported without creating circular
dependencies.

Pattern syntax mirrors the tmux provider's pattern_match logic:
  - ``<env>/<svc>`` — exact or glob match on both segments
  - ``<bare>`` (no ``/``) — expands to ``<bare>/*`` (all services in env)
  - Wildcard env segments (``*``/``?``) cannot be resolved without scanning
    the filesystem; ``envs_from_patterns`` excludes them and callers that need
    to enumerate must scan ``WINTER_WORKSPACE_DIR`` themselves.
"""

from __future__ import annotations

import fnmatch


def service_matches_any_pattern(env_name: str, svc_name: str, patterns: list[str]) -> bool:
    """Return True if <env_name>/<svc_name> matches any pattern.

    A bare pattern (no '/') expands to ``<pattern>/*``.
    """
    if not patterns:
        return True
    for pattern in patterns:
        if "/" not in pattern:
            expanded = f"{pattern}/*"
        else:
            expanded = pattern
        env_pat, svc_pat = expanded.split("/", 1)
        if fnmatch.fnmatchcase(env_name, env_pat) and fnmatch.fnmatchcase(svc_name, svc_pat):
            return True
    return False


def envs_from_patterns(patterns: list[str]) -> list[str]:
    """Extract distinct env names from a list of patterns.

    A bare pattern (no '/') is itself an env name.  A ``<env>/<svc>`` pattern
    contributes the env segment.  Wildcard env segments (``*``) are excluded —
    they cannot be resolved to a concrete env name without scanning the
    filesystem; callers handle the empty-list case.
    """
    envs: list[str] = []
    seen: set[str] = set()
    for pat in patterns:
        if "/" not in pat:
            env_seg = pat
        else:
            env_seg = pat.split("/", 1)[0]
        if env_seg and "*" not in env_seg and "?" not in env_seg and env_seg not in seen:
            seen.add(env_seg)
            envs.append(env_seg)
    return envs


def has_glob(segment: str) -> bool:
    """Return True when *segment* contains a glob wildcard character."""
    return "*" in segment or "?" in segment


def env_segment_of(pattern: str) -> str:
    """Return the env segment of a pattern (left of ``/``, or the whole token)."""
    if "/" not in pattern:
        return pattern
    return pattern.split("/", 1)[0]
