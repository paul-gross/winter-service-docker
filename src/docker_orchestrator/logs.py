"""Logs command implementation for winter-service-docker.

Implements ``logs [<pattern>...]`` using the injectable ``ComposeClient``
seam.  Winter forwards zero or more ``<env>/<service>`` glob patterns and
sets the ``WINTER_LOG_*`` env vars (see ``ai/provider-contract.md``).

NDJSON line contract (one object per line on stdout):
    {"ts": "<RFC3339>", "env": "<env>", "svc": "<service>", "msg": "<line>"}

    ``ts`` is derived by splitting docker's ``--timestamps`` prefix off each
    raw log line.  Docker emits ``<RFC3339Nano>  <msg>`` lines when
    ``--timestamps`` is active (always passed so winter gets timestamps).
    If a line has no parseable leading timestamp, ``ts`` is set to null and
    the whole line goes to ``msg``.

    ``env`` and ``svc`` are set from the loop context (not parsed from the
    docker output).

WINTER_LOG_* env var → docker flag mapping:
    WINTER_LOG_FOLLOW=1       → ``--follow``
    WINTER_LOG_TAIL=<n>       → ``--tail <n>``
    WINTER_LOG_SINCE=<rfc>    → ``--since <rfc>``
    WINTER_LOG_UNTIL=<rfc>    → ``--until <rfc>``
    WINTER_LOG_TIMESTAMPS=*   → always pass ``--timestamps`` (required for ts field)

For follow mode a streaming ``compose_stream`` call is used so lines arrive
incrementally.  For non-follow mode, ``compose`` with ``capture_output=True``
is used.

Multi-service fan-out:
    When multiple services match, each is fetched sequentially (non-follow)
    or streamed in parallel-looking sequential loops (follow).  In follow mode
    with multiple services, each service's stream is read to completion before
    the next — a simple serial approach suitable for the injectable-seam tests.
    The real CLI caller (winter) re-aggregates streams from multiple provider
    calls; multi-service follow within a single provider is a best-effort
    sequential implementation.

Empty patterns = all declared services in the target env(s).

Return value: worst exit code across all service log calls.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from io import StringIO
from pathlib import Path
from typing import IO

from docker_orchestrator.compose_client import ComposeClient
from docker_orchestrator.env_context import build_env_context
from docker_orchestrator.manifest import DockerManifest
from docker_orchestrator.patterns import envs_from_patterns, service_matches_any_pattern

# Backward-compat aliases consumed by tests that import from this module
_envs_from_patterns = envs_from_patterns
_service_matches_any_pattern = service_matches_any_pattern

# Docker's --timestamps output format:
#   <RFC3339Nano> <msg>
# Example: "2024-01-15T10:23:45.123456789Z some log line"
# The timestamp is everything up to (but not including) the first space.
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+(.*)", re.DOTALL)


def _parse_docker_log_line(raw: str) -> tuple[str | None, str]:
    """Split a docker ``--timestamps`` prefixed log line into ``(ts, msg)``.

    Docker emits ``<RFC3339Nano> <msg>`` when ``--timestamps`` is active.
    The nanosecond precision is trimmed to microseconds in the ts field so
    the result is valid RFC3339.

    Returns ``(None, raw_stripped)`` when the line has no parseable leading
    timestamp (e.g. a blank line, or docker output without --timestamps).
    """
    stripped = raw.rstrip("\n")
    m = _TS_RE.match(stripped)
    if not m:
        return None, stripped

    ts_raw = m.group(1)
    msg = m.group(2)

    # Normalise nanoseconds → microseconds (RFC3339 allows fractional seconds,
    # but 9-digit nanosecond precision is non-standard in some parsers).
    # Trim the fraction to at most 6 digits.
    ts = re.sub(r"(\.\d{6})\d+Z$", r"\1Z", ts_raw)

    return ts, msg


def _build_log_args(
    svc: str,
    *,
    follow: bool,
    tail: str | None,
    since: str | None,
    until: str | None,
) -> list[str]:
    """Build the ``docker compose logs`` argument list for one service.

    ``--timestamps`` is always included so ``ts`` can be populated in the
    NDJSON output.  ``--no-log-prefix`` is always included so each line begins
    with the RFC3339 timestamp instead of docker compose's ``<svc>-<n>  | ``
    prefix — without it the timestamp is not at the start of the line and
    ``ts`` parsing fails (env/svc come from loop context, not the prefix).
    Other flags are added only when the corresponding WINTER_LOG_* var is set.
    """
    args = ["logs", "--no-log-prefix", "--timestamps"]
    if follow:
        args.append("--follow")
    if tail is not None:
        args.extend(["--tail", tail])
    if since:
        args.extend(["--since", since])
    if until:
        args.extend(["--until", until])
    args.append(svc)
    return args


def _emit_ndjson(
    lines: list[str] | None,
    line_iter: None,
    env: str,
    svc: str,
    sink: IO[str],
) -> None:
    """Emit NDJSON events from a list of raw docker log lines."""
    for raw in (lines or []):
        ts, msg = _parse_docker_log_line(raw)
        event: dict[str, object] = {"ts": ts, "env": env, "svc": svc, "msg": msg}
        sink.write(json.dumps(event, ensure_ascii=False) + "\n")
    sink.flush()


def _stream_ndjson(
    line_iter,
    env: str,
    svc: str,
    sink: IO[str],
) -> None:
    """Emit NDJSON events from a streaming iterator of raw docker log lines."""
    for raw in line_iter:
        ts, msg = _parse_docker_log_line(raw)
        event: dict[str, object] = {"ts": ts, "env": env, "svc": svc, "msg": msg}
        sink.write(json.dumps(event, ensure_ascii=False) + "\n")
        sink.flush()


def _read_log_options() -> tuple[bool, str | None, str | None, str | None]:
    """Read WINTER_LOG_* env vars.

    Returns ``(follow, tail, since, until)``.
    """
    follow = os.environ.get("WINTER_LOG_FOLLOW", "0") == "1"
    tail = os.environ.get("WINTER_LOG_TAIL") or None
    since = os.environ.get("WINTER_LOG_SINCE") or None
    until = os.environ.get("WINTER_LOG_UNTIL") or None
    return follow, tail, since, until


def _collect_log_targets(
    patterns: list[str],
    manifest: DockerManifest,
) -> list[tuple[str, str]]:
    """Return ``(env, svc)`` pairs for the given patterns.

    Empty patterns means "all services in all envs from the pattern set".
    When patterns is empty AND no env can be inferred, return an empty list
    (no output, not an error — winter may have already filtered).

    Bare ``<env>`` tokens (no '/') match all services in that env.
    """
    svc_names = [s.name for s in manifest.services]
    if not svc_names:
        return []

    # No patterns → treat as wildcard over whatever envs are discoverable.
    # Since we can't enumerate live envs here, return empty: the CLI caller
    # should always supply at least an env scope.
    if not patterns:
        return []

    envs_from = envs_from_patterns(patterns)
    if not envs_from:
        return []

    seen: set[tuple[str, str]] = set()
    targets: list[tuple[str, str]] = []

    for env in envs_from:
        for svc in svc_names:
            if service_matches_any_pattern(env, svc, patterns):
                key = (env, svc)
                if key not in seen:
                    seen.add(key)
                    targets.append(key)

    return targets


def cmd_logs(
    patterns: list[str],
    manifest: DockerManifest,
    workspace_root: Path,
    client: ComposeClient,
    *,
    sink: IO[str] | None = None,
    follow: bool | None = None,
    tail: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> int:
    """Implement ``logs [<pattern>...]``.

    Reads WINTER_LOG_* env vars (unless overridden via kwargs for testing)
    and streams NDJSON ``{ts,env,svc,msg}`` lines to *sink* (default stdout).

    Args:
        patterns: Zero or more ``<env>/<service>`` glob patterns.
        manifest: The parsed extension manifest.
        workspace_root: Absolute path to the workspace root.
        client: Injectable ``ComposeClient`` (real or fake).
        sink: Output stream for NDJSON lines; defaults to ``sys.stdout``.
        follow: Override WINTER_LOG_FOLLOW; None means read from env.
        tail: Override WINTER_LOG_TAIL; None means read from env.
        since: Override WINTER_LOG_SINCE; None means read from env.
        until: Override WINTER_LOG_UNTIL; None means read from env.
    """
    out: IO[str] = sink if sink is not None else sys.stdout

    if not manifest.project_prefix or not manifest.compose_file:
        print(
            "docker-orchestrator: logs: manifest is missing project_prefix or compose_file",
            file=sys.stderr,
        )
        return 1

    # Read log options (env vars override if kwargs are not supplied).
    _env_follow, _env_tail, _env_since, _env_until = _read_log_options()
    _follow = follow if follow is not None else _env_follow
    _tail = tail if tail is not None else _env_tail
    _since = since if since is not None else _env_since
    _until = until if until is not None else _env_until

    # Resolve targets from patterns.
    targets = _collect_log_targets(patterns, manifest)

    if not targets and patterns:
        print(
            f"docker-orchestrator: logs: no services matched patterns {patterns!r}",
            file=sys.stderr,
        )
        return 1

    if not targets:
        # No patterns and no targets: default to all services from the first env
        # if we can derive one, else return 0 (empty output is valid).
        # Winter typically always passes at least <env>/pattern.
        return 0

    worst = 0

    for env, svc in targets:
        ctx = build_env_context(env, manifest.project_prefix, workspace_root)
        log_args = _build_log_args(
            svc,
            follow=_follow,
            tail=_tail,
            since=_since,
            until=_until,
        )

        if _follow:
            # Streaming mode: use compose_stream so lines are emitted as they arrive.
            try:
                line_iter, wait_fn = client.compose_stream(
                    ctx.compose_project_name,
                    manifest.compose_file,
                    log_args,
                )
                try:
                    _stream_ndjson(line_iter, env, svc, out)
                except BrokenPipeError:
                    return 0
                code = wait_fn()
            except (OSError, subprocess.SubprocessError) as exc:
                print(
                    f"docker-orchestrator: logs: stream error for {svc!r} in {env!r}: {exc}",
                    file=sys.stderr,
                )
                code = 1
        else:
            # Non-follow mode: run to completion.
            try:
                result = client.compose(
                    ctx.compose_project_name,
                    manifest.compose_file,
                    log_args,
                    capture_output=True,
                )
                code = result.returncode
                raw_lines = (result.stdout or "").splitlines(keepends=True)
                _emit_ndjson(raw_lines, None, env, svc, out)
            except (OSError, subprocess.SubprocessError) as exc:
                print(
                    f"docker-orchestrator: logs: compose error for {svc!r} in {env!r}: {exc}",
                    file=sys.stderr,
                )
                code = 1

        worst = max(worst, code)

    return worst
