"""Phase 6 unit tests — comprehensive scope behavior for winter-service-docker.

Covers:
1.  Manifest partition/validation: default scope lands in services; explicit
    "workspace" lands in workspace_services; invalid scope raises ValueError;
    duplicate name across scopes raises ValueError; services_for_scope returns
    correct partition for each env value.
2.  up/down scope filtering with a MIXED manifest (project "backend" + workspace
    "db"): per-env up uses project partition; workspace up uses workspace
    partition; a workspace service is excluded from per-env up; a project
    service is excluded from workspace up.
3.  WSD_PORT_* offset numbered over the scoped list (not the full manifest).
4.  status/restart/logs scope-correctness: alpha shows project services only;
    workspace shows workspace services only; cross-scope restart/logs find
    nothing.
5.  describe enumerates both scopes.
6.  Default-scope back-compat: a manifest with no scope field anywhere behaves
    exactly as before (all services are project-scoped, per-env).
"""

from __future__ import annotations

import json
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from docker_orchestrator.cli import main as cli_main
from docker_orchestrator.env_context import build_env_context
from docker_orchestrator.lifecycle import _build_compose_env, cmd_down, cmd_up
from docker_orchestrator.logs import _collect_log_targets, cmd_logs
from docker_orchestrator.manifest import DockerManifest, ServiceDecl
from docker_orchestrator.manifest import load as load_manifest
from docker_orchestrator.restart import _collect_restart_targets, cmd_restart
from docker_orchestrator.status import cmd_status
from tests.fakes import FakeComposeClient

# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------


def _mixed_manifest(
    prefix: str = "myapp",
    compose_file: str = "compose.yaml",
    project_services: list[str] | None = None,
    workspace_services: list[str] | None = None,
) -> DockerManifest:
    """Build a manifest with both project and workspace services."""
    p_svcs = tuple(ServiceDecl(name=s) for s in (project_services or ["backend"]))
    ws_svcs = tuple(ServiceDecl(name=s) for s in (workspace_services or ["db"]))
    return DockerManifest(
        project_prefix=prefix,
        environment_compose_file=compose_file,
        workspace_compose_file=compose_file,
        services=p_svcs,
        workspace_services=ws_svcs,
    )


def _project_only_manifest(
    prefix: str = "myapp",
    compose_file: str = "compose.yaml",
    services: list[str] | None = None,
) -> DockerManifest:
    """Build a manifest with only project services (no scope field — back-compat)."""
    svcs = tuple(ServiceDecl(name=s) for s in (services or ["db", "api"]))
    return DockerManifest(
        project_prefix=prefix,
        environment_compose_file=compose_file,
        workspace_compose_file=compose_file,
        services=svcs,
        workspace_services=(),
    )


def _ok_result(returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout="", stderr="")


def _ps_result(containers: list[dict], returncode: int = 0) -> subprocess.CompletedProcess:
    stdout = "\n".join(json.dumps(c) for c in containers)
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def _running_container(svc: str, project: str = "myapp-alpha") -> dict:
    return {"Service": svc, "State": "running", "Name": f"{project}-{svc}-1"}


def _clock():
    """Return (time_fn, sleep_fn) pair that advances on each time() call."""
    state = [0.0]

    def time_fn() -> float:
        t = state[0]
        state[0] += 200.0
        return t

    def sleep_fn(n: float) -> None:
        pass

    return time_fn, sleep_fn


def _alpha_workspace(tmp_path: Path) -> Path:
    """Seed a workspace root with an alpha env .winter.env (port_base=4020)."""
    alpha_dir = tmp_path / "alpha"
    alpha_dir.mkdir(parents=True, exist_ok=True)
    (alpha_dir / ".winter.env").write_text(
        "WINTER_ENV=alpha\nWINTER_ENV_INDEX=1\nWINTER_PORT_BASE=4020\n",
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Manifest partition / validation
# ---------------------------------------------------------------------------


class TestManifestPartition:
    """Scope parsing, partition, and validation tests."""

    def test_default_scope_lands_in_services(self, config_dir: Path) -> None:
        """A [[service]] with no scope field defaults to project-scoped."""
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\nenvironment_compose_file = "compose.yaml"\nworkspace_compose_file = "workspace-compose.yaml"\n[[service]]\nname = "backend"\n',
            encoding="utf-8",
        )
        manifest = load_manifest(config_dir)
        assert len(manifest.services) == 1
        assert manifest.services[0].name == "backend"
        assert manifest.workspace_services == ()

    def test_explicit_project_scope_lands_in_services(self, config_dir: Path) -> None:
        """scope = "project" explicitly → lands in services."""
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\nenvironment_compose_file = "compose.yaml"\nworkspace_compose_file = "workspace-compose.yaml"\n'
            '[[service]]\nname = "backend"\nscope = "project"\n',
            encoding="utf-8",
        )
        manifest = load_manifest(config_dir)
        assert len(manifest.services) == 1
        assert manifest.services[0].name == "backend"
        assert manifest.workspace_services == ()

    def test_workspace_scope_lands_in_workspace_services(self, config_dir: Path) -> None:
        """scope = "workspace" → lands in workspace_services, not services."""
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\nenvironment_compose_file = "compose.yaml"\nworkspace_compose_file = "workspace-compose.yaml"\n[[service]]\nname = "db"\nscope = "workspace"\n',
            encoding="utf-8",
        )
        manifest = load_manifest(config_dir)
        assert manifest.services == ()
        assert len(manifest.workspace_services) == 1
        assert manifest.workspace_services[0].name == "db"

    def test_mixed_scope_partition(self, config_dir: Path) -> None:
        """Mixed [[service]] entries partition correctly."""
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\nenvironment_compose_file = "compose.yaml"\nworkspace_compose_file = "workspace-compose.yaml"\n'
            '[[service]]\nname = "backend"\n'
            '[[service]]\nname = "db"\nscope = "workspace"\n'
            '[[service]]\nname = "api"\nscope = "project"\n',
            encoding="utf-8",
        )
        manifest = load_manifest(config_dir)
        project_names = [s.name for s in manifest.services]
        ws_names = [s.name for s in manifest.workspace_services]
        assert set(project_names) == {"backend", "api"}
        assert ws_names == ["db"]

    def test_invalid_scope_raises_value_error(self, config_dir: Path) -> None:
        """An unrecognized scope value raises ValueError."""
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\nenvironment_compose_file = "compose.yaml"\nworkspace_compose_file = "workspace-compose.yaml"\n[[service]]\nname = "svc"\nscope = "global"\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="invalid scope"):
            load_manifest(config_dir)

    def test_duplicate_name_within_project_raises(self, config_dir: Path) -> None:
        """Duplicate name within project scope raises ValueError."""
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\nenvironment_compose_file = "compose.yaml"\nworkspace_compose_file = "workspace-compose.yaml"\n'
            '[[service]]\nname = "db"\n'
            '[[service]]\nname = "db"\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="duplicate name"):
            load_manifest(config_dir)

    def test_duplicate_name_across_scopes_raises(self, config_dir: Path) -> None:
        """A name used in both project and workspace scope raises ValueError.

        This is the global-unique-name requirement: names form a single namespace
        across both partitions.
        """
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\nenvironment_compose_file = "compose.yaml"\nworkspace_compose_file = "workspace-compose.yaml"\n'
            '[[service]]\nname = "db"\n'
            '[[service]]\nname = "db"\nscope = "workspace"\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="duplicate name"):
            load_manifest(config_dir)

    def test_services_for_scope_workspace_returns_workspace_partition(self) -> None:
        """services_for_scope("workspace") returns workspace_services."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        result = manifest.services_for_scope("workspace")
        names = [s.name for s in result]
        assert names == ["db"]

    def test_services_for_scope_env_returns_project_partition(self) -> None:
        """services_for_scope("alpha") returns services (project partition)."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        result = manifest.services_for_scope("alpha")
        names = [s.name for s in result]
        assert names == ["backend"]

    def test_services_for_scope_any_env_name_returns_project(self) -> None:
        """Any env name other than "workspace" returns the project partition."""
        manifest = _mixed_manifest(project_services=["api"], workspace_services=["cache"])
        for env in ("alpha", "beta", "gamma", "prod"):
            result = manifest.services_for_scope(env)
            assert [s.name for s in result] == ["api"], f"failed for env={env!r}"


# ---------------------------------------------------------------------------
# 2. up/down scope filtering
# ---------------------------------------------------------------------------


class TestUpDownScopeFiltering:
    """up workspace uses workspace partition; up alpha uses project partition."""

    def test_up_alpha_calls_project_partition_only(self, tmp_path: Path) -> None:
        """CORE PROBE: up alpha uses the environment compose file and the per-env project.

        With a mixed manifest (backend=project, db=workspace):
        - compose up is called with project myapp-alpha
        - compose file is the environment_compose_file (scope-pure: only backend)
        - up args are ["up", "-d"] (no per-service-name masking; file scope is isolation)
        - WSD_PORT_BACKEND is present in the env
        - WSD_PORT_DB is NOT present in the env
        This is the PRIMARY acceptance criterion for scope isolation — guaranteed
        by file layout rather than service-name masking.
        """
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        ws = _alpha_workspace(tmp_path)
        time_fn, sleep_fn = _clock()
        client = FakeComposeClient(
            compose_results=[
                _ok_result(0),  # compose up -d
                _ps_result([_running_container("backend")]),  # readiness poll
            ]
        )
        rc = cmd_up("alpha", manifest, ws, client, time_fn=time_fn, sleep_fn=sleep_fn, timeout=10.0)
        assert rc == 0

        up_call = client.compose_calls[0]
        env = up_call.env or {}
        # Project name targets the per-env project
        assert up_call.project == "myapp-alpha"
        # No per-service-name masking: the scope-pure file enforces isolation
        assert up_call.args == ["up", "-d"], f"expected ['up', '-d']; got {up_call.args!r}"
        # The environment compose file is used (not the workspace file)
        assert up_call.compose_file == manifest.environment_compose_file
        # Project service gets a port var
        assert "WSD_PORT_BACKEND" in env, "expected WSD_PORT_BACKEND for project service"
        # Workspace service does NOT get a port var
        assert "WSD_PORT_DB" not in env, "WSD_PORT_DB must not appear in per-env up"

    def test_up_workspace_calls_workspace_partition_only(self, tmp_path: Path) -> None:
        """CORE PROBE: up workspace uses the workspace compose file and the workspace project.

        With a mixed manifest (backend=project, db=workspace):
        - compose up is called with project myapp-workspace
        - compose file is workspace_compose_file (scope-pure: only db)
        - up args are ["up", "-d"] (no per-service-name masking)
        - NO WSD_PORT_* vars are present (workspace scope has no port base)
        This is the PRIMARY acceptance criterion for scope isolation — guaranteed
        by file layout rather than service-name masking.
        """
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        time_fn, sleep_fn = _clock()
        client = FakeComposeClient(
            compose_results=[
                _ok_result(0),  # compose up -d
                _ps_result([_running_container("db", project="myapp-workspace")]),  # readiness poll
            ]
        )
        rc = cmd_up("workspace", manifest, tmp_path, client, time_fn=time_fn, sleep_fn=sleep_fn, timeout=10.0)
        assert rc == 0

        up_call = client.compose_calls[0]
        env = up_call.env or {}
        # Project name targets the workspace project
        assert up_call.project == "myapp-workspace"
        # No per-service-name masking: the scope-pure file enforces isolation
        assert up_call.args == ["up", "-d"], f"expected ['up', '-d']; got {up_call.args!r}"
        # The workspace compose file is used (not the environment file)
        assert up_call.compose_file == manifest.workspace_compose_file
        # No WSD_PORT_* at all for workspace scope
        port_keys = [k for k in env if k.startswith("WSD_PORT_")]
        assert port_keys == [], f"unexpected WSD_PORT_* in workspace up: {port_keys}"

    def test_workspace_service_excluded_from_per_env_up(self, tmp_path: Path) -> None:
        """Workspace-scoped services are in a separate file; per-env up uses environment file only.

        With two scope-pure files, the environment_compose_file does not contain
        workspace services.  The readiness poll only queries myapp-alpha.
        """
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        ws = _alpha_workspace(tmp_path)
        time_fn, sleep_fn = _clock()
        client = FakeComposeClient(
            compose_results=[
                _ok_result(0),
                _ps_result([_running_container("backend")]),
            ]
        )
        cmd_up("alpha", manifest, ws, client, time_fn=time_fn, sleep_fn=sleep_fn, timeout=10.0)

        # The up call must use the environment compose file (scope-pure isolation)
        up_call = client.compose_calls[0]
        assert up_call.compose_file == manifest.environment_compose_file

        # Only one compose call (up -d) and one ps poll should be made
        # The ps poll queries myapp-alpha, NOT myapp-workspace
        ps_calls = [c for c in client.compose_calls if "ps" in c.args]
        assert len(ps_calls) >= 1
        for ps_call in ps_calls:
            assert ps_call.project == "myapp-alpha", (
                f"ps called on wrong project: {ps_call.project!r} — workspace scope must not bleed into per-env up"
            )

    def test_project_service_excluded_from_workspace_up(self, tmp_path: Path) -> None:
        """Project-scoped services are in a separate file; workspace up uses workspace file only.

        The workspace_compose_file does not contain project services.
        """
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        time_fn, sleep_fn = _clock()
        client = FakeComposeClient(
            compose_results=[
                _ok_result(0),
                _ps_result([_running_container("db", project="myapp-workspace")]),
            ]
        )
        cmd_up("workspace", manifest, tmp_path, client, time_fn=time_fn, sleep_fn=sleep_fn, timeout=10.0)

        up_call = client.compose_calls[0]
        env = up_call.env or {}
        # The workspace compose file is used (scope-pure isolation)
        assert up_call.compose_file == manifest.workspace_compose_file
        assert "WSD_PORT_BACKEND" not in env, (
            "WSD_PORT_BACKEND must not appear in workspace up — backend is project-scoped"
        )

    def test_up_empty_workspace_scope_no_compose_call(self, tmp_path: Path) -> None:
        """EMPTY-SCOPE GUARD: up workspace with no workspace services makes no compose call.

        When a manifest declares only project services and 'up workspace' is called,
        scoped_services is empty.  cmd_up must return 0 without calling compose at all
        (calling 'up -d' with no names would start ALL services and defeat isolation).
        """
        manifest = DockerManifest(
            project_prefix="myapp",
            environment_compose_file="compose.yaml",
            workspace_compose_file="compose.yaml",
            services=(ServiceDecl(name="backend"),),
            workspace_services=(),  # no workspace services
        )
        client = FakeComposeClient()
        rc = cmd_up("workspace", manifest, tmp_path, client)
        assert rc == 0, f"expected 0, got {rc}"
        up_calls = [c for c in client.compose_calls if "up" in c.args]
        assert up_calls == [], (
            f"compose 'up' must not be called when no workspace services declared; "
            f"got calls: {[c.args for c in up_calls]}"
        )

    def test_up_empty_project_scope_no_compose_call(self, tmp_path: Path) -> None:
        """EMPTY-SCOPE GUARD: up alpha with no project services makes no compose call.

        When a manifest declares only workspace services and 'up alpha' is called,
        scoped_services is empty.  cmd_up must return 0 without calling compose.
        """
        manifest = DockerManifest(
            project_prefix="myapp",
            environment_compose_file="compose.yaml",
            workspace_compose_file="compose.yaml",
            services=(),  # no project services
            workspace_services=(ServiceDecl(name="db"),),
        )
        ws = _alpha_workspace(tmp_path)
        client = FakeComposeClient()
        rc = cmd_up("alpha", manifest, ws, client)
        assert rc == 0, f"expected 0, got {rc}"
        up_calls = [c for c in client.compose_calls if "up" in c.args]
        assert up_calls == [], (
            f"compose 'up' must not be called when no project services declared; "
            f"got calls: {[c.args for c in up_calls]}"
        )

    def test_down_alpha_targets_per_env_project(self, tmp_path: Path) -> None:
        """cmd_down for alpha uses myapp-alpha, not myapp-workspace."""
        manifest = _mixed_manifest()
        ws = _alpha_workspace(tmp_path)
        client = FakeComposeClient(compose_results=[_ok_result(0)])
        cmd_down("alpha", manifest, ws, client)
        assert client.compose_calls[0].project == "myapp-alpha"

    def test_down_workspace_targets_workspace_project(self, tmp_path: Path) -> None:
        """cmd_down for workspace uses myapp-workspace."""
        manifest = _mixed_manifest()
        client = FakeComposeClient(compose_results=[_ok_result(0)])
        cmd_down("workspace", manifest, tmp_path, client)
        assert client.compose_calls[0].project == "myapp-workspace"


# ---------------------------------------------------------------------------
# 3. WSD_PORT_* offset over scoped list
# ---------------------------------------------------------------------------


class TestPortOffsetScopedList:
    """WSD_PORT_* offsets are numbered over the project-only list."""

    def test_workspace_service_does_not_consume_port_offset(self, tmp_path: Path) -> None:
        """Workspace service 'db' does not shift offsets for project services.

        Manifest: [backend (project), db (workspace), api (project)]
        Expected per-env port assignment over project-only list [backend, api]:
            WSD_PORT_BACKEND = port_base + 0
            WSD_PORT_API     = port_base + 1
        db must not consume offset slot 1 (which would shift api to +2).
        """
        manifest = DockerManifest(
            project_prefix="myapp",
            environment_compose_file="compose.yaml",
            workspace_compose_file="compose.yaml",
            services=(ServiceDecl("backend"), ServiceDecl("api")),
            workspace_services=(ServiceDecl("db"),),
        )
        ws = _alpha_workspace(tmp_path)
        ctx = build_env_context("alpha", "myapp", ws)  # port_base=4020
        scoped = manifest.services_for_scope("alpha")
        env = _build_compose_env(ctx, scoped)

        assert env["WSD_PORT_BACKEND"] == "4020"
        assert env["WSD_PORT_API"] == "4021"
        assert "WSD_PORT_DB" not in env, "db is workspace-scoped; must not get per-env port"

    def test_port_offset_with_multiple_workspace_services(self, tmp_path: Path) -> None:
        """Multiple workspace services don't affect project port numbering."""
        manifest = DockerManifest(
            project_prefix="myapp",
            environment_compose_file="compose.yaml",
            workspace_compose_file="compose.yaml",
            services=(ServiceDecl("frontend"),),
            workspace_services=(ServiceDecl("db"), ServiceDecl("redis")),
        )
        ws = _alpha_workspace(tmp_path)
        ctx = build_env_context("alpha", "myapp", ws)
        scoped = manifest.services_for_scope("alpha")
        env = _build_compose_env(ctx, scoped)

        # Only frontend gets a port; workspace services are absent
        assert env["WSD_PORT_FRONTEND"] == "4020"
        assert "WSD_PORT_DB" not in env
        assert "WSD_PORT_REDIS" not in env

    def test_workspace_services_for_scope_has_correct_count(self) -> None:
        """services_for_scope("alpha") returns the project list, not the combined list."""
        manifest = DockerManifest(
            project_prefix="myapp",
            environment_compose_file="compose.yaml",
            workspace_compose_file="compose.yaml",
            services=(ServiceDecl("a"), ServiceDecl("b")),
            workspace_services=(ServiceDecl("c"), ServiceDecl("d"), ServiceDecl("e")),
        )
        project_svcs = manifest.services_for_scope("alpha")
        ws_svcs = manifest.services_for_scope("workspace")
        assert len(project_svcs) == 2
        assert len(ws_svcs) == 3


# ---------------------------------------------------------------------------
# 4. status/restart/logs scope-correctness
# ---------------------------------------------------------------------------


class TestStatusScopeCorrectness:
    """status alpha shows project services only; status workspace shows workspace only."""

    def test_status_alpha_queries_project_project(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """status alpha queries compose project myapp-alpha (not myapp-workspace)."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        ws = _alpha_workspace(tmp_path)
        client = FakeComposeClient(compose_results=[_ps_result([_running_container("backend")])])
        cmd_status(["alpha"], manifest, ws, client)
        call = client.compose_calls[0]
        assert call.project == "myapp-alpha"

    def test_status_alpha_shows_only_project_services(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """status alpha lists backend (project) but NOT db (workspace)."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        ws = _alpha_workspace(tmp_path)
        client = FakeComposeClient(compose_results=[_ps_result([_running_container("backend")])])
        cmd_status(["alpha"], manifest, ws, client)
        doc = json.loads(capsys.readouterr().out)
        svc_names = [s["name"] for s in doc["envs"][0]["services"]]
        assert "backend" in svc_names
        assert "db" not in svc_names, "db is workspace-scoped; must not appear in per-env status"

    def test_status_workspace_shows_only_workspace_services(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """status workspace lists db (workspace) but NOT backend (project)."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        client = FakeComposeClient(compose_results=[_ps_result([_running_container("db", project="myapp-workspace")])])
        cmd_status(["workspace"], manifest, tmp_path, client)
        doc = json.loads(capsys.readouterr().out)
        svc_names = [s["name"] for s in doc["envs"][0]["services"]]
        assert "db" in svc_names
        assert "backend" not in svc_names, "backend is project-scoped; must not appear in workspace status"

    def test_status_workspace_queries_workspace_project(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """status workspace queries compose project myapp-workspace."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        client = FakeComposeClient(compose_results=[_ps_result([])])
        cmd_status(["workspace"], manifest, tmp_path, client)
        call = client.compose_calls[0]
        assert call.project == "myapp-workspace"


class TestRestartScopeCorrectness:
    """restart workspace/backend and restart alpha/db match nothing (wrong scope)."""

    def test_restart_workspace_project_service_no_match(self) -> None:
        """restart workspace/backend returns 1: backend is project-scoped, not in workspace."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        # backend is not in workspace_services; pattern workspace/backend finds nothing
        targets = _collect_restart_targets(["workspace/backend"], manifest)
        assert targets == [], "backend is project-scoped; restarting it under workspace scope must yield no targets"

    def test_restart_alpha_workspace_service_no_match(self) -> None:
        """restart alpha/db returns 1: db is workspace-scoped, not in alpha."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        # db is not in services (project); pattern alpha/db finds nothing
        targets = _collect_restart_targets(["alpha/db"], manifest)
        assert targets == [], "db is workspace-scoped; restarting it under alpha scope must yield no targets"

    def test_restart_workspace_db_correct_scope_finds_target(self, tmp_path: Path) -> None:
        """restart workspace/db finds db — db is workspace-scoped, pattern is correct."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        targets = _collect_restart_targets(["workspace/db"], manifest)
        assert targets == [("workspace", "db")]

    def test_restart_alpha_backend_correct_scope_finds_target(self, tmp_path: Path) -> None:
        """restart alpha/backend finds backend — backend is project-scoped, pattern is correct."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        targets = _collect_restart_targets(["alpha/backend"], manifest)
        assert targets == [("alpha", "backend")]

    def test_cmd_restart_workspace_project_service_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """cmd_restart workspace/backend emits diagnostic and returns 1."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        client = FakeComposeClient()
        rc = cmd_restart(["workspace/backend"], manifest, tmp_path, client)
        assert rc == 1
        assert client.compose_calls == []

    def test_cmd_restart_alpha_workspace_service_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """cmd_restart alpha/db emits diagnostic and returns 1."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        client = FakeComposeClient()
        rc = cmd_restart(["alpha/db"], manifest, tmp_path, client)
        assert rc == 1
        assert client.compose_calls == []


class TestLogsScopeCorrectness:
    """logs scope: workspace/backend and alpha/db find nothing (wrong scope)."""

    def test_collect_log_targets_workspace_project_service_empty(self, tmp_path: Path) -> None:
        """logs workspace/backend: backend is project-scoped → no targets."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        targets = _collect_log_targets(["workspace/backend"], manifest)
        assert targets == []

    def test_collect_log_targets_alpha_workspace_service_empty(self, tmp_path: Path) -> None:
        """logs alpha/db: db is workspace-scoped → no targets."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        targets = _collect_log_targets(["alpha/db"], manifest)
        assert targets == []

    def test_collect_log_targets_workspace_db_correct_scope(self, tmp_path: Path) -> None:
        """logs workspace/db: db is workspace-scoped → target found."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        targets = _collect_log_targets(["workspace/db"], manifest)
        assert targets == [("workspace", "db")]

    def test_collect_log_targets_alpha_backend_correct_scope(self, tmp_path: Path) -> None:
        """logs alpha/backend: backend is project-scoped → target found."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        targets = _collect_log_targets(["alpha/backend"], manifest)
        assert targets == [("alpha", "backend")]

    def test_cmd_logs_alpha_workspace_service_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """cmd_logs alpha/db: no targets → returns 1 with diagnostic."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        client = FakeComposeClient()
        sink = StringIO()
        rc = cmd_logs(["alpha/db"], manifest, tmp_path, client, sink=sink)
        assert rc == 1
        assert client.compose_calls == []

    def test_cmd_logs_workspace_backend_returns_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """cmd_logs workspace/backend: no targets → returns 1 with diagnostic."""
        manifest = _mixed_manifest(project_services=["backend"], workspace_services=["db"])
        client = FakeComposeClient()
        sink = StringIO()
        rc = cmd_logs(["workspace/backend"], manifest, tmp_path, client, sink=sink)
        assert rc == 1
        assert client.compose_calls == []


# ---------------------------------------------------------------------------
# 5. describe enumerates both scopes
# ---------------------------------------------------------------------------


class TestDescribeBothScopes:
    """describe emits names from both project and workspace partitions."""

    def test_describe_lists_project_and_workspace_services(
        self, config_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """describe with mixed scope manifest emits names from both partitions."""
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\nenvironment_compose_file = "compose.yaml"\nworkspace_compose_file = "workspace-compose.yaml"\n'
            '[[service]]\nname = "backend"\n'
            '[[service]]\nname = "db"\nscope = "workspace"\n',
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"WINTER_EXT_CONFIG_DIR": str(config_dir)}):
            rc = cli_main(["describe"])

        assert rc == 0
        captured = capsys.readouterr()
        doc = json.loads(captured.out)
        assert set(doc["services"]) == {"backend", "db"}

    def test_describe_workspace_only_manifest(self, config_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """describe with only workspace-scoped services lists those names."""
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\nenvironment_compose_file = "compose.yaml"\nworkspace_compose_file = "workspace-compose.yaml"\n'
            '[[service]]\nname = "db"\nscope = "workspace"\n'
            '[[service]]\nname = "redis"\nscope = "workspace"\n',
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"WINTER_EXT_CONFIG_DIR": str(config_dir)}):
            rc = cli_main(["describe"])

        assert rc == 0
        doc = json.loads(capsys.readouterr().out)
        assert set(doc["services"]) == {"db", "redis"}

    def test_describe_order_project_then_workspace(self, config_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """describe emits project services first, then workspace services."""
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\nenvironment_compose_file = "compose.yaml"\nworkspace_compose_file = "workspace-compose.yaml"\n'
            '[[service]]\nname = "api"\n'
            '[[service]]\nname = "db"\nscope = "workspace"\n',
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"WINTER_EXT_CONFIG_DIR": str(config_dir)}):
            cli_main(["describe"])

        doc = json.loads(capsys.readouterr().out)
        services = doc["services"]
        # api (project) should appear before db (workspace)
        assert services.index("api") < services.index("db")


# ---------------------------------------------------------------------------
# 6. Default-scope back-compat
# ---------------------------------------------------------------------------


class TestDefaultScopeBackCompat:
    """A manifest with no scope field behaves exactly as before the feature."""

    def test_no_scope_field_all_services_are_project(self, config_dir: Path) -> None:
        """Without any scope= fields, every [[service]] is project-scoped."""
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\nenvironment_compose_file = "compose.yaml"\nworkspace_compose_file = "workspace-compose.yaml"\n'
            '[[service]]\nname = "db"\n'
            '[[service]]\nname = "api"\n',
            encoding="utf-8",
        )
        manifest = load_manifest(config_dir)
        assert len(manifest.services) == 2
        assert manifest.workspace_services == ()

    def test_no_scope_field_services_for_scope_alpha(self, config_dir: Path) -> None:
        """services_for_scope("alpha") returns all services when no scope field."""
        (config_dir / "config.toml").write_text(
            'project_prefix = "myapp"\nenvironment_compose_file = "compose.yaml"\nworkspace_compose_file = "workspace-compose.yaml"\n'
            '[[service]]\nname = "db"\n'
            '[[service]]\nname = "api"\n',
            encoding="utf-8",
        )
        manifest = load_manifest(config_dir)
        result = manifest.services_for_scope("alpha")
        assert len(result) == 2
        assert {s.name for s in result} == {"db", "api"}

    def test_no_scope_field_up_alpha_includes_all_services(self, tmp_path: Path) -> None:
        """Without scope, per-env up gets WSD_PORT_* for all services."""
        manifest = _project_only_manifest(services=["db", "api"])
        ws = _alpha_workspace(tmp_path)
        ctx = build_env_context("alpha", "myapp", ws)
        scoped = manifest.services_for_scope("alpha")
        env = _build_compose_env(ctx, scoped)

        assert "WSD_PORT_DB" in env
        assert "WSD_PORT_API" in env

    def test_no_scope_field_port_offsets_unchanged(self, tmp_path: Path) -> None:
        """Without scope, port offsets are 0-based over the full service list."""
        manifest = _project_only_manifest(services=["db", "api", "worker"])
        ws = _alpha_workspace(tmp_path)
        ctx = build_env_context("alpha", "myapp", ws)
        scoped = manifest.services_for_scope("alpha")
        env = _build_compose_env(ctx, scoped)

        # port_base=4020 from fixture
        assert env["WSD_PORT_DB"] == "4020"
        assert env["WSD_PORT_API"] == "4021"
        assert env["WSD_PORT_WORKER"] == "4022"

    def test_no_scope_field_workspace_services_is_empty(self) -> None:
        """DockerManifest constructed without workspace_services defaults to empty tuple."""
        # This tests the back-compat path of constructing DockerManifest directly
        # (or loading a config.toml with no scope= entries).
        manifest = DockerManifest(
            project_prefix="myapp",
            environment_compose_file="compose.yaml",
            workspace_compose_file="compose.yaml",
            services=(ServiceDecl("db"), ServiceDecl("api")),
        )
        assert manifest.workspace_services == ()

    def test_no_scope_field_restart_alpha_finds_all_services(self) -> None:
        """With no scope, restart alpha/* matches all project services."""
        manifest = _project_only_manifest(services=["db", "api"])
        targets = _collect_restart_targets(["alpha/*"], manifest)
        svc_names = [t[1] for t in targets]
        assert set(svc_names) == {"db", "api"}

    def test_no_scope_field_status_alpha_shows_all_services(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With no scope, status alpha shows all declared services."""
        manifest = _project_only_manifest(services=["db", "api"])
        ws = _alpha_workspace(tmp_path)
        client = FakeComposeClient(
            compose_results=[
                _ps_result(
                    [
                        _running_container("db"),
                        _running_container("api"),
                    ]
                )
            ]
        )
        cmd_status(["alpha"], manifest, ws, client)
        doc = json.loads(capsys.readouterr().out)
        svc_names = {s["name"] for s in doc["envs"][0]["services"]}
        assert svc_names == {"db", "api"}
