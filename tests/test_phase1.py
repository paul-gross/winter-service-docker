"""Phase 1 unit tests for winter-service-docker.

Covers:
1. COMPOSE_PROJECT_NAME derivation for an env and for the ``workspace`` scope.
2. Port-base parsing from a fixture ``.winter.env``.
3. Published-port computation.
4. Manifest loading — config present (with services) and missing config.
5. ``describe`` output shape via the fake / via the CLI.
6. CLI exit codes for known/unknown/refused actions.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from docker_orchestrator.cli import main as cli_main
from docker_orchestrator.compose_client import ComposeClient
from docker_orchestrator.env_context import (
    WORKSPACE_SCOPE,
    build_env_context,
    compose_project_name,
    published_port,
    read_port_base,
)
from docker_orchestrator.manifest import DockerManifest, ServiceDecl, load as load_manifest
from tests.fakes import FakeComposeClient, FakeRunner


# ---------------------------------------------------------------------------
# 1. COMPOSE_PROJECT_NAME derivation
# ---------------------------------------------------------------------------


def test_compose_project_name_env() -> None:
    assert compose_project_name("myapp", "alpha") == "myapp-alpha"


def test_compose_project_name_beta() -> None:
    assert compose_project_name("myapp", "beta") == "myapp-beta"


def test_compose_project_name_workspace() -> None:
    assert compose_project_name("myapp", WORKSPACE_SCOPE) == "myapp-workspace"


def test_compose_project_name_custom_prefix() -> None:
    assert compose_project_name("proj", "gamma") == "proj-gamma"


# ---------------------------------------------------------------------------
# 2. Port-base parsing from fixture .winter.env
# ---------------------------------------------------------------------------


def test_read_port_base_alpha(tmp_workspace: Path) -> None:
    base = read_port_base(tmp_workspace, "alpha")
    assert base == 4020


def test_read_port_base_missing_env(tmp_workspace: Path) -> None:
    """An env without a .winter.env returns None."""
    base = read_port_base(tmp_workspace, "delta")
    assert base is None


def test_read_port_base_workspace_scope(tmp_workspace: Path) -> None:
    """Workspace scope always returns None (no per-env file)."""
    base = read_port_base(tmp_workspace, WORKSPACE_SCOPE)
    assert base is None


def test_read_port_base_key_absent(tmp_path: Path) -> None:
    """A .winter.env that exists but lacks WINTER_PORT_BASE returns None."""
    env_dir = tmp_path / "zeta"
    env_dir.mkdir()
    (env_dir / ".winter.env").write_text("WINTER_ENV=zeta\n", encoding="utf-8")
    base = read_port_base(tmp_path, "zeta")
    assert base is None


def test_read_port_base_env_with_export(tmp_path: Path) -> None:
    """parse_env_file strips leading 'export ' correctly."""
    env_dir = tmp_path / "eta"
    env_dir.mkdir()
    (env_dir / ".winter.env").write_text("export WINTER_PORT_BASE=4100\n", encoding="utf-8")
    base = read_port_base(tmp_path, "eta")
    assert base == 4100


# ---------------------------------------------------------------------------
# 3. Published-port computation
# ---------------------------------------------------------------------------


def test_published_port_zero_offset() -> None:
    assert published_port(4020, 0) == 4020


def test_published_port_nonzero_offset() -> None:
    assert published_port(4020, 5) == 4025


def test_published_port_large_base() -> None:
    assert published_port(4100, 3) == 4103


# ---------------------------------------------------------------------------
# 4. Manifest loading
# ---------------------------------------------------------------------------


def test_manifest_load_missing_dir() -> None:
    """Absent config dir returns an empty manifest gracefully."""
    manifest = load_manifest(Path("/tmp/nonexistent-winter-service-docker-config-xyz"))
    assert isinstance(manifest, DockerManifest)
    assert manifest.project_prefix is None
    assert manifest.compose_file is None
    assert manifest.services == ()


def test_manifest_load_missing_config_toml(config_dir: Path) -> None:
    """Config dir exists but has no config.toml → empty manifest."""
    manifest = load_manifest(config_dir)
    assert manifest.project_prefix is None
    assert manifest.compose_file is None
    assert manifest.services == ()


def test_manifest_load_minimal(config_dir: Path) -> None:
    """Minimal config.toml with required fields only."""
    (config_dir / "config.toml").write_text(
        'project_prefix = "myapp"\ncompose_file = "compose.yaml"\n',
        encoding="utf-8",
    )
    manifest = load_manifest(config_dir)
    assert manifest.project_prefix == "myapp"
    # Relative compose_file is resolved against the config dir, not cwd.
    assert manifest.compose_file == str(config_dir / "compose.yaml")
    assert manifest.services == ()


def test_manifest_load_with_services(config_dir: Path) -> None:
    """config.toml with [[service]] entries."""
    (config_dir / "config.toml").write_text(
        'project_prefix = "proj"\n'
        'compose_file = "docker/compose.yaml"\n'
        "\n"
        "[[service]]\n"
        'name = "backend"\n'
        "\n"
        "[[service]]\n"
        'name = "frontend"\n',
        encoding="utf-8",
    )
    manifest = load_manifest(config_dir)
    assert manifest.project_prefix == "proj"
    # Relative nested path resolves against the config dir.
    assert manifest.compose_file == str(config_dir / "docker/compose.yaml")
    assert len(manifest.services) == 2
    assert manifest.services[0] == ServiceDecl(name="backend")
    assert manifest.services[1] == ServiceDecl(name="frontend")


def test_manifest_load_absolute_compose_file_passes_through(config_dir: Path) -> None:
    """An absolute compose_file is used as-is, never re-anchored to the config dir."""
    (config_dir / "config.toml").write_text(
        'project_prefix = "myapp"\ncompose_file = "/etc/winter/compose.yaml"\n',
        encoding="utf-8",
    )
    manifest = load_manifest(config_dir)
    assert manifest.compose_file == "/etc/winter/compose.yaml"


def test_manifest_load_bad_toml(config_dir: Path) -> None:
    """Malformed TOML raises ValueError."""
    (config_dir / "config.toml").write_text("project_prefix = [bad toml\n", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed TOML"):
        load_manifest(config_dir)


# ---------------------------------------------------------------------------
# 5. describe output shape
# ---------------------------------------------------------------------------


def test_cli_describe_no_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """describe with no config dir emits {"services": []} and exits 0."""
    rc = cli_main(["describe"])
    assert rc == 0
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc == {"services": []}


def test_cli_describe_with_services(config_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """describe with configured services emits the service name list."""
    (config_dir / "config.toml").write_text(
        'project_prefix = "myapp"\ncompose_file = "compose.yaml"\n'
        "[[service]]\nname = \"db\"\n"
        "[[service]]\nname = \"api\"\n",
        encoding="utf-8",
    )
    with patch.dict("os.environ", {"WINTER_EXT_CONFIG_DIR": str(config_dir)}):
        rc = cli_main(["describe"])
    assert rc == 0
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc == {"services": ["db", "api"]}


# ---------------------------------------------------------------------------
# 6. CLI exit codes
# ---------------------------------------------------------------------------


def test_cli_unknown_action_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main(["bogus"])
    assert rc == 2


def test_cli_no_action_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main([])
    assert rc == 2


@pytest.mark.parametrize("action", ["restart", "logs"])
def test_cli_known_actions_exit_non_2(action: str, capsys: pytest.CaptureFixture[str]) -> None:
    """restart and logs are implemented actions: calling without args exits non-2 (not unknown)."""
    rc = cli_main([action])
    assert rc != 2  # not "unknown action"
    assert rc != 3  # not "refuse/unimplemented"


# ---------------------------------------------------------------------------
# 7. build_env_context integration
# ---------------------------------------------------------------------------


def test_build_env_context_alpha(tmp_workspace: Path) -> None:
    ctx = build_env_context("alpha", "myapp", tmp_workspace)
    assert ctx.env == "alpha"
    assert ctx.compose_project_name == "myapp-alpha"
    assert ctx.port_base == 4020


def test_build_env_context_workspace(tmp_workspace: Path) -> None:
    ctx = build_env_context("workspace", "myapp", tmp_workspace)
    assert ctx.env == "workspace"
    assert ctx.compose_project_name == "myapp-workspace"
    assert ctx.port_base is None


# ---------------------------------------------------------------------------
# 8. FakeRunner / ComposeClient seam
# ---------------------------------------------------------------------------


def test_fake_runner_records_compose_call() -> None:
    """ComposeClient with FakeRunner records the full docker compose invocation."""
    result = subprocess.CompletedProcess([], 0, stdout='[{"Name": "myapp-alpha_db_1"}]', stderr="")
    runner = FakeRunner(results=[result])
    client = ComposeClient(runner=runner)
    ret = client.compose("myapp-alpha", "compose.yaml", ["ps", "--format=json"], capture_output=True)
    assert ret.stdout == '[{"Name": "myapp-alpha_db_1"}]'
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call.args == ["docker", "compose", "-p", "myapp-alpha", "-f", "compose.yaml", "ps", "--format=json"]
    assert call.capture_output is True


def test_fake_runner_records_docker_call() -> None:
    """ComposeClient.docker() with FakeRunner records the docker invocation."""
    result = subprocess.CompletedProcess([], 0, stdout="Docker version 24.0.0", stderr="")
    runner = FakeRunner(results=[result])
    client = ComposeClient(runner=runner)
    ret = client.docker(["version", "--format={{.Server.Version}}"], capture_output=True)
    assert ret.stdout == "Docker version 24.0.0"
    assert runner.calls[0].args == ["docker", "version", "--format={{.Server.Version}}"]


def test_fake_runner_default_result_when_exhausted() -> None:
    """FakeRunner returns default_result when the result list is exhausted."""
    default = subprocess.CompletedProcess([], 1, stdout="", stderr="error")
    runner = FakeRunner(default_result=default)
    client = ComposeClient(runner=runner)
    ret = client.docker(["info"])
    assert ret.returncode == 1


def test_fake_compose_client_records_compose_call() -> None:
    """FakeComposeClient records compose() calls at the method level."""
    fake = FakeComposeClient()
    fake.compose("proj-alpha", "compose.yaml", ["up", "-d"])
    assert len(fake.compose_calls) == 1
    c = fake.compose_calls[0]
    assert c.project == "proj-alpha"
    assert c.compose_file == "compose.yaml"
    assert c.args == ["up", "-d"]


def test_fake_compose_client_records_docker_call() -> None:
    """FakeComposeClient records docker() calls at the method level."""
    fake = FakeComposeClient()
    fake.docker(["info"])
    assert len(fake.docker_calls) == 1
    assert fake.docker_calls[0].args == ["info"]
