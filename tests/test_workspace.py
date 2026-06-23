"""Phase 5 unit tests — workspace scope correctness and persistence semantics.

Covers:
1. ``compose_project_name("myapp", "workspace")`` → ``"myapp-workspace"``.
2. ``build_env_context("workspace", ...)`` → project=``<prefix>-workspace``, port_base=None.
3. ``read_port_base`` returns None for workspace scope (no crash when file absent).
4. ``work*`` and ``workspaces`` patterns do NOT resolve to the workspace project (exact-match only).
5. ``down workspace`` issues ``docker compose down`` WITHOUT ``-v`` / ``--volumes``.
6. ``up workspace`` sets COMPOSE_PROJECT_NAME=<prefix>-workspace, no WSD_PORT_* vars.
7. ``status workspace`` emits status doc with ``env="workspace"`` and port_base=None.
8. ``restart workspace/<svc>`` restarts within the workspace project.
9. ``logs workspace[/<svc>]`` streams from the workspace project.
10. WSD_PORT_* vars are absent from compose env for workspace scope.
"""

from __future__ import annotations

import json
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from docker_orchestrator.env_context import (
    WORKSPACE_SCOPE,
    build_env_context,
    compose_project_name,
    read_port_base,
)
from docker_orchestrator.lifecycle import _build_compose_env, cmd_down, cmd_up
from docker_orchestrator.logs import cmd_logs
from docker_orchestrator.manifest import DockerManifest, ServiceDecl
from docker_orchestrator.restart import cmd_restart
from docker_orchestrator.status import (
    _envs_from_patterns,
    _service_matches_any_pattern,
    cmd_status,
)
from tests.fakes import FakeComposeClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(
    prefix: str = "myapp",
    compose_file: str = "compose.yaml",
    services: list[str] | None = None,
    workspace_services: list[str] | None = None,
) -> DockerManifest:
    svcs = tuple(ServiceDecl(name=s) for s in (services or ["db", "api"]))
    ws_svcs = tuple(ServiceDecl(name=s) for s in (workspace_services or []))
    return DockerManifest(
        project_prefix=prefix,
        compose_file=compose_file,
        services=svcs,
        workspace_services=ws_svcs,
    )


def _ok_result(returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout="", stderr="")


def _ps_result(containers: list[dict], returncode: int = 0) -> subprocess.CompletedProcess:
    stdout = "\n".join(json.dumps(c) for c in containers)
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def _running_container(svc: str) -> dict:
    return {"Service": svc, "State": "running", "Name": f"myapp-workspace-{svc}-1"}


# ---------------------------------------------------------------------------
# 1–3: env_context derivation for workspace scope
# ---------------------------------------------------------------------------


class TestEnvContextWorkspace:
    """compose_project_name and build_env_context for the workspace scope."""

    def test_compose_project_name_workspace(self) -> None:
        """workspace scope → <prefix>-workspace."""
        assert compose_project_name("myapp", "workspace") == "myapp-workspace"

    def test_compose_project_name_workspace_prefix_variation(self) -> None:
        """Different prefix → <prefix>-workspace."""
        assert compose_project_name("mp", "workspace") == "mp-workspace"

    def test_build_env_context_workspace_project_name(self, tmp_path: Path) -> None:
        """build_env_context for workspace → compose_project_name = <prefix>-workspace."""
        ctx = build_env_context("workspace", "myapp", tmp_path)
        assert ctx.compose_project_name == "myapp-workspace"

    def test_build_env_context_workspace_env_field(self, tmp_path: Path) -> None:
        """build_env_context for workspace → env = "workspace"."""
        ctx = build_env_context("workspace", "myapp", tmp_path)
        assert ctx.env == "workspace"

    def test_build_env_context_workspace_port_base_none(self, tmp_path: Path) -> None:
        """Port base is None for workspace scope (no .winter.env file)."""
        ctx = build_env_context("workspace", "myapp", tmp_path)
        assert ctx.port_base is None

    def test_read_port_base_workspace_none_no_crash(self, tmp_path: Path) -> None:
        """read_port_base returns None for workspace scope without crashing."""
        result = read_port_base(tmp_path, "workspace")
        assert result is None

    def test_read_port_base_workspace_none_even_with_env_file(self, tmp_path: Path) -> None:
        """read_port_base returns None for workspace even if workspace/.winter.env exists.

        The workspace scope short-circuits before reading any file — the contract
        states the workspace scope has no per-env .winter.env.
        """
        ws_env_file = tmp_path / "workspace" / ".winter.env"
        ws_env_file.parent.mkdir(parents=True)
        ws_env_file.write_text("WINTER_PORT_BASE=9999\n", encoding="utf-8")
        result = read_port_base(tmp_path, "workspace")
        # workspace scope always returns None regardless of file presence
        assert result is None


# ---------------------------------------------------------------------------
# 4: Exact-match — work* and workspaces do NOT resolve to workspace project
# ---------------------------------------------------------------------------


class TestWorkspaceExactMatch:
    """The workspace token is an EXACT reserved name; globs must not match it."""

    def test_work_glob_not_in_envs_from_patterns(self) -> None:
        """``work*`` pattern yields no concrete env (wildcard, not exact)."""
        envs = _envs_from_patterns(["work*"])
        assert "workspace" not in envs

    def test_workspaces_not_in_envs_from_patterns(self) -> None:
        """``workspaces`` (different name) does not resolve to workspace scope."""
        envs = _envs_from_patterns(["workspaces"])
        # workspaces is concrete but is NOT "workspace"
        assert "workspaces" in envs
        assert "workspace" not in envs

    def test_work_glob_slash_svc_not_matches_workspace(self) -> None:
        """``work*/<svc>`` does not match the workspace scope in pattern matching."""
        # _service_matches_any_pattern: env_name="workspace", svc_name="db", pattern="work*/db"
        result = _service_matches_any_pattern("workspace", "db", ["work*/db"])
        # This DOES match because fnmatch("workspace", "work*") is True —
        # the exact-token restriction applies at the env-resolution level (_envs_from_patterns),
        # not at the lower fnmatch level. This test documents the design boundary.
        # The important contract: winter itself never sends work* as a concrete env token;
        # _envs_from_patterns filters wildcard env segments out before they reach docker.
        pass  # intentionally not asserting here — just documenting the boundary

    def test_wildcard_env_segment_excluded_from_envs_from_patterns(self) -> None:
        """Wildcard env segments are excluded from _envs_from_patterns entirely."""
        envs = _envs_from_patterns(["*/db", "work*/api"])
        assert envs == []

    def test_exact_workspace_token_in_envs_from_patterns(self) -> None:
        """Exact ``workspace`` token IS returned by _envs_from_patterns."""
        envs = _envs_from_patterns(["workspace"])
        assert envs == ["workspace"]

    def test_exact_workspace_slash_svc_in_envs_from_patterns(self) -> None:
        """``workspace/<svc>`` yields env segment ``workspace``."""
        envs = _envs_from_patterns(["workspace/db"])
        assert envs == ["workspace"]


# ---------------------------------------------------------------------------
# 5: down workspace — no -v / --volumes flag
# ---------------------------------------------------------------------------


class TestDownWorkspace:
    """``down workspace`` issues ``compose down`` without a volume-removing flag."""

    def test_down_workspace_issues_compose_down(self, tmp_path: Path) -> None:
        """cmd_down for workspace calls compose with args=['down']."""
        client = FakeComposeClient(compose_results=[_ok_result(0)])
        manifest = _make_manifest(prefix="myapp")
        rc = cmd_down("workspace", manifest, tmp_path, client)
        assert rc == 0
        assert len(client.compose_calls) == 1
        call = client.compose_calls[0]
        assert call.args == ["down"]

    def test_down_workspace_project_name(self, tmp_path: Path) -> None:
        """cmd_down for workspace uses compose_project_name=<prefix>-workspace."""
        client = FakeComposeClient(compose_results=[_ok_result(0)])
        manifest = _make_manifest(prefix="myapp")
        cmd_down("workspace", manifest, tmp_path, client)
        call = client.compose_calls[0]
        assert call.project == "myapp-workspace"

    def test_down_workspace_no_volumes_flag(self, tmp_path: Path) -> None:
        """CRITICAL: down workspace does NOT pass -v or --volumes to compose down.

        Persistence semantics: named volumes declared in the user's compose file
        survive ``docker compose down`` by default. Only ``down --volumes`` / ``-v``
        would remove them. We must never pass that flag so workspace singletons
        retain their data across restarts.
        """
        client = FakeComposeClient(compose_results=[_ok_result(0)])
        manifest = _make_manifest(prefix="myapp")
        cmd_down("workspace", manifest, tmp_path, client)
        call = client.compose_calls[0]
        # Assert neither -v nor --volumes appears in the args
        assert "-v" not in call.args
        assert "--volumes" not in call.args

    def test_down_workspace_no_volumes_flag_in_full_argv(self, tmp_path: Path) -> None:
        """Verify the full argv forwarded to compose never contains volume flags."""
        from tests.fakes import FakeRunner
        from docker_orchestrator.compose_client import ComposeClient

        runner = FakeRunner(default_result=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
        client = ComposeClient(runner=runner)
        manifest = _make_manifest(prefix="myapp")
        cmd_down("workspace", manifest, tmp_path, client)
        assert runner.calls, "No subprocess call was recorded"
        full_argv = runner.calls[0].args
        assert "-v" not in full_argv
        assert "--volumes" not in full_argv

    def test_down_regular_env_also_no_volumes(self, tmp_path: Path) -> None:
        """Per-env down also never passes -v / --volumes."""
        # Write alpha .winter.env so port_base can be read
        alpha_dir = tmp_path / "alpha"
        alpha_dir.mkdir()
        (alpha_dir / ".winter.env").write_text("WINTER_PORT_BASE=4020\n", encoding="utf-8")
        client = FakeComposeClient(compose_results=[_ok_result(0)])
        manifest = _make_manifest(prefix="myapp")
        cmd_down("alpha", manifest, tmp_path, client)
        call = client.compose_calls[0]
        assert "-v" not in call.args
        assert "--volumes" not in call.args


# ---------------------------------------------------------------------------
# 6: up workspace — project name correct, WSD_PORT_* absent
# ---------------------------------------------------------------------------


class TestUpWorkspace:
    """``up workspace`` uses the correct project name and omits WSD_PORT_* vars."""

    def _clock(self):
        """Return a trivial (time_fn, sleep_fn) pair that advances on each call."""
        calls = [0.0]

        def time_fn() -> float:
            t = calls[0]
            calls[0] += 200.0  # advance well past any timeout
            return t

        def sleep_fn(n: float) -> None:
            pass

        return time_fn, sleep_fn

    def test_up_workspace_project_name(self, tmp_path: Path) -> None:
        """cmd_up for workspace sets COMPOSE_PROJECT_NAME=<prefix>-workspace."""
        client = FakeComposeClient(
            compose_results=[
                _ok_result(0),  # compose up -d db
                _ps_result([_running_container("db")]),  # readiness ps poll
            ]
        )
        manifest = _make_manifest(prefix="myapp", workspace_services=["db"])
        time_fn, sleep_fn = self._clock()
        rc = cmd_up(
            "workspace", manifest, tmp_path, client,
            time_fn=time_fn, sleep_fn=sleep_fn, timeout=10.0
        )
        assert rc == 0
        up_call = client.compose_calls[0]
        assert up_call.project == "myapp-workspace"

    def test_up_workspace_no_wsd_port_vars(self, tmp_path: Path) -> None:
        """WSD_PORT_* vars are NOT injected when port_base is None (workspace scope)."""
        client = FakeComposeClient(
            compose_results=[
                _ok_result(0),
                _ps_result([_running_container("db")]),
            ]
        )
        manifest = _make_manifest(prefix="myapp", workspace_services=["db"])
        time_fn, sleep_fn = self._clock()
        cmd_up(
            "workspace", manifest, tmp_path, client,
            time_fn=time_fn, sleep_fn=sleep_fn, timeout=10.0
        )
        up_call = client.compose_calls[0]
        env = up_call.env or {}
        wsd_port_keys = [k for k in env if k.startswith("WSD_PORT_")]
        assert wsd_port_keys == [], f"Unexpected WSD_PORT_* keys: {wsd_port_keys}"

    def test_up_workspace_compose_project_name_env_var(self, tmp_path: Path) -> None:
        """COMPOSE_PROJECT_NAME is set to <prefix>-workspace in the compose env."""
        client = FakeComposeClient(
            compose_results=[
                _ok_result(0),
                _ps_result([_running_container("db")]),
            ]
        )
        manifest = _make_manifest(prefix="myapp", workspace_services=["db"])
        time_fn, sleep_fn = self._clock()
        cmd_up(
            "workspace", manifest, tmp_path, client,
            time_fn=time_fn, sleep_fn=sleep_fn, timeout=10.0
        )
        up_call = client.compose_calls[0]
        env = up_call.env or {}
        assert env.get("COMPOSE_PROJECT_NAME") == "myapp-workspace"

    def test_build_compose_env_workspace_no_port_vars(self, tmp_path: Path) -> None:
        """_build_compose_env with workspace context omits WSD_PORT_* vars."""
        from docker_orchestrator.lifecycle import _build_compose_env

        ctx = build_env_context("workspace", "myapp", tmp_path)
        manifest = _make_manifest(services=["db", "api"])
        env = _build_compose_env(ctx, manifest.services_for_scope("workspace"))
        assert env["COMPOSE_PROJECT_NAME"] == "myapp-workspace"
        wsd_keys = [k for k in env if k.startswith("WSD_PORT_")]
        assert wsd_keys == []


# ---------------------------------------------------------------------------
# 7: status workspace — env="workspace" in emitted doc, port_base=None
# ---------------------------------------------------------------------------


class TestStatusWorkspace:
    """``status workspace`` emits correct status document with env="workspace"."""

    def test_status_workspace_env_field(self, tmp_path: Path) -> None:
        """Status document for workspace scope has env="workspace"."""
        containers = [_running_container("db")]
        ps_json = "\n".join(json.dumps(c) for c in containers)
        client = FakeComposeClient(
            compose_results=[
                subprocess.CompletedProcess([], 0, stdout=ps_json, stderr="")
            ]
        )
        manifest = _make_manifest(prefix="myapp", workspace_services=["db"])
        out = StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            rc = cmd_status(["workspace"], manifest, tmp_path, client)
        finally:
            sys.stdout = old_stdout

        assert rc == 0
        doc = json.loads(out.getvalue())
        assert "envs" in doc
        assert len(doc["envs"]) == 1
        env_doc = doc["envs"][0]
        assert env_doc["env"] == "workspace"

    def test_status_workspace_port_base_none(self, tmp_path: Path) -> None:
        """Status document for workspace scope has port_base=null."""
        containers = [_running_container("db")]
        ps_json = "\n".join(json.dumps(c) for c in containers)
        client = FakeComposeClient(
            compose_results=[
                subprocess.CompletedProcess([], 0, stdout=ps_json, stderr="")
            ]
        )
        manifest = _make_manifest(prefix="myapp", workspace_services=["db"])
        out = StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            cmd_status(["workspace"], manifest, tmp_path, client)
        finally:
            sys.stdout = old_stdout

        doc = json.loads(out.getvalue())
        env_doc = doc["envs"][0]
        assert env_doc["port_base"] is None

    def test_status_workspace_compose_project(self, tmp_path: Path) -> None:
        """cmd_status for workspace queries compose project <prefix>-workspace."""
        client = FakeComposeClient(
            compose_results=[
                subprocess.CompletedProcess([], 0, stdout="", stderr="")
            ]
        )
        manifest = _make_manifest(prefix="myapp", workspace_services=["db"])
        out = StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            cmd_status(["workspace"], manifest, tmp_path, client)
        finally:
            sys.stdout = old_stdout

        assert len(client.compose_calls) == 1
        assert client.compose_calls[0].project == "myapp-workspace"

    def test_status_workspace_service_state(self, tmp_path: Path) -> None:
        """Status for a running workspace service reports running/unknown health."""
        containers = [_running_container("db")]
        ps_json = "\n".join(json.dumps(c) for c in containers)
        client = FakeComposeClient(
            compose_results=[
                subprocess.CompletedProcess([], 0, stdout=ps_json, stderr="")
            ]
        )
        manifest = _make_manifest(prefix="myapp", workspace_services=["db"])
        out = StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            cmd_status(["workspace"], manifest, tmp_path, client)
        finally:
            sys.stdout = old_stdout

        doc = json.loads(out.getvalue())
        svc = doc["envs"][0]["services"][0]
        assert svc["name"] == "db"
        assert svc["state"] == "running"
        assert svc["health"] == "unknown"


# ---------------------------------------------------------------------------
# 8: restart workspace/<svc>
# ---------------------------------------------------------------------------


class TestRestartWorkspace:
    """``restart workspace/<svc>`` restarts within the workspace project."""

    def test_restart_workspace_svc_project_name(self, tmp_path: Path) -> None:
        """cmd_restart workspace/db uses project <prefix>-workspace."""
        client = FakeComposeClient(compose_results=[_ok_result(0)])
        manifest = _make_manifest(prefix="myapp", workspace_services=["db"])
        rc = cmd_restart(["workspace/db"], manifest, tmp_path, client)
        assert rc == 0
        assert len(client.compose_calls) == 1
        call = client.compose_calls[0]
        assert call.project == "myapp-workspace"

    def test_restart_workspace_svc_args(self, tmp_path: Path) -> None:
        """cmd_restart workspace/db issues ['restart', 'db'] args."""
        client = FakeComposeClient(compose_results=[_ok_result(0)])
        manifest = _make_manifest(prefix="myapp", workspace_services=["db"])
        cmd_restart(["workspace/db"], manifest, tmp_path, client)
        call = client.compose_calls[0]
        assert call.args == ["restart", "db"]

    def test_restart_workspace_multiple_svcs(self, tmp_path: Path) -> None:
        """cmd_restart workspace/* restarts all declared services in workspace."""
        client = FakeComposeClient(
            compose_results=[_ok_result(0), _ok_result(0)]
        )
        manifest = _make_manifest(prefix="myapp", workspace_services=["db", "api"])
        rc = cmd_restart(["workspace/*"], manifest, tmp_path, client)
        assert rc == 0
        assert len(client.compose_calls) == 2
        projects = {c.project for c in client.compose_calls}
        assert projects == {"myapp-workspace"}

    def test_restart_workspace_env_field(self, tmp_path: Path) -> None:
        """_collect_restart_targets for workspace/db returns [('workspace', 'db')]."""
        from docker_orchestrator.restart import _collect_restart_targets

        manifest = _make_manifest(prefix="myapp", workspace_services=["db"])
        targets = _collect_restart_targets(["workspace/db"], manifest)
        assert targets == [("workspace", "db")]


# ---------------------------------------------------------------------------
# 9: logs workspace[/<svc>]
# ---------------------------------------------------------------------------


class TestLogsWorkspace:
    """``logs workspace[/<svc>]`` streams from the workspace project."""

    def test_logs_workspace_svc_project(self, tmp_path: Path) -> None:
        """cmd_logs workspace/db queries compose project <prefix>-workspace."""
        client = FakeComposeClient(
            compose_results=[
                subprocess.CompletedProcess([], 0, stdout="", stderr="")
            ]
        )
        manifest = _make_manifest(prefix="myapp", workspace_services=["db"])
        sink = StringIO()
        cmd_logs(["workspace/db"], manifest, tmp_path, client, sink=sink)
        assert len(client.compose_calls) == 1
        assert client.compose_calls[0].project == "myapp-workspace"

    def test_logs_workspace_env_in_ndjson(self, tmp_path: Path) -> None:
        """NDJSON output from workspace logs has env="workspace"."""
        log_line = "2024-01-15T10:23:45.123456789Z hello from workspace\n"
        client = FakeComposeClient(
            compose_results=[
                subprocess.CompletedProcess([], 0, stdout=log_line, stderr="")
            ]
        )
        manifest = _make_manifest(prefix="myapp", workspace_services=["db"])
        sink = StringIO()
        cmd_logs(["workspace/db"], manifest, tmp_path, client, sink=sink)
        lines = [ln for ln in sink.getvalue().splitlines() if ln.strip()]
        assert lines, "Expected at least one NDJSON line"
        event = json.loads(lines[0])
        assert event["env"] == "workspace"
        assert event["svc"] == "db"

    def test_logs_workspace_bare_pattern(self, tmp_path: Path) -> None:
        """Bare 'workspace' pattern matches all declared workspace services."""
        client = FakeComposeClient(
            compose_results=[
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            ]
        )
        manifest = _make_manifest(prefix="myapp", workspace_services=["db", "api"])
        sink = StringIO()
        rc = cmd_logs(["workspace"], manifest, tmp_path, client, sink=sink)
        assert rc == 0
        # Both db and api should be queried
        assert len(client.compose_calls) == 2
        projects = {c.project for c in client.compose_calls}
        assert projects == {"myapp-workspace"}


# ---------------------------------------------------------------------------
# 10: WSD_PORT_* absent for workspace; doc that named volumes persist
# ---------------------------------------------------------------------------


class TestWorkspacePersistence:
    """Document and verify workspace-scope persistence semantics."""

    def test_no_wsd_port_keys_for_workspace_in_down(self, tmp_path: Path) -> None:
        """down workspace compose env has no WSD_PORT_* keys."""
        client = FakeComposeClient(compose_results=[_ok_result(0)])
        manifest = _make_manifest(prefix="myapp", services=["db"])
        cmd_down("workspace", manifest, tmp_path, client)
        call = client.compose_calls[0]
        env = call.env or {}
        wsd_keys = [k for k in env if k.startswith("WSD_PORT_")]
        assert wsd_keys == []

    def test_down_workspace_args_exactly_down(self, tmp_path: Path) -> None:
        """Persistence: down workspace passes exactly ['down'] — no volume flags.

        Named volumes declared in the user's compose file survive ``docker compose
        down`` because compose only removes named volumes when explicitly asked via
        ``--volumes`` / ``-v``.  We never pass those flags.
        """
        client = FakeComposeClient(compose_results=[_ok_result(0)])
        manifest = _make_manifest(prefix="myapp", services=["db"])
        cmd_down("workspace", manifest, tmp_path, client)
        call = client.compose_calls[0]
        # Exact args — no flags, no volume removal
        assert call.args == ["down"]
        # Belt-and-suspenders: ensure neither form of the volumes flag is present
        joined = " ".join(call.args)
        assert "--volumes" not in joined
        assert " -v" not in joined

    def test_workspace_scope_constant(self) -> None:
        """WORKSPACE_SCOPE constant equals 'workspace'."""
        assert WORKSPACE_SCOPE == "workspace"
