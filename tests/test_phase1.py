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
    read_service_prefix,
    resolve_project_prefix,
)
from docker_orchestrator.manifest import DockerManifest, ServiceDecl
from docker_orchestrator.manifest import load as load_manifest
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
# 2. Port-base parsing from the process environment
# ---------------------------------------------------------------------------


def test_read_port_base_alpha(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_port_base returns the integer value of WINTER_PORT_BASE from os.environ."""
    monkeypatch.setenv("WINTER_PORT_BASE", "4020")
    base = read_port_base()
    assert base == 4020


def test_read_port_base_absent() -> None:
    """When WINTER_PORT_BASE is not in the environment, returns None."""
    base = read_port_base()
    assert base is None


def test_read_port_base_workspace_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Workspace scope reads WINTER_PORT_BASE from the process environment like any other scope."""
    monkeypatch.setenv("WINTER_PORT_BASE", "4000")
    base = read_port_base()
    assert base == 4000


def test_read_port_base_absent_returns_none() -> None:
    """When WINTER_PORT_BASE is absent from the environment, returns None."""
    base = read_port_base()
    assert base is None


def test_read_port_base_custom_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_port_base returns any integer value injected via the process environment."""
    monkeypatch.setenv("WINTER_PORT_BASE", "4100")
    base = read_port_base()
    assert base == 4100


# ---------------------------------------------------------------------------
# 2b. WINTER_SERVICE_PREFIX resolution (issue #5)
# ---------------------------------------------------------------------------


def test_read_service_prefix_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_service_prefix returns WINTER_SERVICE_PREFIX from os.environ."""
    monkeypatch.setenv("WINTER_SERVICE_PREFIX", "myapp")
    assert read_service_prefix() == "myapp"


def test_read_service_prefix_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """When WINTER_SERVICE_PREFIX is not in the environment, returns None."""
    monkeypatch.delenv("WINTER_SERVICE_PREFIX", raising=False)
    assert read_service_prefix() is None


def test_read_service_prefix_empty_treated_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty WINTER_SERVICE_PREFIX value is treated as absent."""
    monkeypatch.setenv("WINTER_SERVICE_PREFIX", "")
    assert read_service_prefix() is None


def test_resolve_project_prefix_defaults_to_service_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no manifest override, resolve_project_prefix falls back to WINTER_SERVICE_PREFIX."""
    monkeypatch.setenv("WINTER_SERVICE_PREFIX", "winter")
    assert resolve_project_prefix(None) == "winter"


def test_resolve_project_prefix_manifest_override_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit manifest project_prefix override wins over WINTER_SERVICE_PREFIX."""
    monkeypatch.setenv("WINTER_SERVICE_PREFIX", "winter")
    assert resolve_project_prefix("myapp") == "myapp"


def test_resolve_project_prefix_none_when_neither_source_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent manifest override and absent WINTER_SERVICE_PREFIX resolves to None."""
    monkeypatch.delenv("WINTER_SERVICE_PREFIX", raising=False)
    assert resolve_project_prefix(None) is None


def test_build_env_context_prefix_from_resolve_project_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_env_context combined with resolve_project_prefix derives COMPOSE_PROJECT_NAME
    from WINTER_SERVICE_PREFIX when no manifest override is configured."""
    monkeypatch.setenv("WINTER_SERVICE_PREFIX", "myapp")
    monkeypatch.setenv("WINTER_PORT_BASE", "4020")
    prefix = resolve_project_prefix(None)
    assert prefix is not None
    ctx = build_env_context("alpha", prefix)
    assert ctx.compose_project_name == "myapp-alpha"
    assert ctx.port_base == 4020


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
    assert manifest.environment_compose_file is None
    assert manifest.workspace_compose_file is None
    assert manifest.services == ()


def test_manifest_load_missing_config_toml(config_dir: Path) -> None:
    """Config dir exists but has no config.toml → empty manifest."""
    manifest = load_manifest(config_dir)
    assert manifest.project_prefix is None
    assert manifest.environment_compose_file is None
    assert manifest.workspace_compose_file is None
    assert manifest.services == ()


def test_manifest_load_minimal(config_dir: Path) -> None:
    """Minimal config.toml with required fields only."""
    (config_dir / "config.toml").write_text(
        'project_prefix = "myapp"\n'
        'environment_compose_file = "compose.yaml"\n'
        'workspace_compose_file = "workspace-compose.yaml"\n',
        encoding="utf-8",
    )
    manifest = load_manifest(config_dir)
    assert manifest.project_prefix == "myapp"
    # Relative paths are resolved against the config dir, not cwd.
    assert manifest.environment_compose_file == str(config_dir / "compose.yaml")
    assert manifest.workspace_compose_file == str(config_dir / "workspace-compose.yaml")
    assert manifest.services == ()


def test_manifest_load_with_services(config_dir: Path) -> None:
    """config.toml with [[service]] entries."""
    (config_dir / "config.toml").write_text(
        'project_prefix = "proj"\n'
        'environment_compose_file = "docker/environment-compose.yaml"\n'
        'workspace_compose_file = "docker/workspace-compose.yaml"\n'
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
    # Relative nested paths resolve against the config dir.
    assert manifest.environment_compose_file == str(config_dir / "docker/environment-compose.yaml")
    assert manifest.workspace_compose_file == str(config_dir / "docker/workspace-compose.yaml")
    assert len(manifest.services) == 2
    assert manifest.services[0] == ServiceDecl(name="backend")
    assert manifest.services[1] == ServiceDecl(name="frontend")


def test_manifest_load_absolute_compose_file_passes_through(config_dir: Path) -> None:
    """Absolute compose file paths are used as-is, never re-anchored to the config dir."""
    (config_dir / "config.toml").write_text(
        'project_prefix = "myapp"\n'
        'environment_compose_file = "/etc/winter/environment-compose.yaml"\n'
        'workspace_compose_file = "/etc/winter/workspace-compose.yaml"\n',
        encoding="utf-8",
    )
    manifest = load_manifest(config_dir)
    assert manifest.environment_compose_file == "/etc/winter/environment-compose.yaml"
    assert manifest.workspace_compose_file == "/etc/winter/workspace-compose.yaml"


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
    """describe emits scope-qualified names: */db and */api for project-scoped services."""
    (config_dir / "config.toml").write_text(
        'project_prefix = "myapp"\n'
        'environment_compose_file = "compose.yaml"\n'
        'workspace_compose_file = "workspace-compose.yaml"\n'
        '[[service]]\nname = "db"\n'
        '[[service]]\nname = "api"\n',
        encoding="utf-8",
    )
    with patch.dict("os.environ", {"WINTER_EXT_CONFIG_DIR": str(config_dir)}):
        rc = cli_main(["describe"])
    assert rc == 0
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    # describe now emits scope-qualified names (*/name for project, workspace/name for workspace)
    # so that winter-cli core can split the workspace vs per-env axis in the status call-matrix.
    assert set(doc["services"]) == {"*/db", "*/api"}


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


def test_build_env_context_alpha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINTER_PORT_BASE", "4020")
    ctx = build_env_context("alpha", "myapp")
    assert ctx.env == "alpha"
    assert ctx.compose_project_name == "myapp-alpha"
    assert ctx.port_base == 4020


def test_build_env_context_workspace() -> None:
    ctx = build_env_context("workspace", "myapp")
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
