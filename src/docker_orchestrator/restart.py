"""Restart command implementation for winter-service-docker.

Implements ``restart <pattern>...`` using the injectable ``ComposeClient``
seam.  Winter forwards one or more ``<env>/<service>`` glob patterns
verbatim in single-provider mode.

Pattern resolution:
    Each pattern is matched against the manifest's declared services via the
    same ``_service_matches_any_pattern`` helper used by ``status``.  An
    ``<env>/<svc>`` pattern names the env explicitly.  A bare ``<svc>`` token
    (no ``/``) resolves the env from ``_envs_from_patterns``; when that yields
    nothing (wildcard env segment or truly bare), the manifest's services are
    matched in the ``workspace`` scope only for backward-compat; an actionable
    stderr message is emitted so the caller can correct the pattern.

For each matched ``(env, service)`` pair, the implementation issues:

    docker compose -p <prefix>-<env> -f <compose_file> restart <svc>

All diagnostics go to stderr.  The worst (highest) exit code across all
matched restarts is returned.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from docker_orchestrator.compose_client import IComposeClient
from docker_orchestrator.env_context import build_env_context, resolve_env_file
from docker_orchestrator.manifest import DockerManifest
from docker_orchestrator.patterns import envs_from_patterns, service_matches_any_pattern

# Backward-compat aliases consumed by tests that import from this module
_envs_from_patterns = envs_from_patterns
_service_matches_any_pattern = service_matches_any_pattern


def _collect_restart_targets(
    patterns: list[str],
    manifest: DockerManifest,
) -> list[tuple[str, str]]:
    """Return a list of ``(env, service_name)`` pairs matching *patterns*.

    Rules:
    - A bare token (no ``/``) is treated as ``<env>`` by ``_envs_from_patterns``
      (matches nothing useful for restart since we need a svc name, so we treat
      it as ``<env>/*``).
    - Wildcard env segments are not resolved here; those pairs are skipped with
      an actionable error to stderr.
    - Each declared service in the manifest is checked against every pattern.

    Returns unique ``(env, svc)`` pairs in pattern/manifest order.
    """
    # Normalise patterns: expand bare tokens (no '/') to <token>/* so they
    # match services across all envs named <token>.  If the caller intended
    # a bare service name without an env, _envs_from_patterns returns the
    # token as the env segment — restart requires knowing the env, so a bare
    # svc-only pattern is ambiguous and we emit a diagnostic.
    envs_from = envs_from_patterns(patterns)
    if not envs_from:
        print(
            "docker-orchestrator: restart: no concrete env found in patterns "
            f"{patterns!r}. Use '<env>/<service>' patterns.",
            file=sys.stderr,
        )
        return []

    seen: set[tuple[str, str]] = set()
    targets: list[tuple[str, str]] = []

    for env in envs_from:
        svc_names = [s.name for s in manifest.services_for_scope(env)]
        for svc in svc_names:
            if service_matches_any_pattern(env, svc, patterns):
                key = (env, svc)
                if key not in seen:
                    seen.add(key)
                    targets.append(key)

    return targets


def cmd_restart(
    patterns: list[str],
    manifest: DockerManifest,
    workspace_root: Path,
    client: IComposeClient,
) -> int:
    """Implement ``restart <pattern>...``.

    Matches *patterns* against the manifest's declared services, then issues
    ``docker compose restart <svc>`` for each match.  Returns the worst exit
    code across all invocations; 1 when no patterns were supplied or no
    services matched.

    Args:
        patterns: One or more ``<env>/<service>`` glob patterns forwarded by winter.
        manifest: The parsed extension manifest.
        workspace_root: Absolute path to the workspace root.
        client: Injectable ``ComposeClient`` (real or fake).
    """
    if not patterns:
        print(
            "docker-orchestrator: restart: at least one <env>/<service> pattern is required",
            file=sys.stderr,
        )
        return 1

    if not manifest.project_prefix or not manifest.compose_file:
        print(
            "docker-orchestrator: restart: manifest is missing project_prefix or compose_file",
            file=sys.stderr,
        )
        return 1

    targets = _collect_restart_targets(patterns, manifest)

    if not targets:
        print(
            f"docker-orchestrator: restart: no services matched patterns {patterns!r}",
            file=sys.stderr,
        )
        return 1

    worst = 0
    for env, svc in targets:
        ctx = build_env_context(env, manifest.project_prefix, workspace_root)
        print(
            f"docker-orchestrator: restart: restarting {svc!r} in {ctx.compose_project_name}",
            file=sys.stderr,
        )
        try:
            result = client.compose(
                ctx.compose_project_name,
                manifest.compose_file,
                ["restart", svc],
                source_env_file=resolve_env_file(workspace_root, env),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(
                f"docker-orchestrator: restart: compose error for {svc!r} in {env!r}: {exc}",
                file=sys.stderr,
            )
            worst = max(worst, 1)
            continue

        if result.returncode != 0:
            print(
                f"docker-orchestrator: restart: 'compose restart {svc}' exited {result.returncode} in {env!r}",
                file=sys.stderr,
            )
        worst = max(worst, result.returncode)

    return worst
