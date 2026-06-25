"""Status command implementation for winter-service-docker.

Queries ``docker compose ps --all --format json`` for each target env and maps
the docker container state/health into winter's env-keyed status document.

Docker → winter state/health mapping (per ``ai/provider-contract.md``):
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
"""

from __future__ import annotations

import sys
from pathlib import Path

from docker_orchestrator.compose_client import IComposeClient
from docker_orchestrator.compose_ps import (
    extract_health,
    map_docker_health,
    map_docker_state,
    parse_compose_ps_output,
)
from docker_orchestrator.env_context import build_env_context, resolve_env_file
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
# Env enumeration from workspace directory
# ──────────────────────────────────────────────────────────────────────────────

_WINTER_ENV_FILE = ".winter.env"


def _enumerate_workspace_envs(workspace_root: Path) -> list[str]:
    """Scan ``workspace_root`` for immediate subdirs that contain ``.winter.env``.

    These are feature envs.  The workspace root itself is excluded (it has no
    ``.winter.env`` at its own level).  Returns env names in filesystem order.
    """
    envs: list[str] = []
    try:
        for entry in sorted(workspace_root.iterdir()):
            if not entry.is_dir():
                continue
            if (entry / _WINTER_ENV_FILE).is_file():
                envs.append(entry.name)
    except OSError:
        pass
    return envs


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
    workspace_root: Path,
    client: IComposeClient,
    patterns: list[str],
) -> dict:  # type: ignore[type-arg]
    """Build the env status entry by querying docker compose ps.

    Returns a dict conforming to the winter env-keyed status document env shape.
    """
    # Require manifest fields
    if not manifest.project_prefix or not manifest.compose_file:
        print(
            f"docker-orchestrator: status: manifest is missing project_prefix or compose_file for env '{env}'",
            file=sys.stderr,
        )
        return {
            "env": env,
            "session": None,
            "port_base": None,
            "services": [],
        }

    ctx = build_env_context(env, manifest.project_prefix, workspace_root)
    compose_file = manifest.compose_file

    result = client.compose(
        ctx.compose_project_name,
        compose_file,
        ["ps", "--all", "--format", "json"],
        capture_output=True,
        source_env_file=resolve_env_file(workspace_root, env),
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
        "port_base": ctx.port_base,
        "services": service_entries,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Top-level status command
# ──────────────────────────────────────────────────────────────────────────────

import json  # noqa: E402  — import here to keep the module-level grouping readable


def cmd_status(
    patterns: list[str],
    manifest: DockerManifest,
    workspace_root: Path,
    client: IComposeClient,
) -> int:
    """Implement the ``status [<pattern>...]`` action.

    Emits winter's env-keyed status document as JSON on stdout, exits 0.

    When no patterns are supplied (or all pattern env-segments are wildcards),
    candidate envs are enumerated by scanning ``workspace_root`` for immediate
    subdirectories that contain a ``.winter.env`` file.  When a pattern's
    env-segment contains a glob (``*``/``?``), it is matched against the
    enumerated env names via fnmatch.  The reserved ``workspace`` token
    continues to resolve exactly (it is not a glob).  Stdout stays pure JSON.
    """
    # Derive concrete env names from patterns, then add any that need enumeration.
    concrete_envs = envs_from_patterns(patterns)
    concrete_set = set(concrete_envs)

    # Check whether any pattern has a wildcard env-segment or patterns is empty.
    needs_enumeration = not patterns or any(has_glob(p.split("/", 1)[0] if "/" in p else p) for p in patterns)

    if needs_enumeration:
        enumerated = _enumerate_workspace_envs(workspace_root)
        for env in enumerated:
            if env not in concrete_set:
                # Apply wildcard env patterns against enumerated envs
                if not patterns:
                    # No patterns → include all enumerated envs
                    concrete_envs.append(env)
                    concrete_set.add(env)
                else:
                    # Has patterns but some have wildcard env-segments — check
                    # whether any of those wildcard patterns matches this env.
                    import fnmatch as _fnmatch

                    for pat in patterns:
                        env_seg = pat.split("/", 1)[0] if "/" in pat else pat
                        if has_glob(env_seg) and _fnmatch.fnmatchcase(env, env_seg):
                            concrete_envs.append(env)
                            concrete_set.add(env)
                            break

    env_docs: list[dict] = []  # type: ignore[type-arg]
    for env in concrete_envs:
        env_doc = _status_for_env(env, manifest, workspace_root, client, patterns)
        env_docs.append(env_doc)

    doc = {"envs": env_docs}
    sys.stdout.write(json.dumps(doc) + "\n")
    sys.stdout.flush()
    return 0
