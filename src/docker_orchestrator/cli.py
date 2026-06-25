"""Winter service-provider CLI entrypoint for winter-service-docker.

Winter invokes this as::

    <entrypoint> <action> [positional...]

with cwd at the workspace root and ``WINTER_*`` env vars set (see
``ai/provider-contract.md``).

Exit codes:
  0  — success
  2  — unknown action
  3  — recognized action not yet implemented (refuse)

All six known actions (``describe``, ``status``, ``up``, ``down``,
``restart``, ``logs``) are implemented.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from docker_orchestrator.manifest import load as load_manifest

_KNOWN_ACTIONS = frozenset({"up", "down", "status", "restart", "logs", "describe", "catalog"})
_UNKNOWN_EXIT = 2
_REFUSE_EXIT = 3


def _cmd_describe(workspace_root: Path | None) -> int:
    """Emit scope-qualified ``{"services": [...]}`` from the configured service list.

    Emits ``workspace/<name>`` for workspace-scoped services and ``*/<name>`` for
    project-scoped services, matching the ``catalog`` action's output shape.  This
    allows winter-cli core to split the workspace vs per-env axis when building the
    status call-matrix (Phase 2 of winter#109).
    """
    from docker_orchestrator.manifest import _CONFIG_FILE, resolve_config_dir

    config_dir = resolve_config_dir(workspace_root)
    if config_dir is not None and not (config_dir / _CONFIG_FILE).exists():
        print(
            f"docker-orchestrator: no config.toml at {config_dir}; run the scaffolder",
            file=sys.stderr,
        )
    manifest = load_manifest(config_dir)
    names: list[str] = []
    for svc in manifest.workspace_services:
        names.append(f"workspace/{svc.name}")
    for svc in manifest.services:
        names.append(f"*/{svc.name}")
    sys.stdout.write(json.dumps({"services": names}) + "\n")
    sys.stdout.flush()
    return 0


def _cmd_catalog(workspace_root: Path | None) -> int:
    """Emit scope-qualified service names as ``{"services": [...]}`` JSON.

    Returns ``workspace/<name>`` for workspace-scoped services and ``*/<name>``
    for project-scoped services (env-agnostic; any env may run them).  An absent
    or empty manifest returns an empty list rather than an error — the lint check
    distinguishes "no catalog" from "unknown reference".
    """
    from docker_orchestrator.manifest import resolve_config_dir

    config_dir = resolve_config_dir(workspace_root)
    try:
        manifest = load_manifest(config_dir)
    except ValueError:
        sys.stdout.write(json.dumps({"services": []}) + "\n")
        sys.stdout.flush()
        return 0

    names: list[str] = []
    for svc in manifest.workspace_services:
        names.append(f"workspace/{svc.name}")
    for svc in manifest.services:
        names.append(f"*/{svc.name}")
    sys.stdout.write(json.dumps({"services": names}) + "\n")
    sys.stdout.flush()
    return 0


def _cmd_status(argv_rest: list[str], workspace_root: Path | None) -> int:
    """Implement ``status [<pattern>...]``, emitting winter's env-keyed status document."""
    from docker_orchestrator.compose_client import ComposeClient
    from docker_orchestrator.manifest import _CONFIG_FILE, resolve_config_dir
    from docker_orchestrator.status import cmd_status

    config_dir = resolve_config_dir(workspace_root)
    if config_dir is not None and not (config_dir / _CONFIG_FILE).exists():
        print(
            f"docker-orchestrator: no config.toml at {config_dir}; run the scaffolder",
            file=sys.stderr,
        )
    manifest = load_manifest(config_dir)

    # workspace_root defaults to cwd when WINTER_WORKSPACE_DIR is unset
    ws_root = workspace_root if workspace_root is not None else Path.cwd()

    client = ComposeClient()
    return cmd_status(patterns=argv_rest, manifest=manifest, workspace_root=ws_root, client=client)


def _cmd_up(argv_rest: list[str], workspace_root: Path | None) -> int:
    """Implement ``up <env>``."""
    from docker_orchestrator.compose_client import ComposeClient
    from docker_orchestrator.lifecycle import cmd_up
    from docker_orchestrator.manifest import resolve_config_dir

    if not argv_rest:
        print("docker-orchestrator: up: missing required <env> argument", file=sys.stderr)
        return 2

    env = argv_rest[0]
    config_dir = resolve_config_dir(workspace_root)
    manifest = load_manifest(config_dir)
    ws_root = workspace_root if workspace_root is not None else Path.cwd()
    client = ComposeClient()
    # Allow the timeout and poll interval to be overridden for testing / fast CI.
    _timeout = float(os.environ.get("WSD_UP_TIMEOUT", "120"))
    _poll_interval = float(os.environ.get("WSD_UP_POLL_INTERVAL", "2"))
    return cmd_up(
        env=env,
        manifest=manifest,
        workspace_root=ws_root,
        client=client,
        timeout=_timeout,
        poll_interval=_poll_interval,
    )


def _cmd_down(argv_rest: list[str], workspace_root: Path | None) -> int:
    """Implement ``down <env>``."""
    from docker_orchestrator.compose_client import ComposeClient
    from docker_orchestrator.lifecycle import cmd_down
    from docker_orchestrator.manifest import resolve_config_dir

    if not argv_rest:
        print("docker-orchestrator: down: missing required <env> argument", file=sys.stderr)
        return 2

    env = argv_rest[0]
    config_dir = resolve_config_dir(workspace_root)
    manifest = load_manifest(config_dir)
    ws_root = workspace_root if workspace_root is not None else Path.cwd()
    client = ComposeClient()
    return cmd_down(env=env, manifest=manifest, workspace_root=ws_root, client=client)


def _cmd_restart(argv_rest: list[str], workspace_root: Path | None) -> int:
    """Implement ``restart <pattern>...``."""
    from docker_orchestrator.compose_client import ComposeClient
    from docker_orchestrator.manifest import resolve_config_dir
    from docker_orchestrator.restart import cmd_restart

    if not argv_rest:
        print("docker-orchestrator: restart: at least one <env>/<service> pattern is required", file=sys.stderr)
        return 1

    config_dir = resolve_config_dir(workspace_root)
    manifest = load_manifest(config_dir)
    ws_root = workspace_root if workspace_root is not None else Path.cwd()
    client = ComposeClient()
    return cmd_restart(patterns=argv_rest, manifest=manifest, workspace_root=ws_root, client=client)


def _cmd_logs(argv_rest: list[str], workspace_root: Path | None) -> int:
    """Implement ``logs [<pattern>...] [render flags]``."""
    from docker_orchestrator.compose_client import ComposeClient
    from docker_orchestrator.logs import cmd_logs, read_log_options
    from docker_orchestrator.manifest import resolve_config_dir

    patterns, follow, tail, since, until = read_log_options(argv_rest)

    config_dir = resolve_config_dir(workspace_root)
    manifest = load_manifest(config_dir)
    ws_root = workspace_root if workspace_root is not None else Path.cwd()
    client = ComposeClient()
    return cmd_logs(
        patterns=patterns,
        manifest=manifest,
        workspace_root=ws_root,
        client=client,
        follow=follow,
        tail=tail,
        since=since,
        until=until,
    )


def main(argv: list[str]) -> int:
    """Parse ``[action, *rest]`` and dispatch.

    *argv* should be ``sys.argv[1:]``.  ``sys.argv`` (the full list) is echoed
    to stderr so ``winter ext verify`` forwards-params check passes.
    """
    # Echo argv to stderr for the forwards-params conformance check.
    print(" ".join(sys.argv), file=sys.stderr)

    if not argv:
        print("usage: <action> [args...]\n  actions: " + ", ".join(sorted(_KNOWN_ACTIONS)), file=sys.stderr)
        return _UNKNOWN_EXIT

    action = argv[0]

    if action not in _KNOWN_ACTIONS:
        print(f"docker-orchestrator: unknown action '{action}'", file=sys.stderr)
        return _UNKNOWN_EXIT

    ws_dir = os.environ.get("WINTER_WORKSPACE_DIR")
    workspace_root = Path(ws_dir) if ws_dir else None

    if action == "catalog":
        return _cmd_catalog(workspace_root)

    if action == "describe":
        return _cmd_describe(workspace_root)

    if action == "status":
        return _cmd_status(argv[1:], workspace_root)

    if action == "up":
        return _cmd_up(argv[1:], workspace_root)

    if action == "down":
        return _cmd_down(argv[1:], workspace_root)

    if action == "restart":
        return _cmd_restart(argv[1:], workspace_root)

    if action == "logs":
        return _cmd_logs(argv[1:], workspace_root)

    # Should never reach here — all known actions are handled above.
    print(f"docker-orchestrator: action '{action}' is not yet implemented", file=sys.stderr)
    return _REFUSE_EXIT


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
