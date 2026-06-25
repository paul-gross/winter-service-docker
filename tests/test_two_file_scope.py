"""Tests for the two-file scope split introduced in issue #4.

Covers:
1. Two-file selection: up/down use environment_compose_file for env scope and
   workspace_compose_file for workspace scope.
2. Two-file selection for restart: correct file per service scope.
3. Two-file selection for logs: correct file per service scope.
4. Scaffold emits three files (environment-compose.yaml, workspace-compose.yaml, config.toml).
5. Scaffold config.toml uses environment_compose_file / workspace_compose_file keys.
6. Back-compat: legacy compose_file-only config raises ValueError with migration message.
7. compose_file_for_scope returns the correct file for each scope.
8. status uses the correct file for each scope.
"""

from __future__ import annotations

import json
import subprocess
from io import StringIO
from pathlib import Path

import pytest

from docker_orchestrator.lifecycle import cmd_down, cmd_up
from docker_orchestrator.logs import cmd_logs
from docker_orchestrator.manifest import DockerManifest, ServiceDecl, load
from docker_orchestrator.restart import cmd_restart
from docker_orchestrator.scaffold import scaffold
from docker_orchestrator.status import cmd_status
from tests.fakes import FakeComposeClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_result(returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout="", stderr="")


def _ps_result(containers: list[dict], returncode: int = 0) -> subprocess.CompletedProcess:
    stdout = "\n".join(json.dumps(c) for c in containers)
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def _running_container(svc: str, project: str = "myapp-alpha") -> dict:
    return {"Service": svc, "State": "running", "Name": f"{project}-{svc}-1"}


def _make_manifest(
    env_file: str = "/cfg/environment-compose.yaml",
    ws_file: str = "/cfg/workspace-compose.yaml",
    project_services: list[str] | None = None,
    workspace_services: list[str] | None = None,
) -> DockerManifest:
    p_svcs = tuple(ServiceDecl(name=s) for s in (project_services or ["backend"]))
    ws_svcs = tuple(ServiceDecl(name=s) for s in (workspace_services or ["db"]))
    return DockerManifest(
        project_prefix="myapp",
        environment_compose_file=env_file,
        workspace_compose_file=ws_file,
        services=p_svcs,
        workspace_services=ws_svcs,
    )


def _clock():
    state = [0.0]

    def time_fn() -> float:
        t = state[0]
        state[0] += 200.0
        return t

    return time_fn, (lambda _: None)


# ---------------------------------------------------------------------------
# 1. compose_file_for_scope
# ---------------------------------------------------------------------------


class TestComposeFileForScope:
    def test_env_scope_returns_environment_file(self) -> None:
        manifest = _make_manifest(env_file="/cfg/env.yaml", ws_file="/cfg/ws.yaml")
        assert manifest.compose_file_for_scope("alpha") == "/cfg/env.yaml"

    def test_workspace_scope_returns_workspace_file(self) -> None:
        manifest = _make_manifest(env_file="/cfg/env.yaml", ws_file="/cfg/ws.yaml")
        assert manifest.compose_file_for_scope("workspace") == "/cfg/ws.yaml"

    def test_beta_scope_returns_environment_file(self) -> None:
        manifest = _make_manifest(env_file="/cfg/env.yaml", ws_file="/cfg/ws.yaml")
        assert manifest.compose_file_for_scope("beta") == "/cfg/env.yaml"

    def test_none_environment_file_returns_none_for_env(self) -> None:
        manifest = DockerManifest(
            project_prefix="myapp",
            environment_compose_file=None,
            workspace_compose_file="/cfg/ws.yaml",
            services=(),
        )
        assert manifest.compose_file_for_scope("alpha") is None

    def test_none_workspace_file_returns_none_for_workspace(self) -> None:
        manifest = DockerManifest(
            project_prefix="myapp",
            environment_compose_file="/cfg/env.yaml",
            workspace_compose_file=None,
            services=(),
        )
        assert manifest.compose_file_for_scope("workspace") is None


# ---------------------------------------------------------------------------
# 2. Two-file selection: up
# ---------------------------------------------------------------------------


class TestUpTwoFileSelection:
    def test_up_alpha_uses_environment_file(self, tmp_path: Path) -> None:
        """up alpha selects environment_compose_file, not workspace_compose_file."""
        manifest = _make_manifest(
            env_file="/cfg/environment-compose.yaml",
            ws_file="/cfg/workspace-compose.yaml",
        )
        time_fn, sleep_fn = _clock()
        client = FakeComposeClient(
            compose_results=[
                _ok_result(0),
                _ps_result([_running_container("backend")]),
            ]
        )
        rc = cmd_up("alpha", manifest, tmp_path, client, time_fn=time_fn, sleep_fn=sleep_fn, timeout=10.0)
        assert rc == 0
        up_call = client.compose_calls[0]
        assert up_call.compose_file == "/cfg/environment-compose.yaml"
        assert up_call.project == "myapp-alpha"

    def test_up_workspace_uses_workspace_file(self, tmp_path: Path) -> None:
        """up workspace selects workspace_compose_file, not environment_compose_file."""
        manifest = _make_manifest(
            env_file="/cfg/environment-compose.yaml",
            ws_file="/cfg/workspace-compose.yaml",
        )
        time_fn, sleep_fn = _clock()
        client = FakeComposeClient(
            compose_results=[
                _ok_result(0),
                _ps_result([_running_container("db", project="myapp-workspace")]),
            ]
        )
        rc = cmd_up("workspace", manifest, tmp_path, client, time_fn=time_fn, sleep_fn=sleep_fn, timeout=10.0)
        assert rc == 0
        up_call = client.compose_calls[0]
        assert up_call.compose_file == "/cfg/workspace-compose.yaml"
        assert up_call.project == "myapp-workspace"

    def test_up_uses_arg_less_compose_up(self, tmp_path: Path) -> None:
        """up issues compose up -d without per-service-name args (scope-pure file)."""
        manifest = _make_manifest()
        time_fn, sleep_fn = _clock()
        client = FakeComposeClient(
            compose_results=[
                _ok_result(0),
                _ps_result([_running_container("backend")]),
            ]
        )
        cmd_up("alpha", manifest, tmp_path, client, time_fn=time_fn, sleep_fn=sleep_fn, timeout=10.0)
        up_call = client.compose_calls[0]
        assert up_call.args == ["up", "-d"]

    def test_up_readiness_poll_also_uses_correct_file(self, tmp_path: Path) -> None:
        """The readiness-gate ps poll uses the same scope-correct compose file as up."""
        manifest = _make_manifest(
            env_file="/cfg/environment-compose.yaml",
            ws_file="/cfg/workspace-compose.yaml",
        )
        time_fn, sleep_fn = _clock()
        client = FakeComposeClient(
            compose_results=[
                _ok_result(0),
                _ps_result([_running_container("backend")]),
            ]
        )
        cmd_up("alpha", manifest, tmp_path, client, time_fn=time_fn, sleep_fn=sleep_fn, timeout=10.0)
        ps_calls = [c for c in client.compose_calls if "ps" in c.args]
        assert len(ps_calls) == 1
        assert ps_calls[0].compose_file == "/cfg/environment-compose.yaml"


# ---------------------------------------------------------------------------
# 3. Two-file selection: down
# ---------------------------------------------------------------------------


class TestDownTwoFileSelection:
    def test_down_alpha_uses_environment_file(self, tmp_path: Path) -> None:
        """down alpha selects environment_compose_file."""
        manifest = _make_manifest(
            env_file="/cfg/environment-compose.yaml",
            ws_file="/cfg/workspace-compose.yaml",
        )
        client = FakeComposeClient(compose_results=[_ok_result(0)])
        cmd_down("alpha", manifest, tmp_path, client)
        call = client.compose_calls[0]
        assert call.compose_file == "/cfg/environment-compose.yaml"
        assert call.project == "myapp-alpha"

    def test_down_workspace_uses_workspace_file(self, tmp_path: Path) -> None:
        """down workspace selects workspace_compose_file."""
        manifest = _make_manifest(
            env_file="/cfg/environment-compose.yaml",
            ws_file="/cfg/workspace-compose.yaml",
        )
        client = FakeComposeClient(compose_results=[_ok_result(0)])
        cmd_down("workspace", manifest, tmp_path, client)
        call = client.compose_calls[0]
        assert call.compose_file == "/cfg/workspace-compose.yaml"
        assert call.project == "myapp-workspace"

    def test_down_alpha_missing_env_file_returns_nonzero(self, tmp_path: Path) -> None:
        """down alpha returns non-zero when environment_compose_file is None."""
        manifest = DockerManifest(
            project_prefix="myapp",
            environment_compose_file=None,
            workspace_compose_file="/cfg/ws.yaml",
            services=(ServiceDecl("backend"),),
        )
        client = FakeComposeClient()
        rc = cmd_down("alpha", manifest, tmp_path, client)
        assert rc != 0
        assert client.compose_calls == []

    def test_down_workspace_missing_ws_file_returns_nonzero(self, tmp_path: Path) -> None:
        """down workspace returns non-zero when workspace_compose_file is None."""
        manifest = DockerManifest(
            project_prefix="myapp",
            environment_compose_file="/cfg/env.yaml",
            workspace_compose_file=None,
            workspace_services=(ServiceDecl("db"),),
        )
        client = FakeComposeClient()
        rc = cmd_down("workspace", manifest, tmp_path, client)
        assert rc != 0
        assert client.compose_calls == []


# ---------------------------------------------------------------------------
# 4. Two-file selection: restart
# ---------------------------------------------------------------------------


class TestRestartTwoFileSelection:
    def test_restart_alpha_service_uses_environment_file(self, tmp_path: Path) -> None:
        """restart alpha/backend selects environment_compose_file."""
        manifest = _make_manifest(
            env_file="/cfg/environment-compose.yaml",
            ws_file="/cfg/workspace-compose.yaml",
        )
        client = FakeComposeClient(compose_default=_ok_result(0))
        cmd_restart(["alpha/backend"], manifest, tmp_path, client)
        assert len(client.compose_calls) == 1
        call = client.compose_calls[0]
        assert call.compose_file == "/cfg/environment-compose.yaml"
        assert call.project == "myapp-alpha"

    def test_restart_workspace_service_uses_workspace_file(self, tmp_path: Path) -> None:
        """restart workspace/db selects workspace_compose_file."""
        manifest = _make_manifest(
            env_file="/cfg/environment-compose.yaml",
            ws_file="/cfg/workspace-compose.yaml",
        )
        client = FakeComposeClient(compose_default=_ok_result(0))
        cmd_restart(["workspace/db"], manifest, tmp_path, client)
        assert len(client.compose_calls) == 1
        call = client.compose_calls[0]
        assert call.compose_file == "/cfg/workspace-compose.yaml"
        assert call.project == "myapp-workspace"

    def test_restart_missing_env_file_returns_nonzero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """restart alpha/backend returns non-zero when environment_compose_file is None."""
        manifest = DockerManifest(
            project_prefix="myapp",
            environment_compose_file=None,
            workspace_compose_file="/cfg/ws.yaml",
            services=(ServiceDecl("backend"),),
        )
        client = FakeComposeClient()
        rc = cmd_restart(["alpha/backend"], manifest, tmp_path, client)
        assert rc != 0
        err = capsys.readouterr().err
        assert "manifest is missing" in err


# ---------------------------------------------------------------------------
# 5. Two-file selection: logs
# ---------------------------------------------------------------------------


class TestLogsTwoFileSelection:
    def test_logs_alpha_service_uses_environment_file(self, tmp_path: Path) -> None:
        """logs alpha/backend selects environment_compose_file."""
        manifest = _make_manifest(
            env_file="/cfg/environment-compose.yaml",
            ws_file="/cfg/workspace-compose.yaml",
        )
        client = FakeComposeClient(compose_default=_ok_result())
        sink = StringIO()
        cmd_logs(["alpha/backend"], manifest, tmp_path, client, sink=sink)
        assert len(client.compose_calls) == 1
        call = client.compose_calls[0]
        assert call.compose_file == "/cfg/environment-compose.yaml"
        assert call.project == "myapp-alpha"

    def test_logs_workspace_service_uses_workspace_file(self, tmp_path: Path) -> None:
        """logs workspace/db selects workspace_compose_file."""
        manifest = _make_manifest(
            env_file="/cfg/environment-compose.yaml",
            ws_file="/cfg/workspace-compose.yaml",
        )
        client = FakeComposeClient(compose_default=_ok_result())
        sink = StringIO()
        cmd_logs(["workspace/db"], manifest, tmp_path, client, sink=sink)
        assert len(client.compose_calls) == 1
        call = client.compose_calls[0]
        assert call.compose_file == "/cfg/workspace-compose.yaml"
        assert call.project == "myapp-workspace"

    def test_logs_missing_env_file_returns_nonzero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """logs alpha/backend returns non-zero when environment_compose_file is None."""
        manifest = DockerManifest(
            project_prefix="myapp",
            environment_compose_file=None,
            workspace_compose_file="/cfg/ws.yaml",
            services=(ServiceDecl("backend"),),
        )
        client = FakeComposeClient()
        sink = StringIO()
        rc = cmd_logs(["alpha/backend"], manifest, tmp_path, client, sink=sink)
        assert rc != 0
        err = capsys.readouterr().err
        assert "manifest is missing" in err


# ---------------------------------------------------------------------------
# 6. Status two-file selection
# ---------------------------------------------------------------------------


class TestStatusTwoFileSelection:
    def test_status_alpha_uses_environment_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """status alpha queries using environment_compose_file."""
        manifest = _make_manifest(
            env_file="/cfg/environment-compose.yaml",
            ws_file="/cfg/workspace-compose.yaml",
        )
        client = FakeComposeClient(compose_results=[_ps_result([_running_container("backend")])])
        cmd_status(["alpha"], manifest, tmp_path, client)
        call = client.compose_calls[0]
        assert call.compose_file == "/cfg/environment-compose.yaml"

    def test_status_workspace_uses_workspace_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """status workspace queries using workspace_compose_file."""
        manifest = _make_manifest(
            env_file="/cfg/environment-compose.yaml",
            ws_file="/cfg/workspace-compose.yaml",
        )
        client = FakeComposeClient(compose_results=[_ps_result([_running_container("db", project="myapp-workspace")])])
        cmd_status(["workspace"], manifest, tmp_path, client)
        call = client.compose_calls[0]
        assert call.compose_file == "/cfg/workspace-compose.yaml"


# ---------------------------------------------------------------------------
# 7. Two-file scaffold output
# ---------------------------------------------------------------------------


class TestScaffoldTwoFiles:
    def test_scaffold_writes_three_files(self, tmp_path: Path) -> None:
        """scaffold writes environment-compose.yaml, workspace-compose.yaml, and config.toml."""
        written = scaffold(tmp_path)
        names = {p.name for p in written}
        assert "environment-compose.yaml" in names
        assert "workspace-compose.yaml" in names
        assert "config.toml" in names
        assert len(written) == 3

    def test_config_toml_has_environment_compose_file_key(self, tmp_path: Path) -> None:
        """Scaffolded config.toml contains environment_compose_file key."""
        scaffold(tmp_path)
        content = (tmp_path / "config.toml").read_text()
        assert "environment_compose_file" in content

    def test_config_toml_has_workspace_compose_file_key(self, tmp_path: Path) -> None:
        """Scaffolded config.toml contains workspace_compose_file key."""
        scaffold(tmp_path)
        content = (tmp_path / "config.toml").read_text()
        assert "workspace_compose_file" in content

    def test_config_toml_does_not_have_legacy_compose_file_key(self, tmp_path: Path) -> None:
        """Scaffolded config.toml must not contain the legacy compose_file key."""
        scaffold(tmp_path)
        content = (tmp_path / "config.toml").read_text()
        # Must not have the bare 'compose_file' key (it has environment_compose_file
        # and workspace_compose_file but NOT a plain compose_file).
        lines = [line.strip() for line in content.splitlines()]
        assert not any(line.startswith("compose_file") for line in lines), (
            "config.toml must not contain legacy compose_file key"
        )

    def test_environment_compose_yaml_has_wsd_port_substitution(self, tmp_path: Path) -> None:
        """environment-compose.yaml uses ${WSD_PORT_*} placeholders."""
        scaffold(tmp_path)
        content = (tmp_path / "environment-compose.yaml").read_text()
        assert "${WSD_PORT_" in content

    def test_workspace_compose_yaml_has_named_volume(self, tmp_path: Path) -> None:
        """workspace-compose.yaml declares a named volume (singleton persistence)."""
        scaffold(tmp_path)
        content = (tmp_path / "workspace-compose.yaml").read_text()
        assert "volumes:" in content

    def test_environment_compose_yaml_does_not_mix_scopes(self, tmp_path: Path) -> None:
        """environment-compose.yaml must not mention workspace singleton services.

        The scaffolded files are scope-pure: each file contains only its scope's
        services. Workspace comments/services must not appear in environment file.
        """
        scaffold(tmp_path)
        env_content = (tmp_path / "environment-compose.yaml").read_text()
        # The environment file should not contain workspace scope language
        # (workspace services like 'db:' belong only in workspace-compose.yaml)
        assert "workspace-compose.yaml" not in env_content

    def test_scaffolded_config_toml_is_parseable_by_load(self, tmp_path: Path) -> None:
        """The scaffolded config.toml can be loaded by manifest.load() without error."""
        scaffold(tmp_path)
        # Load should succeed (parse the new format keys without raising ValueError)
        import docker_orchestrator.manifest as manifest_mod

        manifest = manifest_mod.load(tmp_path)
        assert manifest.project_prefix == "myapp"
        assert manifest.environment_compose_file is not None
        assert manifest.workspace_compose_file is not None


# ---------------------------------------------------------------------------
# 8. Back-compat: legacy compose_file key raises ValueError with migration message
# ---------------------------------------------------------------------------


class TestLegacyComposeFileBackCompat:
    def test_legacy_compose_file_raises_value_error(self, config_dir: Path) -> None:
        """A config.toml with the legacy compose_file key raises ValueError."""
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\ncompose_file = "compose.yaml"\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            load(config_dir)

    def test_legacy_compose_file_error_message_mentions_migration(self, config_dir: Path) -> None:
        """The error message tells the user what to do (migration guidance)."""
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\ncompose_file = "compose.yaml"\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="environment_compose_file"):
            load(config_dir)

    def test_legacy_compose_file_error_mentions_workspace_compose_file(self, config_dir: Path) -> None:
        """The error message also mentions workspace_compose_file."""
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\ncompose_file = "compose.yaml"\n[[service]]\nname = "backend"\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="workspace_compose_file"):
            load(config_dir)

    def test_new_format_does_not_raise(self, config_dir: Path) -> None:
        """A config.toml with the new keys loads successfully."""
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\n'
            'environment_compose_file = "environment-compose.yaml"\n'
            'workspace_compose_file = "workspace-compose.yaml"\n',
            encoding="utf-8",
        )
        manifest = load(config_dir)
        assert manifest.project_prefix == "myapp"
        assert manifest.environment_compose_file == str(config_dir / "environment-compose.yaml")
        assert manifest.workspace_compose_file == str(config_dir / "workspace-compose.yaml")

    def test_partial_new_format_no_legacy_key_does_not_raise(self, config_dir: Path) -> None:
        """A config.toml with only environment_compose_file (no workspace) loads gracefully."""
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\nenvironment_compose_file = "environment-compose.yaml"\n',
            encoding="utf-8",
        )
        manifest = load(config_dir)
        assert manifest.environment_compose_file == str(config_dir / "environment-compose.yaml")
        assert manifest.workspace_compose_file is None
