"""Status command implementation for winter-service-docker.

Queries ``docker compose ps --all --format json`` for each target env and maps
the docker container state/health into winter's env-keyed status document.

Docker → winter state/health mapping (per ``context/provider-contract.md``):
  running + health=healthy   → state=running / health=healthy
  running + no healthcheck   → state=running / health=unknown
  running + health=unhealthy → state=running / health=unhealthy
  running + health=starting  → state=running / health=unknown  (coerce)
  exited / created / dead    → state=stopped / health=unknown

The compose ``ps --all --format json`` output is either:
  - One JSON object per line (compose v2 default)
  - A single top-level JSON array (some compose versions / flags)

Both encodings are handled: try line-delimited objects first, fall back to a
top-level array.

Scope identification (Phase 3 — core injection):
  Winter-cli core invokes this provider as ``status <scope>/*`` (e.g.
  ``status alpha/*`` or ``status workspace/*``) with ``WINTER_ENV``,
  ``WINTER_ENV_INDEX``, and ``WINTER_PORT_BASE`` already present in the
  process environment.  The provider reads the scope from the ``<scope>/*``
  pattern argument (primary source); ``WINTER_ENV`` is available in the
  environment as a cross-check.  The provider does NOT enumerate envs or
  read any per-env file on the status path — core injects all variables.
"""

from __future__ import annotations

import os
import sys

from docker_orchestrator.compose_client import IComposeClient
from docker_orchestrator.compose_ps import (
    extract_health,
    map_docker_health,
    map_docker_state,
    parse_compose_ps_output,
)
from docker_orchestrator.env_context import WORKSPACE_SCOPE
from docker_orchestrator.env_context import compose_project_name as _compose_project_name
from docker_orchestrator.manifest import DockerManifest
from docker_orchestrator.patterns import envs_from_patterns, has_glob, service_matches_any_pattern

# ──────────────────────────────────────────────────────────────────────────────
# Backward-compat aliases (consumed by existing tests and other modules)
# ──────────────────────────────────────────────────────────────────────────────

_map_docker_state = map_docker_state
_map_docker_health = map_docker_health
_parse_compose_ps_output = parse_compose_ps_output
_service_matches_any_pattern = service_matches_any_pattern
_envs_from_patterns = envs_from_patterns


# ──────────────────────────────────────────────────────────────────────────────
# Port extraction (status-only; not shared elsewhere)
# ──────────────────────────────────────────────────────────────────────────────


def _extract_ports(publishers: object) -> list[int]:
    """Extract published host ports from the compose ps Publishers field.

    ``Publishers`` is a list of dicts with a ``PublishedPort`` int key.
    Non-list, non-dict, or non-int values are silently dropped.
    """
    if not isinstance(publishers, list):
        return []
    ports: list[int] = []
    seen: set[int] = set()
    for pub in publishers:
        if not isinstance(pub, dict):
            continue
        port = pub.get("PublishedPort")
        if isinstance(port, int) and not isinstance(port, bool) and port > 0 and port not in seen:
            seen.add(port)
            ports.append(port)
    return ports


# ──────────────────────────────────────────────────────────────────────────────
# Port-substitution helpers (status path)
# ──────────────────────────────────────────────────────────────────────────────


def _port_env_vars_for_status(services: tuple, port_base: int) -> dict[str, str]:
    """Build ``WSD_PORT_<SVC>`` vars for the compose ps invocation.

    Mirrors the same computation in ``lifecycle._port_env_vars`` so that
    ``docker compose ps`` resolves placeholders the same way ``up`` does.
    """
    return {f"WSD_PORT_{svc.name.upper()}": str(port_base + i) for i, svc in enumerate(services)}


def _build_status_compose_env(
    project_name: str,
    scoped_services: tuple,
    port_base: int | None,
) -> dict[str, str]:
    """Build the environment dict for the status compose ps call.

    Starts from the current process environment (which already contains the
    core-injected ``WINTER_ENV``/``WINTER_ENV_INDEX``/``WINTER_PORT_BASE`` and
    every variable core injected for the scope), then overlays
    ``COMPOSE_PROJECT_NAME`` and, when *port_base* is not None, the
    ``WSD_PORT_*`` port-substitution vars.  No env file is read
    here — core injects the scope's variables before invoking this provider.
    """
    env: dict[str, str] = dict(os.environ)
    env["COMPOSE_PROJECT_NAME"] = project_name
    if port_base is not None:
        env.update(_port_env_vars_for_status(scoped_services, port_base))
    return env


# ──────────────────────────────────────────────────────────────────────────────
# Per-env status builder
# ──────────────────────────────────────────────────────────────────────────────


def _build_service_entry(container: dict, svc_name: str) -> dict:  # type: ignore[type-arg]
    """Build one service entry for the winter status document from a compose ps dict."""
    docker_state: str = container.get("State", "") or ""
    # docker compose ps --format json emits Health as a plain string
    # ("", "starting", "healthy", "unhealthy"); extract_health also tolerates
    # the nested {"Status": ...} form defensively.
    docker_health: str | None = extract_health(container)

    state = map_docker_state(docker_state)
    health = map_docker_health(docker_state, docker_health)

    publishers = container.get("Publishers")
    ports = _extract_ports(publishers)

    # handle: container Name is more readable than ID
    handle: str | None = container.get("Name") or container.get("ID") or None

    # since: use the container's RunningFor or StartedAt field if available
    since: str | None = None
    # Some compose ps JSON versions expose "StartedAt" directly
    started_at = container.get("StartedAt")
    if isinstance(started_at, str) and started_at:
        since = started_at

    return {
        "name": svc_name,
        "state": state,
        "health": health,
        "ports": ports,
        "handle": handle,
        "log_path": None,
        "since": since,
    }


def _status_for_env(
    env: str,
    manifest: DockerManifest,
    client: IComposeClient,
    patterns: list[str],
) -> dict:  # type: ignore[type-arg]
    """Build the env status entry by querying docker compose ps.

    Returns a dict conforming to the winter env-keyed status document env shape.

    On the status path, ``WINTER_PORT_BASE`` is read from the process environment
    (injected by winter-cli core) rather than from any per-env file.  Core injects
    the scope's variables before invoking this provider.
    """
    # Require manifest fields
    compose_file = manifest.compose_file_for_scope(env)
    if not manifest.project_prefix or not compose_file:
        print(
            f"docker-orchestrator: status: manifest is missing project_prefix or compose file for scope of env '{env}'",
            file=sys.stderr,
        )
        return {
            "env": env,
            "session": None,
            "port_base": None,
            "services": [],
        }

    # Read the scope's port base from the process environment (injected by core).
    # Do NOT read any per-env file on the status path — core injects it.
    # The workspace scope exposes its band as WINTER_WORKSPACE_PORT_BASE (it has
    # no per-env WINTER_PORT_BASE); per-env scopes use WINTER_PORT_BASE.
    import contextlib

    port_base: int | None = None
    port_base_var = "WINTER_WORKSPACE_PORT_BASE" if env == WORKSPACE_SCOPE else "WINTER_PORT_BASE"
    raw_port_base = os.environ.get(port_base_var)
    if raw_port_base is not None:
        with contextlib.suppress(ValueError):
            port_base = int(raw_port_base)

    project_name = _compose_project_name(manifest.project_prefix, env)
    scoped_services = manifest.services_for_scope(env)
    compose_env = _build_status_compose_env(project_name, scoped_services, port_base)

    result = client.compose(
        project_name,
        compose_file,
        ["ps", "--all", "--format", "json"],
        capture_output=True,
        env=compose_env,
    )

    containers = parse_compose_ps_output(result.stdout or "")

    # Build a map from compose service name → container dicts (may be multiple replicas)
    svc_to_containers: dict[str, list[dict]] = {}  # type: ignore[type-arg]
    for ct in containers:
        svc = ct.get("Service") or ""
        if svc:
            svc_to_containers.setdefault(svc, []).append(ct)

    # Build service entries from the scope-correct declared service list.
    # If compose reports services not in the manifest, we still include them.
    declared_names = [s.name for s in manifest.services_for_scope(env)]

    # Determine which service names to report: declared + any compose-returned extras
    all_svc_names: list[str] = list(declared_names)
    for svc in svc_to_containers:
        if svc not in declared_names:
            all_svc_names.append(svc)

    # If manifest has no declared services but compose returned some, use compose names
    # If manifest has declared services and compose returned nothing, report stopped state for each declared service
    service_entries: list[dict] = []  # type: ignore[type-arg]

    for svc_name in all_svc_names:
        # Apply pattern filter
        if not service_matches_any_pattern(env, svc_name, patterns):
            continue

        if svc_name in svc_to_containers:
            # Use the first container (single replica case)
            ct = svc_to_containers[svc_name][0]
            entry = _build_service_entry(ct, svc_name)
        else:
            # Declared but not returned by compose ps → stopped/unknown
            entry = {
                "name": svc_name,
                "state": "stopped",
                "health": "unknown",
                "ports": [],
                "handle": None,
                "log_path": None,
                "since": None,
            }

        service_entries.append(entry)

    return {
        "env": env,
        "session": None,
        "port_base": port_base,
        "services": service_entries,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Top-level status command
# ──────────────────────────────────────────────────────────────────────────────

import json  # noqa: E402  — import here to keep the module-level grouping readable


def cmd_status(
    patterns: list[str],
    manifest: DockerManifest,
    client: IComposeClient,
) -> int:
    """Implement the ``status [<pattern>...]`` action.

    Emits winter's env-keyed status document as JSON on stdout, exits 0.

    **Phase 3 — core injection contract.**  Winter-cli core invokes this
    provider once per scope as ``status <scope>/*`` (e.g. ``status alpha/*``
    or ``status workspace/*``) with ``WINTER_ENV``, ``WINTER_ENV_INDEX``, and
    ``WINTER_PORT_BASE`` (plus every variable sourced from the scope's env
    file) already present in the process environment.

    Scope identification precedence:
    1. The env segment of the first pattern argument (``<scope>/*`` → ``scope``).
       Core always supplies a concrete, non-wildcard scope here.
    2. ``WINTER_ENV`` from the process environment (cross-check / fallback when
       patterns is empty — should not happen in normal core-driven calls).

    The provider does NOT enumerate envs from the filesystem and does NOT
    read any per-env file on the status path — core injects the variables.
    ``WINTER_PORT_BASE`` is read from ``os.environ``.

    Service-level pattern filtering (the ``/<svc>`` segment) is still applied
    within the single resolved scope so scope-qualified patterns like
    ``alpha/db`` work correctly.
    """
    # Resolve the single concrete scope from the pattern argument.
    # Core passes exactly one concrete scope per call (e.g. "alpha/*").
    env: str | None = None
    if patterns:
        env_seg = patterns[0].split("/", 1)[0] if "/" in patterns[0] else patterns[0]
        if env_seg and not has_glob(env_seg):
            env = env_seg

    # Fallback: read WINTER_ENV from the injected process environment.
    if env is None:
        env = os.environ.get("WINTER_ENV") or None

    if env is None:
        # No scope available — emit empty document; this should not happen in
        # normal core-driven calls.
        doc: dict = {"envs": []}  # type: ignore[type-arg]
        sys.stdout.write(json.dumps(doc) + "\n")
        sys.stdout.flush()
        return 0

    env_doc = _status_for_env(env, manifest, client, patterns)

    doc = {"envs": [env_doc]}
    sys.stdout.write(json.dumps(doc) + "\n")
    sys.stdout.flush()
    return 0
