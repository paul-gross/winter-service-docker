"""In-memory test doubles for docker_orchestrator seams.

``FakeRunner`` replaces ``subprocess.run`` for ``ComposeClient`` so tests never
need a real docker daemon.  It records every invocation and returns canned
``CompletedProcess`` results.

``FakeComposeClient`` is a higher-level fake that records ``compose()`` and
``docker()`` calls at the method level (without going through a runner).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from docker_orchestrator.compose_client import CompletedProcess, IComposeClient, Runner, StreamResult

# ---------------------------------------------------------------------------
# FakeRunner
# ---------------------------------------------------------------------------


@dataclass
class FakeCall:
    """One recorded subprocess invocation."""

    args: list[str]
    capture_output: bool = False
    text: bool = False
    check: bool = False
    env: dict[str, str] | None = None


class FakeRunner:
    """A ``subprocess.run``-compatible callable that records calls and returns canned results.

    ``results`` is a list of ``CompletedProcess`` objects returned in FIFO order.
    When exhausted, subsequent calls return ``default_result``.

    Usage::

        runner = FakeRunner(results=[subprocess.CompletedProcess(["docker", "compose", ...], 0, stdout="", stderr="")])
        client = ComposeClient(runner=runner)
        result = client.compose("myapp-alpha", "compose.yaml", ["ps"])
        assert runner.calls[0].args == ["docker", "compose", "-p", "myapp-alpha", "-f", "compose.yaml", "ps"]
    """

    def __init__(
        self,
        results: list[CompletedProcess] | None = None,
        default_result: CompletedProcess | None = None,
    ) -> None:
        self._results: list[CompletedProcess] = list(results or [])
        self._default: CompletedProcess = default_result or subprocess.CompletedProcess([], 0, stdout="", stderr="")
        self.calls: list[FakeCall] = []

    def __call__(self, args: list[str], **kwargs: object) -> CompletedProcess:
        self.calls.append(
            FakeCall(
                args=list(args),
                capture_output=bool(kwargs.get("capture_output", False)),
                text=bool(kwargs.get("text", False)),
                check=bool(kwargs.get("check", False)),
                env=kwargs.get("env"),  # type: ignore[arg-type]
            )
        )
        if self._results:
            return self._results.pop(0)
        return self._default


def _conforms_fake_runner(x: FakeRunner) -> Runner:
    """Typecheck-time sentinel: FakeRunner satisfies Runner."""
    return x


# ---------------------------------------------------------------------------
# FakeStreamRunner
# ---------------------------------------------------------------------------


@dataclass
class StreamCall:
    """One recorded streaming subprocess invocation."""

    args: list[str]
    env: dict[str, str] | None = None


class FakeStreamRunner:
    """A streaming runner that records calls and returns canned line streams.

    ``streams`` is a list of ``(lines, exit_code)`` tuples returned in FIFO order.
    When exhausted, returns ``(iter([]), lambda: 0)``.

    Usage::

        runner = FakeStreamRunner(streams=[
            (["2024-01-01T00:00:00.000000000Z hello", "2024-01-01T00:00:01.000000000Z world"], 0),
        ])
        client = ComposeClient(stream_runner=runner)
        lines_iter, wait = client.compose_stream("proj", "compose.yaml", ["logs"])
        assert list(lines_iter) == [...]
        assert wait() == 0
    """

    def __init__(
        self,
        streams: list[tuple[list[str], int]] | None = None,
    ) -> None:
        self._streams: list[tuple[list[str], int]] = list(streams or [])
        self.calls: list[StreamCall] = []

    def __call__(self, args: list[str], **kwargs: object) -> StreamResult:
        self.calls.append(StreamCall(args=list(args), env=kwargs.get("env")))  # type: ignore[arg-type]
        if self._streams:
            lines, code = self._streams.pop(0)
            # Add newlines if missing so callers can rstrip consistently.
            lines_with_nl = [ln if ln.endswith("\n") else ln + "\n" for ln in lines]
            return iter(lines_with_nl), lambda c=code: c
        return iter([]), lambda: 0


# ---------------------------------------------------------------------------
# FakeComposeClient
# ---------------------------------------------------------------------------


@dataclass
class ComposeCall:
    """One recorded ``ComposeClient.compose()`` invocation."""

    project: str
    compose_file: str
    args: list[str]
    capture_output: bool = False
    check: bool = False
    env: dict[str, str] | None = None


@dataclass
class ComposeStreamCall:
    """One recorded ``ComposeClient.compose_stream()`` invocation."""

    project: str
    compose_file: str
    args: list[str]
    env: dict[str, str] | None = None


@dataclass
class DockerCall:
    """One recorded ``ComposeClient.docker()`` invocation."""

    args: list[str]
    capture_output: bool = False
    check: bool = False
    env: dict[str, str] | None = None


class FakeComposeClient:
    """In-memory ComposeClient that records method-level calls.

    ``compose_results`` / ``docker_results``: FIFO lists of ``CompletedProcess``
    returned by ``compose()`` / ``docker()`` respectively.  When exhausted,
    returns ``compose_default`` / ``docker_default``.

    ``compose_stream_results``: FIFO list of ``(lines, exit_code)`` pairs
    returned by ``compose_stream()``.  When exhausted, returns an empty stream.

    ``compose_calls`` / ``docker_calls`` / ``compose_stream_calls``:
    recorded invocations for assertion.
    """

    def __init__(
        self,
        compose_results: list[CompletedProcess] | None = None,
        docker_results: list[CompletedProcess] | None = None,
        compose_default: CompletedProcess | None = None,
        docker_default: CompletedProcess | None = None,
        compose_stream_results: list[tuple[list[str], int]] | None = None,
    ) -> None:
        self._compose_results: list[CompletedProcess] = list(compose_results or [])
        self._docker_results: list[CompletedProcess] = list(docker_results or [])
        self._compose_default: CompletedProcess = compose_default or subprocess.CompletedProcess(
            [], 0, stdout="", stderr=""
        )
        self._docker_default: CompletedProcess = docker_default or subprocess.CompletedProcess(
            [], 0, stdout="", stderr=""
        )
        self._compose_stream_results: list[tuple[list[str], int]] = list(compose_stream_results or [])
        self.compose_calls: list[ComposeCall] = []
        self.docker_calls: list[DockerCall] = []
        self.compose_stream_calls: list[ComposeStreamCall] = []

    def compose(
        self,
        project: str,
        compose_file: str,
        args: list[str],
        *,
        capture_output: bool = False,
        check: bool = False,
        env: dict[str, str] | None = None,
    ) -> CompletedProcess:
        self.compose_calls.append(
            ComposeCall(
                project=project,
                compose_file=compose_file,
                args=args,
                capture_output=capture_output,
                check=check,
                env=env,
            )
        )
        if self._compose_results:
            return self._compose_results.pop(0)
        return self._compose_default

    def compose_stream(
        self,
        project: str,
        compose_file: str,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> StreamResult:
        self.compose_stream_calls.append(
            ComposeStreamCall(
                project=project,
                compose_file=compose_file,
                args=args,
                env=env,
            )
        )
        if self._compose_stream_results:
            lines, code = self._compose_stream_results.pop(0)
            lines_with_nl = [ln if ln.endswith("\n") else ln + "\n" for ln in lines]
            return iter(lines_with_nl), lambda c=code: c
        return iter([]), lambda: 0

    def docker(
        self,
        args: list[str],
        *,
        capture_output: bool = False,
        check: bool = False,
        env: dict[str, str] | None = None,
    ) -> CompletedProcess:
        self.docker_calls.append(DockerCall(args=args, capture_output=capture_output, check=check, env=env))
        if self._docker_results:
            return self._docker_results.pop(0)
        return self._docker_default


def _conforms_fake_compose_client(x: FakeComposeClient) -> IComposeClient:
    """Typecheck-time sentinel: FakeComposeClient satisfies IComposeClient."""
    return x
