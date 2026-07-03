"""Up/down lifecycle commands for winter-service-docker.

Implements ``up <env>`` and ``down <env>`` using the injectable ``ComposeClient``
seam.  All ``docker compose`` invocations go through the seam so tests never
need a real docker daemon.

Service-segment patterns (winter#139 / issue #6):
    ``<env>`` may be a bare scope name (``alpha``), the reserved ``workspace``
    scope, or a scope-qualified ``<scope>/<svc-pattern>`` glob (e.g.
    ``alpha/api``, ``alpha/*``). A bare scope or ``<scope>/*`` preserves the
    original whole-scope behavior (start/stop every declared service in that
    scope). A concrete or glob service segment (``alpha/api``, ``alpha/wor*``)
    is expanded via ``patterns.service_matches_any_pattern`` against the
    scope's declared services, and only the matched subset is targeted:
    ``up`` passes the matched names to ``compose up -d <svc>...`` (compose
    targets services natively on ``up``); ``down`` cannot target services at
    the project level, so a partial ``down`` instead issues
    ``compose stop <svc>...`` followed by ``compose rm -f <svc>...`` for the
    matched subset, leaving unmatched containers running. Whole-project
    ``compose down`` is reserved for the bare-scope / ``<scope>/*`` case.

Port-substitution convention (``up`` only):
    When the env has a ``WINTER_PORT_BASE``, the following env vars are injected
    into the compose invocation environment for each declared service:

        WSD_PORT_<UPPERCASE_SERVICE_NAME> = WINTER_PORT_BASE + <service_index>

    where ``<service_index>`` is the 0-based position in the manifest's
    ``[[service]]`` list.  The user's ``compose.yaml`` references published host
    ports via ``${WSD_PORT_DB}``, ``${WSD_PORT_API}``, etc.  Winter owns only
    the project-name and port-substitution variables; compose semantics are the
    user's responsibility.

    Example (``WINTER_PORT_BASE=4060``, services: ``[db, api]``):
        WSD_PORT_DB=4060
        WSD_PORT_API=4061

Readiness gate (``up`` only):
    After ``compose up -d`` exits 0, the gate polls ``compose ps --all --format json``
    at ``poll_interval`` second intervals until every service is either:
      - running + healthy (has healthcheck + passed)
      - running + no healthcheck (treated as immediately ready)

    A service is NOT ready while ``health == "unhealthy"`` or
    ``health == "starting"`` (both map to ``unknown`` in the winter health
    mapping, but here we inspect the raw docker health value).

    ``--all`` is passed so that exited/crashed containers are visible and can be
    detected as non-ready rather than being invisible (which would block the gate
    until timeout).

    If any service remains unready after ``timeout`` seconds, ``up`` returns a
    non-zero exit and emits an actionable stderr message naming the service.

    The ``time_fn`` and ``sleep_fn`` parameters are injectable so tests can
    control the clock without real sleeping.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable

from docker_orchestrator.compose_client import IComposeClient
from docker_orchestrator.compose_ps import extract_health, parse_compose_ps_output
from docker_orchestrator.env_context import EnvContext, build_env_context, resolve_project_prefix
from docker_orchestrator.manifest import DockerManifest, ServiceDecl
from docker_orchestrator.patterns import env_segment_of, service_matches_any_pattern

# ---------------------------------------------------------------------------
# Service-segment pattern helpers
# ---------------------------------------------------------------------------


def _is_whole_scope_pattern(token: str) -> bool:
    """Return True when *token* is a bare scope or ``<scope>/*`` (whole-scope).

    Anything else — a concrete ``<scope>/<svc>`` or a glob service segment
    (``<scope>/wor*``) — names a subset and is NOT whole-scope.
    """
    if "/" not in token:
        return True
    return token.split("/", 1)[1] == "*"


def _matched_service_names(
    token: str,
    scope: str,
    scoped_services: tuple[ServiceDecl, ...],
) -> list[str]:
    """Return the names of *scoped_services* whose name matches the service
    segment of *token* for *scope*.

    Delegates to ``patterns.service_matches_any_pattern`` so glob semantics
    match ``restart``/``logs``/``status``.
    """
    return [svc.name for svc in scoped_services if service_matches_any_pattern(scope, svc.name, [token])]


# ---------------------------------------------------------------------------
# Port-substitution helpers
# ---------------------------------------------------------------------------


def _port_env_vars(services: tuple[ServiceDecl, ...], port_base: int) -> dict[str, str]:
    """Build the ``WSD_PORT_<SVC>`` env vars for the compose invocation.

    One entry per service in *services*, keyed by ``WSD_PORT_<UPPERCASE_NAME>``,
    value is ``str(port_base + index)``.  Offsets are numbered over the supplied
    tuple so that scope partitioning does not shift per-env port assignments.
    Returns an empty dict when *services* is empty.
    """
    return {f"WSD_PORT_{svc.name.upper()}": str(port_base + i) for i, svc in enumerate(services)}


def _build_compose_env(
    ctx: EnvContext,
    scoped_services: tuple[ServiceDecl, ...],
) -> dict[str, str]:
    """Build the environment dict to pass to compose invocations.

    Starts from the current process environment (so PATH and other shell vars
    are inherited), then overlays ``COMPOSE_PROJECT_NAME`` and, when
    ``ctx.port_base`` is not None, the ``WSD_PORT_*`` port-substitution vars
    indexed over *scoped_services* (the scope-appropriate service subset).

    Workspace scope has ``port_base=None`` and emits no ``WSD_PORT_*`` vars.
    """
    import os

    env: dict[str, str] = dict(os.environ)
    env["COMPOSE_PROJECT_NAME"] = ctx.compose_project_name
    if ctx.port_base is not None:
        env.update(_port_env_vars(scoped_services, ctx.port_base))
    return env


# ---------------------------------------------------------------------------
# Readiness-gate helpers
# ---------------------------------------------------------------------------


def _is_service_ready(raw_docker_state: str, raw_docker_health: str | None) -> bool:
    """Return True when a container is considered ready for the readiness gate.

    A container is ready when:
    - It is running AND has no healthcheck (health is None/empty → treat as ready).
    - It is running AND health == "healthy".

    Not ready:
    - health == "unhealthy" or "starting" (still in progress).
    - Not in the "running" state (including exited/crashed containers, which are
      now visible via ``--all``).
    """
    if raw_docker_state != "running":
        return False
    # No healthcheck → immediately ready
    if not raw_docker_health:
        return True
    return raw_docker_health == "healthy"


def _poll_readiness(
    project: str,
    compose_file: str,
    client: IComposeClient,
    compose_env: dict[str, str],
    service_names: tuple[str, ...] | None = None,
) -> tuple[bool, str]:
    """Poll ``compose ps --all [<svc>...]`` once and return (all_ready, unready_service_name).

    Returns ``(True, "")`` when all containers are ready.

    Returns ``(False, "<name>")`` naming the first container that is not
    ready (but is still starting / unhealthy / exited).

    When compose ps returns no containers, the project may not have started
    yet → not ready.

    ``service_names``, when given, restricts the ``ps`` call to that subset
    (the matched services of a partial ``up``) so the readiness gate reports
    health for the matched subset only, not the whole scope.
    """
    ps_args = ["ps", "--all", "--format", "json"]
    if service_names:
        ps_args.extend(service_names)
    result = client.compose(
        project,
        compose_file,
        ps_args,
        capture_output=True,
        env=compose_env,
    )
    containers = parse_compose_ps_output(result.stdout or "")
    if not containers:
        return False, "<no containers>"

    for ct in containers:
        raw_state: str = ct.get("State", "") or ""
        # compose ps emits Health as a plain string ("", "starting",
        # "healthy", "unhealthy"); extract_health tolerates the nested form too.
        raw_health: str | None = extract_health(ct)
        if not _is_service_ready(raw_state, raw_health):
            name: str = ct.get("Name") or ct.get("Service") or "<unknown>"
            return False, name

    return True, ""


# ---------------------------------------------------------------------------
# down command
# ---------------------------------------------------------------------------


def cmd_down(
    env: str,
    manifest: DockerManifest,
    client: IComposeClient,
) -> int:
    """Implement ``down <env>``.

    *env* is either a bare scope (``alpha``, ``workspace``) or a scope-qualified
    ``<scope>/<svc-pattern>`` (e.g. ``alpha/api``, ``alpha/*``). A bare scope or
    a ``<scope>/*`` pattern runs whole-project teardown: ``docker compose
    -p <project> -f <compose_file> down``. A concrete or glob service segment
    stops (and removes) only the matched services via ``compose stop <svc>...``
    followed by ``compose rm -f <svc>...``, leaving unmatched containers
    running.

    Sets ``COMPOSE_PROJECT_NAME`` in the invocation environment.
    Returns the compose exit code.
    Emits a concise diagnostic line to stderr.
    """
    scope = env_segment_of(env)
    scoped_services: tuple[ServiceDecl, ...] = manifest.services_for_scope(scope)
    compose_file = manifest.compose_file_for_scope(scope)
    # Guard: a scope with no declared services and no compose file was never
    # configured at all (e.g. a workspace-only manifest asked to tear down a
    # feature env) — nothing to do.  When a compose file IS configured, "down"
    # still runs even if scoped_services is empty, since the file may reference
    # containers that a prior "up" started; compose down is a harmless no-op
    # against an unknown/empty project.
    if not scoped_services and not compose_file:
        scope_label = "workspace" if scope == "workspace" else scope
        print(
            f"docker-orchestrator: down: no {scope_label} services declared; nothing to tear down",
            file=sys.stderr,
        )
        return 0

    prefix = resolve_project_prefix(manifest.project_prefix)
    if not prefix or not compose_file:
        print(
            "docker-orchestrator: down: no project-name prefix available "
            "(set WINTER_SERVICE_PREFIX or config.toml's project_prefix override) "
            "or missing compose file for this scope",
            file=sys.stderr,
        )
        return 1

    ctx: EnvContext = build_env_context(scope, prefix)
    compose_env = _build_compose_env(ctx, scoped_services)

    whole_scope = _is_whole_scope_pattern(env)

    if whole_scope:
        print(
            f"docker-orchestrator: down: stopping {ctx.compose_project_name}",
            file=sys.stderr,
        )

        # "compose down" is project-level teardown and does not accept service-name
        # filters the way "up" does.  Because the compose project name is already
        # scope-specific (<prefix>-<env> vs <prefix>-workspace) and "up" only ever
        # starts scoped services inside that project, a whole-project "down" is both
        # correct and sufficient — it tears down exactly what "up" started.
        # If scoped_services is empty here the project was never started, so "down"
        # is a harmless no-op (compose exits 0 on an unknown/empty project).
        try:
            result = client.compose(
                ctx.compose_project_name,
                compose_file,
                ["down"],
                env=compose_env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"docker-orchestrator: down: compose error: {exc}", file=sys.stderr)
            return 1

        return result.returncode

    # Partial down: a service-segment filter names a subset of the scope's
    # services.  "compose down" cannot target services, so stop (and remove)
    # only the matched services, leaving unmatched containers running.
    matched = _matched_service_names(env, scope, scoped_services)
    if not matched:
        print(
            f"docker-orchestrator: down: no services matched pattern {env!r}; nothing to stop",
            file=sys.stderr,
        )
        return 0

    print(
        f"docker-orchestrator: down: stopping {matched!r} in {ctx.compose_project_name}",
        file=sys.stderr,
    )

    try:
        stop_result = client.compose(
            ctx.compose_project_name,
            compose_file,
            ["stop", *matched],
            env=compose_env,
        )
        if stop_result.returncode != 0:
            return stop_result.returncode

        rm_result = client.compose(
            ctx.compose_project_name,
            compose_file,
            ["rm", "-f", *matched],
            env=compose_env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"docker-orchestrator: down: compose error: {exc}", file=sys.stderr)
        return 1

    return rm_result.returncode


# ---------------------------------------------------------------------------
# up command
# ---------------------------------------------------------------------------


def cmd_up(
    env: str,
    manifest: DockerManifest,
    client: IComposeClient,
    *,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
    time_fn: Callable[[], float] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> int:
    """Implement ``up <env>``.

    *env* is either a bare scope (``alpha``, ``workspace``) or a scope-qualified
    ``<scope>/<svc-pattern>`` (e.g. ``alpha/api``, ``alpha/*``). A bare scope or
    a ``<scope>/*`` pattern starts every declared service in the scope (as
    before). A concrete or glob service segment starts only the matched subset.

    1. Runs ``docker compose -p <project> up -d [<svc>...]`` with
       ``COMPOSE_PROJECT_NAME`` and ``WSD_PORT_*`` vars in the environment —
       the matched service names are appended for a partial up, omitted for
       whole-scope.
    2. Polls ``compose ps --all [<svc>...]`` until every targeted service is
       healthy/ready, or *timeout* seconds elapse — restricted to the matched
       subset for a partial up.

    Args:
        env: The feature environment name (e.g. ``"alpha"``), optionally
            qualified with a service-segment pattern (``"alpha/api"``).
        manifest: The parsed extension manifest.
        client: Injectable ``ComposeClient`` (real or fake).
        timeout: Seconds to wait for all containers to become ready.
        poll_interval: Seconds between readiness polls.
        time_fn: Injectable ``time.time``-compatible callable; defaults to
            ``time.monotonic``.  Tests inject a fake clock.
        sleep_fn: Injectable ``time.sleep``-compatible callable; defaults to
            ``time.sleep``.  Tests inject a no-op to avoid real waits.

    Returns:
        0 on success; non-zero on compose failure or readiness timeout.
    """
    import time as _time

    _time_fn = time_fn if time_fn is not None else _time.monotonic
    _sleep_fn = sleep_fn if sleep_fn is not None else _time.sleep

    scope = env_segment_of(env)

    # Guard: if no services belong to this scope, skip the compose call entirely.
    # Each scope-pure compose file contains only its scope's services, so
    # an arg-less "up -d" starts exactly the right set.  But when the manifest
    # declares no services for this scope there is nothing to start.
    scoped_services: tuple[ServiceDecl, ...] = manifest.services_for_scope(scope)
    if not scoped_services:
        scope_label = "workspace" if scope == "workspace" else scope
        print(
            f"docker-orchestrator: up: no {scope_label} services declared; nothing to start",
            file=sys.stderr,
        )
        return 0

    compose_file = manifest.compose_file_for_scope(scope)
    prefix = resolve_project_prefix(manifest.project_prefix)
    if not prefix or not compose_file:
        print(
            "docker-orchestrator: up: no project-name prefix available "
            "(set WINTER_SERVICE_PREFIX or config.toml's project_prefix override) "
            "or missing compose file for this scope",
            file=sys.stderr,
        )
        return 1

    ctx: EnvContext = build_env_context(scope, prefix)
    compose_env = _build_compose_env(ctx, scoped_services)

    whole_scope = _is_whole_scope_pattern(env)
    matched: tuple[str, ...] = ()
    if not whole_scope:
        matched = tuple(_matched_service_names(env, scope, scoped_services))
        if not matched:
            print(
                f"docker-orchestrator: up: no services matched pattern {env!r}; nothing to start",
                file=sys.stderr,
            )
            return 0

    if matched:
        print(
            f"docker-orchestrator: up: starting {matched!r} in {ctx.compose_project_name}",
            file=sys.stderr,
        )
    else:
        print(
            f"docker-orchestrator: up: starting {ctx.compose_project_name}",
            file=sys.stderr,
        )

    # Step 1: compose up -d [<svc>...]
    # Each scope-pure compose file contains only its scope's services, so
    # whole-scope up needs no per-service-name masking — the file itself
    # enforces isolation. A partial up appends the matched service names so
    # compose targets exactly the requested subset.
    try:
        result = client.compose(
            ctx.compose_project_name,
            compose_file,
            ["up", "-d", *matched],
            env=compose_env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"docker-orchestrator: up: compose error: {exc}", file=sys.stderr)
        return 1

    if result.returncode != 0:
        print(
            f"docker-orchestrator: up: 'compose up -d' exited {result.returncode}",
            file=sys.stderr,
        )
        return result.returncode

    # Step 2: readiness gate
    print(
        f"docker-orchestrator: up: waiting for containers to become ready "
        f"(timeout={timeout}s, interval={poll_interval}s)",
        file=sys.stderr,
    )

    deadline = _time_fn() + timeout
    while True:
        # matched-only: any depends_on services compose started implicitly as a
        # side effect of "up -d <matched>" are intentionally outside the
        # readiness gate — the user asked to wait on the named service(s), not
        # their dependencies.
        ready, unready_name = _poll_readiness(
            ctx.compose_project_name,
            compose_file,
            client,
            compose_env,
            service_names=matched or None,
        )
        if ready:
            print(
                f"docker-orchestrator: up: all containers ready for {ctx.compose_project_name}",
                file=sys.stderr,
            )
            return 0

        remaining = deadline - _time_fn()
        if remaining <= 0:
            print(
                f"docker-orchestrator: up: timeout waiting for container '{unready_name}' "
                f"to become healthy in project '{ctx.compose_project_name}'. "
                f"Run 'docker compose -p {ctx.compose_project_name} ps' to inspect.",
                file=sys.stderr,
            )
            return 1

        _sleep_fn(min(poll_interval, remaining))
