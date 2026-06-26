"""Logs command implementation for winter-service-docker.

Implements ``logs [<pattern>...] [render flags]`` using the injectable
``ComposeClient`` seam.  Winter forwards zero or more ``<env>/<service>`` glob
patterns followed by the render flags (see ``ai/provider-contract.md``).

NDJSON line contract (one object per line on stdout):
    {"ts": "<RFC3339>", "env": "<env>", "svc": "<service>", "msg": "<line>"}

    ``ts`` is derived by splitting docker's ``--timestamps`` prefix off each
    raw log line.  Docker emits ``<RFC3339Nano>  <msg>`` lines when
    ``--timestamps`` is active (always passed so winter gets timestamps).
    If a line has no parseable leading timestamp, ``ts`` is set to null and
    the whole line goes to ``msg``.

    ``env`` and ``svc`` are set from the loop context (not parsed from the
    docker output).

argv render flag → docker flag mapping:
    -f / --follow            → ``--follow``
    -n / --tail <n|all>      → ``--tail <n|all>``
    --since <rfc3339>        → ``--since <rfc>``
    --until <rfc3339>        → ``--until <rfc>``
    -t / --timestamps        → accepted and discarded; ``--timestamps`` is always
                               passed regardless (required to populate the ts field)

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
import re
import subprocess
import sys
from typing import IO

from docker_orchestrator.compose_client import IComposeClient
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
    The remaining flags are added only when the corresponding argv option
    (parsed by ``read_log_options``) is present.
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
    for raw in lines or []:
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


def read_log_options(
    tokens: list[str],
) -> tuple[list[str], bool, str | None, str | None, str | None]:
    """Split the ``logs`` action's argv ``tokens`` into patterns + render options.

    Mirrors ``winter service logs``' flag surface, which winter appends after
    the positional ``<pattern...>`` tokens::

        <pattern...> [-f|--follow] [-n|--tail <N|all>] \\
          [--since <rfc3339>] [--until <rfc3339>] [-t|--timestamps]

    ``--since``/``--until`` carry winter's already-resolved RFC3339 values and
    are consumed as-is. ``-t/--timestamps`` is accepted and discarded — docker
    always receives ``--timestamps`` so the ``ts`` field can be populated. Any
    non-flag token is a positional pattern.

    This is a thin contract parser, not a general getopt: it relies on winter's
    emission guarantees — selection patterns never lead with ``-``, and a value
    flag (``-n``/``--tail``, ``--since``, ``--until``) is always followed by its
    value. Do not "harden" it past those guarantees without changing the
    producer contract too.

    Returns ``(patterns, follow, tail, since, until)``.
    """
    patterns: list[str] = []
    follow = False
    tail: str | None = None
    since: str | None = None
    until: str | None = None

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in ("-f", "--follow"):
            follow = True
        elif tok in ("-t", "--timestamps"):
            pass  # docker always passes --timestamps; flag accepted as a no-op
        elif tok in ("-n", "--tail"):
            i += 1
            tail = tokens[i] if i < n else None
        elif tok == "--since":
            i += 1
            since = tokens[i] if i < n else None
        elif tok == "--until":
            i += 1
            until = tokens[i] if i < n else None
        else:
            patterns.append(tok)
        i += 1

    return patterns, follow, tail, since, until


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
        svc_names = [s.name for s in manifest.services_for_scope(env)]
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
    client: IComposeClient,
    *,
    sink: IO[str] | None = None,
    follow: bool = False,
    tail: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> int:
    """Implement ``logs [<pattern>...]``.

    The render options arrive as keyword args (parsed from argv by
    ``read_log_options`` at the CLI entrypoint) and this streams NDJSON
    ``{ts,env,svc,msg}`` lines to *sink* (default stdout).

    Args:
        patterns: Zero or more ``<env>/<service>`` glob patterns.
        manifest: The parsed extension manifest.
        client: Injectable ``ComposeClient`` (real or fake).
        sink: Output stream for NDJSON lines; defaults to ``sys.stdout``.
        follow: Stream live output after the backlog.
        tail: Resolved count string (``N`` or ``all``); None omits ``--tail``.
        since: RFC3339 lower bound; None omits ``--since``.
        until: RFC3339 upper bound; None omits ``--until``.
    """
    out: IO[str] = sink if sink is not None else sys.stdout

    if not manifest.project_prefix:
        print(
            "docker-orchestrator: logs: manifest is missing project_prefix",
            file=sys.stderr,
        )
        return 1

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
        compose_file = manifest.compose_file_for_scope(env)
        if not compose_file:
            print(
                f"docker-orchestrator: logs: manifest is missing compose file for scope of {env!r}",
                file=sys.stderr,
            )
            worst = max(worst, 1)
            continue
        ctx = build_env_context(env, manifest.project_prefix)
        log_args = _build_log_args(
            svc,
            follow=follow,
            tail=tail,
            since=since,
            until=until,
        )

        if follow:
            # Streaming mode: use compose_stream so lines are emitted as they arrive.
            try:
                line_iter, wait_fn = client.compose_stream(
                    ctx.compose_project_name,
                    compose_file,
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
                    compose_file,
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
