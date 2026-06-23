"""Tests for sourcing a winter env file before docker compose.

The orchestrator ``source``s the scope's winter env file (``.winter.env`` per
feature env, ``.winter.workspace.env`` for the workspace scope) in a shell
before exec'ing ``docker compose``, mirroring winter-service-tmux.  Sourcing
(vs. parsing) lets the env file carry shell arithmetic that is evaluated and
exported into compose's environment for ``${VAR}`` interpolation.

Covers:
1. ``resolve_env_file`` path resolution for feature + workspace scopes.
2. ``_wrap_for_source`` argv shape (and pass-through when no file).
3. ``compose`` / ``compose_stream`` wrap the argv when ``source_env_file`` is set.
4. End-to-end through a REAL shell: arithmetic in the env file reaches the
   exec'd child's environment, and a sourced key overrides the inherited
   subprocess environment (precedence contract).
5. Lifecycle integration: ``up``/``down`` forward the resolved env file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from docker_orchestrator.compose_client import (
    _SOURCE_WRAPPER,
    ComposeClient,
    _wrap_for_source,
)
from docker_orchestrator.env_context import resolve_env_file
from docker_orchestrator.lifecycle import cmd_down, cmd_up
from docker_orchestrator.manifest import DockerManifest, ServiceDecl
from tests.fakes import FakeComposeClient, FakeRunner

# ---------------------------------------------------------------------------
# resolve_env_file
# ---------------------------------------------------------------------------


class TestResolveEnvFile:
    def test_feature_env_returns_winter_env_when_present(self, tmp_path: Path) -> None:
        (tmp_path / "alpha").mkdir()
        (tmp_path / "alpha" / ".winter.env").write_text("WINTER_PORT_BASE=4100\n")
        assert resolve_env_file(tmp_path, "alpha") == str(tmp_path / "alpha" / ".winter.env")

    def test_feature_env_returns_none_when_absent(self, tmp_path: Path) -> None:
        assert resolve_env_file(tmp_path, "alpha") is None

    def test_workspace_returns_workspace_env_when_present(self, tmp_path: Path) -> None:
        (tmp_path / ".winter.workspace.env").write_text("WINTER_PORT_BASE=4000\n")
        assert resolve_env_file(tmp_path, "workspace") == str(tmp_path / ".winter.workspace.env")

    def test_workspace_returns_none_when_absent(self, tmp_path: Path) -> None:
        assert resolve_env_file(tmp_path, "workspace") is None

    def test_workspace_ignores_per_env_style_file(self, tmp_path: Path) -> None:
        # A workspace/.winter.env subdir file is unrelated to the workspace scope,
        # whose file lives at the root as .winter.workspace.env.
        (tmp_path / "workspace").mkdir()
        (tmp_path / "workspace" / ".winter.env").write_text("WINTER_PORT_BASE=9999\n")
        assert resolve_env_file(tmp_path, "workspace") is None


# ---------------------------------------------------------------------------
# _wrap_for_source
# ---------------------------------------------------------------------------


class TestWrapForSource:
    def test_none_passes_through_unchanged(self) -> None:
        cmd = ["docker", "compose", "-p", "p", "-f", "f", "up", "-d"]
        assert _wrap_for_source(cmd, None) == cmd

    def test_wraps_in_bash_source_invocation(self) -> None:
        cmd = ["docker", "compose", "up", "-d", "db"]
        wrapped = _wrap_for_source(cmd, "/ws/.winter.workspace.env")
        assert wrapped == [
            "bash",
            "-c",
            _SOURCE_WRAPPER,
            "bash",
            "/ws/.winter.workspace.env",
            *cmd,
        ]


# ---------------------------------------------------------------------------
# ComposeClient threads the wrap into the real argv
# ---------------------------------------------------------------------------


class TestComposeClientWrapping:
    def test_compose_wraps_argv_when_sourcing(self) -> None:
        runner = FakeRunner()
        client = ComposeClient(runner=runner)
        client.compose(
            "myapp-workspace", "compose.yaml", ["up", "-d", "db"], source_env_file="/ws/.winter.workspace.env"
        )
        argv = runner.calls[0].args
        assert argv[:5] == ["bash", "-c", _SOURCE_WRAPPER, "bash", "/ws/.winter.workspace.env"]
        assert argv[5:] == ["docker", "compose", "-p", "myapp-workspace", "-f", "compose.yaml", "up", "-d", "db"]

    def test_compose_plain_argv_without_sourcing(self) -> None:
        runner = FakeRunner()
        client = ComposeClient(runner=runner)
        client.compose("myapp-alpha", "compose.yaml", ["down"])
        assert runner.calls[0].args == [
            "docker",
            "compose",
            "-p",
            "myapp-alpha",
            "-f",
            "compose.yaml",
            "down",
        ]


# ---------------------------------------------------------------------------
# Real shell: arithmetic in the env file reaches the exec'd child
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
class TestRealShellSourcing:
    def test_arithmetic_in_env_file_is_evaluated_and_exported(self, tmp_path: Path) -> None:
        """The whole point: $((...)) in the env file is evaluated by the shell
        and exported, so the exec'd command sees the computed value."""
        env_file = tmp_path / ".winter.workspace.env"
        env_file.write_text("WINTER_PORT_BASE=4000\nWS_DB_PORT=$(( WINTER_PORT_BASE + 12 ))\n")
        wrapped = _wrap_for_source(["printenv", "WS_DB_PORT"], str(env_file))
        result = subprocess.run(wrapped, capture_output=True, text=True)
        assert result.returncode == 0
        assert result.stdout.strip() == "4012"

    def test_plain_assignment_is_exported(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".winter.workspace.env"
        env_file.write_text("WINTER_PORT_BASE=4000\n")
        wrapped = _wrap_for_source(["printenv", "WINTER_PORT_BASE"], str(env_file))
        result = subprocess.run(wrapped, capture_output=True, text=True)
        assert result.stdout.strip() == "4000"

    def test_sourced_key_overrides_inherited_environment(self, tmp_path: Path) -> None:
        """Precedence contract: the env file is sourced ON TOP of the subprocess
        environment (the channel that carries computed COMPOSE_PROJECT_NAME /
        WSD_PORT_* values), so a key assigned in both — the sourced file wins."""
        env_file = tmp_path / ".winter.workspace.env"
        env_file.write_text("WSD_PORT_DB=from_file\n")
        wrapped = _wrap_for_source(["printenv", "WSD_PORT_DB"], str(env_file))
        result = subprocess.run(wrapped, capture_output=True, text=True, env={**os.environ, "WSD_PORT_DB": "from_env"})
        assert result.stdout.strip() == "from_file"

    def test_inherited_key_passes_through_when_file_silent(self, tmp_path: Path) -> None:
        """A var present only in the inherited environment (not reassigned by the
        file) survives sourcing — the file augments, it doesn't replace."""
        env_file = tmp_path / ".winter.workspace.env"
        env_file.write_text("WINTER_PORT_BASE=4000\n")
        wrapped = _wrap_for_source(["printenv", "COMPOSE_PROJECT_NAME"], str(env_file))
        result = subprocess.run(
            wrapped, capture_output=True, text=True, env={**os.environ, "COMPOSE_PROJECT_NAME": "wws-workspace"}
        )
        assert result.stdout.strip() == "wws-workspace"


# ---------------------------------------------------------------------------
# Lifecycle integration — up/down forward the resolved env file
# ---------------------------------------------------------------------------


def _manifest(workspace_services: list[str]) -> DockerManifest:
    return DockerManifest(
        project_prefix="wws",
        compose_file="compose.yaml",
        services=(),
        workspace_services=tuple(ServiceDecl(name=s) for s in workspace_services),
    )


def _clock():
    calls = [0.0]

    def time_fn() -> float:
        t = calls[0]
        calls[0] += 200.0
        return t

    return time_fn, (lambda _n: None)


class TestLifecycleForwardsEnvFile:
    def test_up_workspace_passes_workspace_env_file(self, tmp_path: Path) -> None:
        (tmp_path / ".winter.workspace.env").write_text("WINTER_PORT_BASE=4000\n")
        running = subprocess.CompletedProcess(
            [], 0, stdout='{"Service": "db", "State": "running", "Name": "wws-workspace-db-1"}', stderr=""
        )
        client = FakeComposeClient(compose_results=[subprocess.CompletedProcess([], 0, stdout="", stderr=""), running])
        time_fn, sleep_fn = _clock()
        rc = cmd_up("workspace", _manifest(["db"]), tmp_path, client, time_fn=time_fn, sleep_fn=sleep_fn, timeout=10.0)
        assert rc == 0
        expected = str(tmp_path / ".winter.workspace.env")
        # up -d call and the readiness ps poll both carry the env file.
        assert all(c.source_env_file == expected for c in client.compose_calls)

    def test_down_workspace_passes_workspace_env_file(self, tmp_path: Path) -> None:
        (tmp_path / ".winter.workspace.env").write_text("WINTER_PORT_BASE=4000\n")
        client = FakeComposeClient(compose_results=[subprocess.CompletedProcess([], 0, stdout="", stderr="")])
        cmd_down("workspace", _manifest(["db"]), tmp_path, client)
        assert client.compose_calls[0].source_env_file == str(tmp_path / ".winter.workspace.env")

    def test_up_without_env_file_passes_none(self, tmp_path: Path) -> None:
        running = subprocess.CompletedProcess(
            [], 0, stdout='{"Service": "db", "State": "running", "Name": "wws-workspace-db-1"}', stderr=""
        )
        client = FakeComposeClient(compose_results=[subprocess.CompletedProcess([], 0, stdout="", stderr=""), running])
        time_fn, sleep_fn = _clock()
        cmd_up("workspace", _manifest(["db"]), tmp_path, client, time_fn=time_fn, sleep_fn=sleep_fn, timeout=10.0)
        assert all(c.source_env_file is None for c in client.compose_calls)
