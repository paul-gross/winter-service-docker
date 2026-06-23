"""Injectable docker/compose subprocess seam.

All ``docker`` and ``docker compose`` invocations in this package go through
``ComposeClient``.  Tests substitute a ``FakeComposeClient`` (see
``tests/fakes.py``) so no real docker daemon is required.

The client accepts an injectable *runner* callable with the same signature as
``subprocess.run``:  ``runner(args, **kwargs) -> subprocess.CompletedProcess``.
The real ``ComposeClient`` uses ``subprocess.run``; fakes record calls and
return canned results.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from typing import Any

# Type alias matching subprocess.run's return type.
CompletedProcess = subprocess.CompletedProcess[str]
Runner = Callable[..., CompletedProcess]

# Type alias for the streaming Popen variant.
# Returns (line_iterator, wait_fn) — the iterator yields decoded text lines and
# wait_fn() blocks until the subprocess exits, returning its exit code.
StreamResult = tuple[Iterator[str], Callable[[], int]]
StreamRunner = Callable[..., StreamResult]


# The shell script used to source a winter env file before exec'ing the real
# command.  ``set -a`` (allexport) makes every assignment performed while
# sourcing automatically exported, so the exec'd child inherits them — including
# values produced by shell arithmetic such as ``WTS_DB_PORT=$((WINTER_PORT_BASE+12))``.
# The env-file path arrives as ``$1`` and the real argv as ``$2..`` (via ``"$@"``
# after ``shift``), so no shell-quoting of the docker argv is required and the
# invocation is injection-safe.
_SOURCE_WRAPPER = 'set -a; . "$1"; set +a; shift; exec "$@"'


def _wrap_for_source(cmd: list[str], source_env_file: str | None) -> list[str]:
    """Wrap *cmd* so *source_env_file* is sourced in a shell before exec.

    Returns *cmd* unchanged when *source_env_file* is ``None``.  Otherwise returns
    a ``bash -c`` invocation that sources the file (evaluating and exporting its
    ``KEY=VAL`` lines and ``$((...))`` arithmetic) and then exec's the original
    command, which inherits every exported variable.
    """
    if source_env_file is None:
        return cmd
    return ["bash", "-c", _SOURCE_WRAPPER, "bash", source_env_file, *cmd]


def _default_runner(args: list[str], **kwargs: Any) -> CompletedProcess:
    return subprocess.run(args, **kwargs)


def _default_stream_runner(args: list[str], **kwargs: Any) -> StreamResult:
    """Default streaming runner: opens a subprocess and yields stdout lines."""
    env = kwargs.get("env")
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    def _lines() -> Iterator[str]:
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line

    def _wait() -> int:
        proc.wait()
        return proc.returncode

    return _lines(), _wait


class ComposeClient:
    """Wraps all ``docker`` / ``docker compose`` subprocess calls.

    Args:
        runner: Callable with ``subprocess.run`` semantics.  Defaults to
            ``subprocess.run``.  Tests inject a ``FakeRunner``.
        stream_runner: Callable that opens a subprocess and returns
            ``(line_iterator, wait_fn)``.  Defaults to ``_default_stream_runner``.
            Tests inject a ``FakeStreamRunner`` to avoid real subprocess calls.
    """

    def __init__(
        self,
        runner: Runner | None = None,
        stream_runner: StreamRunner | None = None,
    ) -> None:
        self._run: Runner = runner if runner is not None else _default_runner
        self._stream: StreamRunner = stream_runner if stream_runner is not None else _default_stream_runner

    def compose(
        self,
        project: str,
        compose_file: str,
        args: list[str],
        *,
        capture_output: bool = False,
        check: bool = False,
        env: dict[str, str] | None = None,
        source_env_file: str | None = None,
    ) -> CompletedProcess:
        """Run ``docker compose -p <project> -f <compose_file> <args...>``.

        Args:
            project: The ``COMPOSE_PROJECT_NAME`` value (passed via ``-p``).
            compose_file: Path to the compose file (passed via ``-f``).
            args: Remaining arguments forwarded verbatim to docker compose.
            capture_output: When True, capture stdout/stderr into the result.
            check: When True, raise ``subprocess.CalledProcessError`` on non-zero exit.
            env: Override the subprocess environment; None inherits the current env.
            source_env_file: When set, the invocation is wrapped in a shell that
                ``source``s this file (allexport) before exec'ing docker compose,
                so the file's vars — including ``$((...))`` arithmetic — are
                exported into compose's environment for ``${VAR}`` interpolation.
        """
        cmd = ["docker", "compose", "-p", project, "-f", compose_file, *args]
        cmd = _wrap_for_source(cmd, source_env_file)
        return self._run(
            cmd,
            capture_output=capture_output,
            text=True,
            check=check,
            env=env,
        )

    def docker(
        self,
        args: list[str],
        *,
        capture_output: bool = False,
        check: bool = False,
        env: dict[str, str] | None = None,
    ) -> CompletedProcess:
        """Run ``docker <args...>``.

        Args:
            args: Arguments forwarded verbatim to docker.
            capture_output: When True, capture stdout/stderr into the result.
            check: When True, raise ``subprocess.CalledProcessError`` on non-zero exit.
            env: Override the subprocess environment; None inherits the current env.
        """
        cmd = ["docker", *args]
        return self._run(
            cmd,
            capture_output=capture_output,
            text=True,
            check=check,
            env=env,
        )

    def compose_stream(
        self,
        project: str,
        compose_file: str,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        source_env_file: str | None = None,
    ) -> StreamResult:
        """Run ``docker compose -p <project> -f <compose_file> <args...>`` as a stream.

        Returns ``(line_iterator, wait_fn)`` where ``line_iterator`` yields
        decoded text lines from stdout and ``wait_fn()`` waits for the process
        to exit and returns its exit code.

        Used for ``docker compose logs --follow`` so lines are emitted
        incrementally rather than buffered until completion.

        Args:
            project: The ``COMPOSE_PROJECT_NAME`` value.
            compose_file: Path to the compose file.
            args: Remaining arguments forwarded verbatim.
            env: Override the subprocess environment; None inherits the current env.
            source_env_file: When set, the invocation is wrapped in a shell that
                ``source``s this file before exec'ing docker compose (see
                ``compose``).
        """
        cmd = ["docker", "compose", "-p", project, "-f", compose_file, *args]
        cmd = _wrap_for_source(cmd, source_env_file)
        return self._stream(cmd, env=env)
