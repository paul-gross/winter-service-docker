"""Per-env context derivation for winter-service-docker.

Given an env name (or the reserved ``workspace`` scope), this module computes:

- ``COMPOSE_PROJECT_NAME`` = ``<prefix>-<env>``  (or ``<prefix>-workspace``),
  where ``<prefix>`` is resolved by ``resolve_project_prefix`` — see below.
- ``port_base`` by reading ``WINTER_PORT_BASE`` from the process environment
  (injected by winter-cli core via ``EnvProvisionerService`` before invoking the
  provider subprocess — no env file is read or sourced)

Project-name prefix resolution:
    The prefix is resolved by ``resolve_project_prefix(manifest_override)``:
    the manifest's optional ``project_prefix`` key (an explicit per-provider
    override), when set, takes precedence; otherwise the value falls back to
    ``WINTER_SERVICE_PREFIX`` read from the process environment.
    ``WINTER_SERVICE_PREFIX`` is injected by winter-cli core (resolved from the
    workspace-level ``service_prefix`` config, folding the deprecated
    ``session_prefix`` key) as a base extension var, present on every dispatch
    action (``up``, ``down``, ``status``, ``restart``, ``logs``, ``describe``,
    ``catalog``) — so ``project_prefix`` is purely optional on every action,
    needed only if a workspace wants this provider specifically to use a
    different prefix than the rest of the workspace.

Port derivation:
    ``WINTER_PORT_BASE`` in ``os.environ`` is the base for all published host
    ports.  Callers can use ``published_port(base, offset)`` to compute a
    specific port.

The ``workspace`` scope is the reserved name for the workspace-scoped singleton
session.  Core injects its own ``WINTER_PORT_BASE`` for the workspace scope too,
so ``read_port_base`` returns it consistently for all scopes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

WORKSPACE_SCOPE = "workspace"


def read_port_base() -> int | None:
    """Read ``WINTER_PORT_BASE`` from the process environment.

    Winter-cli core computes and injects ``WINTER_PORT_BASE`` into the provider
    subprocess environment via ``EnvProvisionerService`` before invoking any
    action.  Providers read it from ``os.environ`` — no env file is accessed.

    Returns the integer port base, or ``None`` when the key is absent or
    non-integer.
    """
    raw = os.environ.get("WINTER_PORT_BASE")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def compose_project_name(project_prefix: str, env: str) -> str:
    """Compute ``COMPOSE_PROJECT_NAME`` for *env* under *project_prefix*.

    Returns ``<prefix>-<env>`` for all env names, including the reserved
    ``workspace`` scope (which yields ``<prefix>-workspace``).
    """
    return f"{project_prefix}-{env}"


def read_service_prefix() -> str | None:
    """Read ``WINTER_SERVICE_PREFIX`` from the process environment.

    Winter-cli core computes and injects ``WINTER_SERVICE_PREFIX`` into the
    provider subprocess environment via ``EnvProvisionerService`` (resolved
    from the workspace-level ``service_prefix`` config key, defaulting to
    ``"winter"`` and folding the deprecated ``session_prefix`` key for
    back-compat) as a base extension var, present on every dispatch action —
    including ``restart`` and ``logs``.

    Returns ``None`` when the key is absent or empty.
    """
    return os.environ.get("WINTER_SERVICE_PREFIX") or None


def resolve_project_prefix(manifest_override: str | None) -> str | None:
    """Resolve the effective ``COMPOSE_PROJECT_NAME`` prefix.

    *manifest_override* is the manifest's optional ``project_prefix`` key.
    When a user hand-sets it in ``config.toml``, it takes precedence over the
    core-injected ``WINTER_SERVICE_PREFIX``. Otherwise falls back to
    ``read_service_prefix()``.

    Returns ``None`` when neither source supplies a value — this should only
    happen if ``WINTER_SERVICE_PREFIX`` itself is absent from the process
    environment (a winter-core bug, since it's a base extension var present
    on every dispatch action) and no manifest override is configured.
    """
    if manifest_override:
        return manifest_override
    return read_service_prefix()


def published_port(port_base: int, offset: int) -> int:
    """Compute a published host port from *port_base* and *offset*.

    Example: ``published_port(4060, 2) == 4062``.
    """
    return port_base + offset


@dataclass(frozen=True)
class EnvContext:
    """Resolved per-env context for docker compose invocations.

    Fields:
        env: The env name (e.g. ``"alpha"`` or ``"workspace"``).
        compose_project_name: The value for ``COMPOSE_PROJECT_NAME``.
        port_base: ``WINTER_PORT_BASE`` from the process environment (injected by
            winter-cli core), or ``None`` when absent.
    """

    env: str
    compose_project_name: str
    port_base: int | None


def build_env_context(
    env: str,
    project_prefix: str,
) -> EnvContext:
    """Build an ``EnvContext`` for *env* using *project_prefix*.

    *project_prefix* must already be resolved by the caller (typically via
    ``resolve_project_prefix``) — this function does not itself consult
    ``WINTER_SERVICE_PREFIX`` or any manifest override.

    Reads ``WINTER_PORT_BASE`` from the process environment (core-injected) to
    populate ``port_base``; absent or non-integer value yields ``port_base=None``.
    """
    project_name = compose_project_name(project_prefix, env)
    port_base = read_port_base()
    return EnvContext(
        env=env,
        compose_project_name=project_name,
        port_base=port_base,
    )
