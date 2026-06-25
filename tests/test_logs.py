"""Phase 4 unit tests for the ``logs`` command implementation.

Covers:
1. argv render flags → docker-flag mapping: follow, tail, since, until, timestamps.
2. NDJSON transform: ts split from msg; env+svc present on every line.
3. Unparseable-ts → null ts + whole line in msg.
4. Multi-service fan-out: each service gets its own compose call.
5. Empty-pattern → no targets (graceful, exit 0).
6. Pattern → correct service filter.
7. Follow mode uses compose_stream; non-follow uses compose.
8. BrokenPipeError in streaming mode returns 0.
9. Missing manifest fields returns 1 with diagnostic.
10. No services matched returns 1 with diagnostic.
11. read_log_options: argv parsing of each flag, flag-absent defaults, --tail all.
12. CLI dispatch: logs exits non-2 and non-3.
"""

from __future__ import annotations

import json
import subprocess
from io import StringIO
from pathlib import Path
from typing import ClassVar

import pytest

from docker_orchestrator.cli import main as cli_main
from docker_orchestrator.logs import (
    _build_log_args,
    _parse_docker_log_line,
    cmd_logs,
    read_log_options,
)
from docker_orchestrator.manifest import DockerManifest, ServiceDecl
from tests.fakes import FakeComposeClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(
    prefix: str = "myapp",
    compose_file: str = "compose.yaml",
    services: list[str] | None = None,
) -> DockerManifest:
    svcs = tuple(ServiceDecl(name=s) for s in (services or ["db"]))
    return DockerManifest(project_prefix=prefix, compose_file=compose_file, services=svcs)


def _ok_result(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def _docker_ts_line(ts: str, msg: str) -> str:
    """Build a docker --timestamps formatted line."""
    return f"{ts} {msg}"


# ---------------------------------------------------------------------------
# 1. Log args build: WINTER_LOG_* → flags
# ---------------------------------------------------------------------------


def test_build_log_args_minimal() -> None:
    args = _build_log_args("db", follow=False, tail=None, since=None, until=None)
    assert args == ["logs", "--no-log-prefix", "--timestamps", "db"]


def test_build_log_args_follow() -> None:
    args = _build_log_args("db", follow=True, tail=None, since=None, until=None)
    assert "--follow" in args


def test_build_log_args_tail() -> None:
    args = _build_log_args("db", follow=False, tail="100", since=None, until=None)
    assert "--tail" in args
    idx = args.index("--tail")
    assert args[idx + 1] == "100"


def test_build_log_args_since() -> None:
    ts = "2024-01-01T00:00:00Z"
    args = _build_log_args("db", follow=False, tail=None, since=ts, until=None)
    assert "--since" in args
    idx = args.index("--since")
    assert args[idx + 1] == ts


def test_build_log_args_until() -> None:
    ts = "2024-12-31T23:59:59Z"
    args = _build_log_args("db", follow=False, tail=None, since=None, until=ts)
    assert "--until" in args
    idx = args.index("--until")
    assert args[idx + 1] == ts


def test_build_log_args_all_flags() -> None:
    args = _build_log_args(
        "api",
        follow=True,
        tail="50",
        since="2024-01-01T00:00:00Z",
        until="2024-12-31T23:59:59Z",
    )
    assert "--timestamps" in args
    assert "--follow" in args
    assert "--tail" in args
    assert "--since" in args
    assert "--until" in args
    assert "api" in args


def test_build_log_args_service_is_last() -> None:
    """Service name is always the last positional arg (after flags)."""
    args = _build_log_args("db", follow=False, tail="10", since=None, until=None)
    assert args[-1] == "db"


# ---------------------------------------------------------------------------
# 2. NDJSON transform: ts split from msg
# ---------------------------------------------------------------------------


def test_parse_docker_log_line_standard() -> None:
    ts, msg = _parse_docker_log_line("2024-01-15T10:23:45.123456789Z hello world\n")
    assert ts is not None
    assert "2024-01-15T10:23:45" in ts
    assert ts.endswith("Z")
    assert msg == "hello world"


def test_parse_docker_log_line_no_ns() -> None:
    """Timestamps without nanoseconds still parse."""
    ts, msg = _parse_docker_log_line("2024-01-15T10:23:45Z some log\n")
    assert ts is not None
    assert msg == "some log"


def test_parse_docker_log_line_unparseable_ts_null() -> None:
    """A line with no leading timestamp gets ts=None and whole line as msg."""
    ts, msg = _parse_docker_log_line("this has no timestamp\n")
    assert ts is None
    assert "this has no timestamp" in msg


def test_parse_docker_log_line_blank() -> None:
    ts, msg = _parse_docker_log_line("\n")
    assert ts is None
    assert msg == ""


def test_parse_docker_log_line_trims_nanoseconds() -> None:
    """9-digit nanosecond precision is trimmed to 6 (microseconds) in ts."""
    ts, _ = _parse_docker_log_line("2024-01-15T10:23:45.123456789Z msg\n")
    assert ts is not None
    # Should not have more than 6 fractional digits
    import re

    m = re.search(r"\.(\d+)Z", ts)
    if m:
        assert len(m.group(1)) <= 6


# ---------------------------------------------------------------------------
# 3. NDJSON output: env+svc present on every line
# ---------------------------------------------------------------------------


def test_cmd_logs_ndjson_env_svc_present(tmp_path: Path) -> None:
    """Every NDJSON line must carry env and svc fields."""
    manifest = _make_manifest(services=["db"])
    lines = [
        "2024-01-15T10:23:45.123456Z msg1\n",
        "2024-01-15T10:23:46.000000Z msg2\n",
    ]
    client = FakeComposeClient(
        compose_results=[_ok_result(stdout="".join(lines))],
    )
    sink = StringIO()

    rc = cmd_logs(["alpha/db"], manifest, tmp_path, client, sink=sink, follow=False)

    assert rc == 0
    output = sink.getvalue().strip().split("\n")
    assert len(output) == 2
    for line in output:
        obj = json.loads(line)
        assert obj["env"] == "alpha"
        assert obj["svc"] == "db"
        assert "msg" in obj


def test_cmd_logs_ndjson_ts_field(tmp_path: Path) -> None:
    """ts field is populated from the docker timestamp prefix."""
    manifest = _make_manifest(services=["db"])
    lines = ["2024-01-15T10:23:45.123456Z hello\n"]
    client = FakeComposeClient(compose_results=[_ok_result(stdout="".join(lines))])
    sink = StringIO()

    cmd_logs(["alpha/db"], manifest, tmp_path, client, sink=sink, follow=False)

    obj = json.loads(sink.getvalue().strip())
    assert obj["ts"] is not None
    assert "2024-01-15" in obj["ts"]
    assert obj["msg"] == "hello"


def test_cmd_logs_ndjson_unparseable_ts_is_null(tmp_path: Path) -> None:
    """A line without a parseable ts gets ts=null, whole line in msg."""
    manifest = _make_manifest(services=["db"])
    lines = ["no timestamp here\n"]
    client = FakeComposeClient(compose_results=[_ok_result(stdout="".join(lines))])
    sink = StringIO()

    cmd_logs(["alpha/db"], manifest, tmp_path, client, sink=sink, follow=False)

    obj = json.loads(sink.getvalue().strip())
    assert obj["ts"] is None
    assert "no timestamp here" in obj["msg"]
    assert obj["env"] == "alpha"
    assert obj["svc"] == "db"


# ---------------------------------------------------------------------------
# 4. Multi-service fan-out
# ---------------------------------------------------------------------------


def test_cmd_logs_multi_service_fan_out(tmp_path: Path) -> None:
    """Each service in the pattern gets its own compose call."""
    manifest = _make_manifest(services=["db", "api"])
    db_lines = ["2024-01-15T10:23:45.000000Z db-log\n"]
    api_lines = ["2024-01-15T10:23:46.000000Z api-log\n"]
    client = FakeComposeClient(
        compose_results=[
            _ok_result(stdout="".join(db_lines)),
            _ok_result(stdout="".join(api_lines)),
        ]
    )
    sink = StringIO()

    rc = cmd_logs(["alpha/*"], manifest, tmp_path, client, sink=sink, follow=False)

    assert rc == 0
    assert len(client.compose_calls) == 2
    svcs_called = [call.args[-1] for call in client.compose_calls]
    assert "db" in svcs_called
    assert "api" in svcs_called

    lines = [ln for ln in sink.getvalue().strip().split("\n") if ln]
    assert len(lines) == 2
    svcs_in_output = {json.loads(ln)["svc"] for ln in lines}
    assert svcs_in_output == {"db", "api"}


# ---------------------------------------------------------------------------
# 5. Empty-pattern → no targets
# ---------------------------------------------------------------------------


def test_cmd_logs_empty_patterns_returns_0(tmp_path: Path) -> None:
    """Empty patterns with a manifest: no targets resolved, exit 0."""
    manifest = _make_manifest(services=["db"])
    client = FakeComposeClient()
    sink = StringIO()

    rc = cmd_logs([], manifest, tmp_path, client, sink=sink, follow=False)

    assert rc == 0
    assert len(client.compose_calls) == 0


# ---------------------------------------------------------------------------
# 6. Pattern → correct service filter
# ---------------------------------------------------------------------------


def test_cmd_logs_pattern_filters_service(tmp_path: Path) -> None:
    """alpha/db pattern calls only db, not api."""
    manifest = _make_manifest(services=["db", "api"])
    client = FakeComposeClient(compose_default=_ok_result(""))
    sink = StringIO()

    rc = cmd_logs(["alpha/db"], manifest, tmp_path, client, sink=sink, follow=False)

    assert rc == 0
    assert len(client.compose_calls) == 1
    assert client.compose_calls[0].args[-1] == "db"


# ---------------------------------------------------------------------------
# 7. Follow mode uses compose_stream; non-follow uses compose
# ---------------------------------------------------------------------------


def test_cmd_logs_non_follow_uses_compose(tmp_path: Path) -> None:
    manifest = _make_manifest(services=["db"])
    client = FakeComposeClient(compose_default=_ok_result(""))
    sink = StringIO()

    cmd_logs(["alpha/db"], manifest, tmp_path, client, sink=sink, follow=False)

    assert len(client.compose_calls) == 1
    assert len(client.compose_stream_calls) == 0


def test_cmd_logs_follow_uses_compose_stream(tmp_path: Path) -> None:
    manifest = _make_manifest(services=["db"])
    stream_lines = ["2024-01-15T10:23:45.000000Z live-line\n"]
    client = FakeComposeClient(compose_stream_results=[(stream_lines, 0)])
    sink = StringIO()

    rc = cmd_logs(["alpha/db"], manifest, tmp_path, client, sink=sink, follow=True)

    assert rc == 0
    assert len(client.compose_calls) == 0
    assert len(client.compose_stream_calls) == 1
    obj = json.loads(sink.getvalue().strip())
    assert obj["msg"] == "live-line"
    assert obj["env"] == "alpha"
    assert obj["svc"] == "db"


def test_cmd_logs_follow_streams_lines_incrementally(tmp_path: Path) -> None:
    """Follow mode emits each line as it arrives (one event per line)."""
    manifest = _make_manifest(services=["db"])
    stream_lines = [
        "2024-01-15T10:23:45.000000Z first\n",
        "2024-01-15T10:23:46.000000Z second\n",
        "2024-01-15T10:23:47.000000Z third\n",
    ]
    client = FakeComposeClient(compose_stream_results=[(stream_lines, 0)])
    sink = StringIO()

    rc = cmd_logs(["alpha/db"], manifest, tmp_path, client, sink=sink, follow=True)

    assert rc == 0
    output_lines = [ln for ln in sink.getvalue().strip().split("\n") if ln]
    assert len(output_lines) == 3
    msgs = [json.loads(ln)["msg"] for ln in output_lines]
    assert msgs == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# 8. BrokenPipeError in streaming mode returns 0
# ---------------------------------------------------------------------------


def test_cmd_logs_follow_broken_pipe_returns_0(tmp_path: Path) -> None:
    """BrokenPipeError during streaming is handled gracefully → exit 0."""
    manifest = _make_manifest(services=["db"])

    # Build a client whose compose_stream returns a generator that raises BrokenPipeError
    def _bp_iter():
        yield "2024-01-15T10:23:45.000000Z msg\n"
        raise BrokenPipeError

    class _BPClient:
        compose_calls: ClassVar[list] = []
        docker_calls: ClassVar[list] = []
        compose_stream_calls: ClassVar[list] = []

        def compose(self, *a, **kw):
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        def docker(self, *a, **kw):
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        def compose_stream(self, project, compose_file, args, *, env=None, source_env_file=None):
            self.compose_stream_calls.append((project, args))
            return _bp_iter(), lambda: 0

    client = _BPClient()
    sink = StringIO()

    rc = cmd_logs(["alpha/db"], manifest, tmp_path, client, sink=sink, follow=True)

    assert rc == 0


# ---------------------------------------------------------------------------
# 9. Missing manifest fields
# ---------------------------------------------------------------------------


def test_cmd_logs_missing_prefix_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = DockerManifest(project_prefix=None, compose_file="compose.yaml", services=(ServiceDecl("db"),))
    client = FakeComposeClient()
    sink = StringIO()

    rc = cmd_logs(["alpha/db"], manifest, tmp_path, client, sink=sink)

    assert rc == 1
    assert "manifest is missing" in capsys.readouterr().err


def test_cmd_logs_missing_compose_file_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = DockerManifest(project_prefix="myapp", compose_file=None, services=(ServiceDecl("db"),))
    client = FakeComposeClient()
    sink = StringIO()

    rc = cmd_logs(["alpha/db"], manifest, tmp_path, client, sink=sink)

    assert rc == 1
    assert "manifest is missing" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 10. No services matched returns 1 with diagnostic
# ---------------------------------------------------------------------------


def test_cmd_logs_no_match_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = _make_manifest(services=["db"])
    client = FakeComposeClient()
    sink = StringIO()

    rc = cmd_logs(["alpha/notexist"], manifest, tmp_path, client, sink=sink)

    assert rc == 1
    assert "no services matched" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 11. read_log_options: argv render-flag parsing
# ---------------------------------------------------------------------------


def test_read_log_options_patterns_only_defaults() -> None:
    """No flags → patterns preserved; follow False, tail/since/until None."""
    patterns, follow, tail, since, until = read_log_options(["alpha/db", "beta/api"])
    assert patterns == ["alpha/db", "beta/api"]
    assert follow is False
    assert tail is None
    assert since is None
    assert until is None


def test_read_log_options_follow_long_and_short() -> None:
    _, follow, _, _, _ = read_log_options(["alpha/db", "--follow"])
    assert follow is True
    _, follow, _, _, _ = read_log_options(["alpha/db", "-f"])
    assert follow is True


def test_read_log_options_tail_long_and_short() -> None:
    _, _, tail, _, _ = read_log_options(["alpha/db", "--tail", "50"])
    assert tail == "50"
    _, _, tail, _, _ = read_log_options(["alpha/db", "-n", "100"])
    assert tail == "100"


def test_read_log_options_tail_all() -> None:
    """``--tail all`` is passed through verbatim (docker accepts ``all``)."""
    _, _, tail, _, _ = read_log_options(["alpha/db", "--tail", "all"])
    assert tail == "all"


def test_read_log_options_since_until_consumed_as_is() -> None:
    patterns, _, _, since, until = read_log_options(
        ["alpha/db", "--since", "2024-01-01T00:00:00Z", "--until", "2024-12-31T23:59:59Z"]
    )
    assert patterns == ["alpha/db"]
    assert since == "2024-01-01T00:00:00Z"
    assert until == "2024-12-31T23:59:59Z"


def test_read_log_options_timestamps_accepted_no_op() -> None:
    """``-t``/``--timestamps`` is accepted and does not become a pattern."""
    patterns, _, _, _, _ = read_log_options(["alpha/db", "-t"])
    assert patterns == ["alpha/db"]
    patterns, _, _, _, _ = read_log_options(["alpha/db", "--timestamps"])
    assert patterns == ["alpha/db"]


def test_read_log_options_flags_only_yields_no_patterns() -> None:
    """A flags-only argv (winter's no-pattern logs dispatch) parses to zero patterns."""
    patterns, follow, tail, _, _ = read_log_options(["--tail", "200"])
    assert patterns == []
    assert follow is False
    assert tail == "200"


def test_read_log_options_all_flags_together() -> None:
    patterns, follow, tail, since, until = read_log_options(
        [
            "alpha/db",
            "-f",
            "-n",
            "25",
            "--since",
            "2024-01-01T00:00:00Z",
            "--until",
            "2024-12-31T23:59:59Z",
            "-t",
        ]
    )
    assert patterns == ["alpha/db"]
    assert follow is True
    assert tail == "25"
    assert since == "2024-01-01T00:00:00Z"
    assert until == "2024-12-31T23:59:59Z"


def test_cmd_logs_kwargs_tail_maps_to_compose_arg(tmp_path: Path) -> None:
    """tail kwarg → --tail in compose args (end-to-end through cmd_logs)."""
    manifest = _make_manifest(services=["db"])
    client = FakeComposeClient(compose_default=_ok_result(""))
    sink = StringIO()

    cmd_logs(["alpha/db"], manifest, tmp_path, client, sink=sink, follow=False, tail="50")

    args = client.compose_calls[0].args
    assert "--tail" in args
    assert args[args.index("--tail") + 1] == "50"


# ---------------------------------------------------------------------------
# 12. CLI dispatch: logs exits non-2 and non-3
# ---------------------------------------------------------------------------


def test_cli_logs_exits_non_2_non_3(capsys: pytest.CaptureFixture[str]) -> None:
    """logs action is implemented: exits non-2 (not unknown) and non-3 (not refuse)."""
    rc = cli_main(["logs"])
    assert rc != 2
    assert rc != 3
