"""Phase 4 unit tests for the ``restart`` command implementation.

Covers:
1. Pattern → service matching: <env>/<svc> picks exactly the right service.
2. Correct ``compose restart <svc>`` argv per env with the right project name.
3. COMPOSE_PROJECT_NAME is derived from ``<prefix>-<env>``.
4. Worst-exit aggregation across multiple matched (env, svc) pairs.
5. Bare ``<env>`` token (no '/') with no matching services: actionable stderr.
6. No-match pattern emits diagnostic and returns 1.
7. Missing manifest fields emits diagnostic and returns 1.
8. Empty patterns emits diagnostic and returns 1.
9. CLI dispatch: ``restart alpha/db`` issues the right compose call.
10. CLI dispatch: ``restart`` without args exits non-2 (not unknown-action).
11. Wildcard env pattern with no concrete env: emits diagnostic and returns 1.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from docker_orchestrator.cli import main as cli_main
from docker_orchestrator.manifest import DockerManifest, ServiceDecl
from docker_orchestrator.restart import _collect_restart_targets, cmd_restart
from tests.fakes import FakeComposeClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(
    prefix: str = "myapp",
    compose_file: str = "compose.yaml",
    services: list[str] | None = None,
) -> DockerManifest:
    svcs = tuple(ServiceDecl(name=s) for s in (services or ["db", "api"]))
    return DockerManifest(
        project_prefix=prefix,
        environment_compose_file=compose_file,
        workspace_compose_file=compose_file,
        services=svcs,
    )


def _ok_result(returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout="", stderr="")


# ---------------------------------------------------------------------------
# 1. Pattern → service matching
# ---------------------------------------------------------------------------


def test_collect_restart_targets_exact_match() -> None:
    manifest = _make_manifest(services=["db", "api"])
    targets = _collect_restart_targets(["alpha/db"], manifest)
    assert targets == [("alpha", "db")]


def test_collect_restart_targets_wildcard_service() -> None:
    manifest = _make_manifest(services=["db", "api", "worker"])
    targets = _collect_restart_targets(["alpha/*"], manifest)
    assert targets == [("alpha", "db"), ("alpha", "api"), ("alpha", "worker")]


def test_collect_restart_targets_multi_env_pattern() -> None:
    manifest = _make_manifest(services=["db"])
    targets = _collect_restart_targets(["alpha/db", "beta/db"], manifest)
    assert targets == [("alpha", "db"), ("beta", "db")]


def test_collect_restart_targets_no_match() -> None:
    manifest = _make_manifest(services=["db", "api"])
    targets = _collect_restart_targets(["alpha/notexist"], manifest)
    assert targets == []


def test_collect_restart_targets_bare_env_matches_all_svcs() -> None:
    """A bare token (no '/') is treated as an env name matching all services."""
    manifest = _make_manifest(services=["db", "api"])
    targets = _collect_restart_targets(["alpha"], manifest)
    # bare "alpha" → _envs_from_patterns returns ["alpha"]; matches all services
    assert targets == [("alpha", "db"), ("alpha", "api")]


def test_collect_restart_targets_empty_manifest() -> None:
    manifest = DockerManifest(
        project_prefix="myapp",
        environment_compose_file="compose.yaml",
        workspace_compose_file="compose.yaml",
        services=(),
    )
    targets = _collect_restart_targets(["alpha/db"], manifest)
    assert targets == []


def test_collect_restart_targets_wildcard_env_no_concrete() -> None:
    """A '*/<svc>' pattern has no concrete env → empty list + diagnostic."""
    manifest = _make_manifest(services=["db"])
    targets = _collect_restart_targets(["*/db"], manifest)
    assert targets == []


# ---------------------------------------------------------------------------
# 2 & 3. Correct compose argv and project name
# ---------------------------------------------------------------------------


def test_cmd_restart_issues_correct_compose_call(tmp_path: Path) -> None:
    """cmd_restart calls compose restart <svc> with the right project name."""
    manifest = _make_manifest(services=["db"])
    client = FakeComposeClient(compose_default=_ok_result(0))

    rc = cmd_restart(["alpha/db"], manifest, client)

    assert rc == 0
    assert len(client.compose_calls) == 1
    call = client.compose_calls[0]
    assert call.project == "myapp-alpha"
    assert call.compose_file == "compose.yaml"
    assert call.args == ["restart", "db"]


def test_cmd_restart_uses_correct_env_prefix(tmp_path: Path) -> None:
    """Project name uses the manifest prefix."""
    manifest = _make_manifest(prefix="mypfx", services=["api"])
    client = FakeComposeClient(compose_default=_ok_result(0))

    cmd_restart(["beta/api"], manifest, client)

    assert client.compose_calls[0].project == "mypfx-beta"


# ---------------------------------------------------------------------------
# 4. Worst-exit aggregation
# ---------------------------------------------------------------------------


def test_cmd_restart_worst_exit_aggregation(tmp_path: Path) -> None:
    """When multiple services match, the worst exit code is returned."""
    manifest = _make_manifest(services=["db", "api", "worker"])
    # db → 0, api → 1, worker → 2
    client = FakeComposeClient(
        compose_results=[_ok_result(0), _ok_result(1), _ok_result(2)],
    )

    rc = cmd_restart(["alpha/*"], manifest, client)

    assert rc == 2
    assert len(client.compose_calls) == 3


def test_cmd_restart_all_fail_returns_worst(tmp_path: Path) -> None:
    manifest = _make_manifest(services=["db", "api"])
    client = FakeComposeClient(
        compose_results=[_ok_result(1), _ok_result(0)],
    )

    rc = cmd_restart(["alpha/*"], manifest, client)

    assert rc == 1


# ---------------------------------------------------------------------------
# 5 & 6. Diagnostic on no-match / bare token
# ---------------------------------------------------------------------------


def test_cmd_restart_no_match_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = _make_manifest(services=["db"])
    client = FakeComposeClient()

    rc = cmd_restart(["alpha/notexist"], manifest, client)

    assert rc == 1
    assert len(client.compose_calls) == 0
    err = capsys.readouterr().err
    assert "no services matched" in err


def test_cmd_restart_wildcard_env_no_concrete_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = _make_manifest(services=["db"])
    client = FakeComposeClient()

    rc = cmd_restart(["*/db"], manifest, client)

    assert rc == 1
    err = capsys.readouterr().err
    assert "no concrete env" in err or "no services matched" in err


# ---------------------------------------------------------------------------
# 7. Missing manifest fields
# ---------------------------------------------------------------------------


def test_cmd_restart_missing_prefix_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edge case: restart has no project-name prefix source when neither the manifest
    override nor WINTER_SERVICE_PREFIX is set. WINTER_SERVICE_PREFIX is a base
    extension var normally present on every dispatch action (including restart); this
    only happens if it's absent from the process environment entirely."""
    monkeypatch.delenv("WINTER_SERVICE_PREFIX", raising=False)
    manifest = DockerManifest(
        project_prefix=None,
        environment_compose_file="compose.yaml",
        workspace_compose_file="compose.yaml",
        services=(ServiceDecl("db"),),
    )
    client = FakeComposeClient()

    rc = cmd_restart(["alpha/db"], manifest, client)

    assert rc == 1
    assert "no project-name prefix available" in capsys.readouterr().err


def test_cmd_restart_missing_compose_file_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = DockerManifest(
        project_prefix="myapp",
        environment_compose_file=None,
        workspace_compose_file=None,
        services=(ServiceDecl("db"),),
    )
    client = FakeComposeClient()

    rc = cmd_restart(["alpha/db"], manifest, client)

    assert rc == 1
    assert "manifest is missing" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 8. Empty patterns
# ---------------------------------------------------------------------------


def test_cmd_restart_empty_patterns_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = _make_manifest(services=["db"])
    client = FakeComposeClient()

    rc = cmd_restart([], manifest, client)

    assert rc == 1
    assert "at least one" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 9. CLI dispatch via cli_main
# ---------------------------------------------------------------------------


def test_cli_restart_dispatches_correctly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI restart alpha/db issues compose restart db with project myapp-alpha."""
    manifest = _make_manifest(services=["db"])
    fake_client = FakeComposeClient(compose_default=_ok_result(0))

    with (
        patch.dict("os.environ", {"WINTER_WORKSPACE_DIR": str(tmp_path)}),
        patch("docker_orchestrator.cli.load_manifest", return_value=manifest),
        patch("docker_orchestrator.compose_client.ComposeClient", return_value=fake_client),
    ):
        # Patch the actual cmd_restart to use our fake_client
        import docker_orchestrator.restart as restart_mod

        original = restart_mod.cmd_restart

        def patched_restart(patterns, manifest, client):
            return original(patterns, manifest, fake_client)

        with patch.object(restart_mod, "cmd_restart", patched_restart):
            rc = cli_main(["restart", "alpha/db"])

    assert rc == 0
    assert len(fake_client.compose_calls) == 1
    assert fake_client.compose_calls[0].args == ["restart", "db"]
    assert fake_client.compose_calls[0].project == "myapp-alpha"


# ---------------------------------------------------------------------------
# 10. CLI dispatch without args
# ---------------------------------------------------------------------------


def test_cli_restart_no_args_exits_non_2_non_3(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main(["restart"])
    assert rc != 2
    assert rc != 3


# ---------------------------------------------------------------------------
# 11. glob matching with prefix
# ---------------------------------------------------------------------------


def test_cmd_restart_glob_prefix_matches(tmp_path: Path) -> None:
    """Pattern alpha/work* should match 'worker' but not 'db'."""
    manifest = _make_manifest(services=["db", "worker"])
    client = FakeComposeClient(compose_default=_ok_result(0))

    rc = cmd_restart(["alpha/work*"], manifest, client)

    assert rc == 0
    assert len(client.compose_calls) == 1
    assert client.compose_calls[0].args == ["restart", "worker"]
