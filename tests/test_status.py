"""Unit tests for the ``status`` command implementation.

Covers:
1. Docker → winter state/health mapping (each case per the contract doc).
2. Published-port extraction from ``Publishers``.
3. Env-keyed document shape (``{"envs": [...]}``, all required fields present).
4. Pattern filtering by env/service.
5. Both compose-ps JSON encodings: line-delimited objects AND top-level array.
6. Manifest-absent / empty-manifest graceful handling.
7. Declared service not returned by compose ps → stopped/unknown.
8. cmd_status integration via FakeComposeClient.
9. CLI dispatch: ``status`` exits 0 and emits valid JSON.
10. Phase 3 contract: WINTER_PORT_BASE injected via env; no self-sourcing; single-scope behaviour.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from docker_orchestrator.cli import main as cli_main
from docker_orchestrator.manifest import DockerManifest, ServiceDecl
from docker_orchestrator.status import (
    _envs_from_patterns,
    _extract_ports,
    _map_docker_health,
    _map_docker_state,
    _parse_compose_ps_output,
    _service_matches_any_pattern,
    cmd_status,
)
from tests.fakes import FakeComposeClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(
    prefix: str | None = "myapp", compose_file: str = "compose.yaml", services: list[str] | None = None
) -> DockerManifest:
    svcs = tuple(ServiceDecl(name=s) for s in (services or []))
    return DockerManifest(
        project_prefix=prefix,
        environment_compose_file=compose_file,
        workspace_compose_file=compose_file,
        services=svcs,
    )


def _container(
    service: str = "db",
    state: str = "running",
    health_status: str | None = None,
    name: str = "myapp-alpha-db-1",
    publishers: list[dict] | None = None,
    started_at: str | None = None,
) -> dict:
    ct: dict = {
        "Service": service,
        "State": state,
        "Name": name,
    }
    if health_status is not None:
        # docker compose ps --format json emits Health as a plain string.
        ct["Health"] = health_status
    if publishers is not None:
        ct["Publishers"] = publishers
    if started_at is not None:
        ct["StartedAt"] = started_at
    return ct


def _ps_json_lines(containers: list[dict]) -> str:
    return "\n".join(json.dumps(c) for c in containers)


def _ps_json_array(containers: list[dict]) -> str:
    return json.dumps(containers)


def _fake_client_ps(containers: list[dict], encoding: str = "lines") -> FakeComposeClient:
    """Build a FakeComposeClient that returns canned ps output."""
    stdout = _ps_json_array(containers) if encoding == "array" else _ps_json_lines(containers)
    result = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
    return FakeComposeClient(compose_results=[result])


# ---------------------------------------------------------------------------
# 1. Docker → winter state mapping
# ---------------------------------------------------------------------------


def test_map_state_running() -> None:
    assert _map_docker_state("running") == "running"


def test_map_state_exited() -> None:
    assert _map_docker_state("exited") == "stopped"


def test_map_state_created() -> None:
    assert _map_docker_state("created") == "stopped"


def test_map_state_dead() -> None:
    assert _map_docker_state("dead") == "stopped"


def test_map_state_paused() -> None:
    assert _map_docker_state("paused") == "stopped"


def test_map_state_removing() -> None:
    assert _map_docker_state("removing") == "stopped"


def test_map_state_unknown_docker_state() -> None:
    assert _map_docker_state("restarting") == "unknown"


# ---------------------------------------------------------------------------
# 2. Docker → winter health mapping
# ---------------------------------------------------------------------------


def test_map_health_running_healthy() -> None:
    assert _map_docker_health("running", "healthy") == "healthy"


def test_map_health_running_unhealthy() -> None:
    assert _map_docker_health("running", "unhealthy") == "unhealthy"


def test_map_health_running_starting() -> None:
    """starting is coerced to unknown."""
    assert _map_docker_health("running", "starting") == "unknown"


def test_map_health_running_no_healthcheck() -> None:
    """None health (no healthcheck) → unknown."""
    assert _map_docker_health("running", None) == "unknown"


def test_map_health_running_empty_string() -> None:
    """Empty-string health (compose omitted it) → unknown."""
    assert _map_docker_health("running", "") == "unknown"


def test_map_health_exited_healthy_field() -> None:
    """Non-running container health is always unknown regardless of health field."""
    assert _map_docker_health("exited", "healthy") == "unknown"


def test_map_health_created_no_health() -> None:
    assert _map_docker_health("created", None) == "unknown"


# ---------------------------------------------------------------------------
# 3. Published-port extraction
# ---------------------------------------------------------------------------


def test_extract_ports_single() -> None:
    pubs = [{"PublishedPort": 5432, "TargetPort": 5432, "Protocol": "tcp"}]
    assert _extract_ports(pubs) == [5432]


def test_extract_ports_multiple() -> None:
    pubs = [
        {"PublishedPort": 8080},
        {"PublishedPort": 8443},
    ]
    assert _extract_ports(pubs) == [8080, 8443]


def test_extract_ports_deduplicates() -> None:
    pubs = [{"PublishedPort": 5432}, {"PublishedPort": 5432}]
    assert _extract_ports(pubs) == [5432]


def test_extract_ports_zero_filtered() -> None:
    """Port 0 means 'not published'; skip it."""
    pubs = [{"PublishedPort": 0}, {"PublishedPort": 5432}]
    assert _extract_ports(pubs) == [5432]


def test_extract_ports_no_publishers() -> None:
    assert _extract_ports(None) == []


def test_extract_ports_empty_list() -> None:
    assert _extract_ports([]) == []


def test_extract_ports_malformed_entry() -> None:
    """Non-dict entries are silently skipped."""
    pubs = ["not-a-dict", {"PublishedPort": 9000}]
    assert _extract_ports(pubs) == [9000]


# ---------------------------------------------------------------------------
# 4. compose ps JSON encoding handling
# ---------------------------------------------------------------------------


def test_parse_ps_line_delimited() -> None:
    containers = [
        {"Service": "db", "State": "running"},
        {"Service": "api", "State": "exited"},
    ]
    result = _parse_compose_ps_output(_ps_json_lines(containers))
    assert len(result) == 2
    assert result[0]["Service"] == "db"
    assert result[1]["Service"] == "api"


def test_parse_ps_array_encoding() -> None:
    containers = [
        {"Service": "db", "State": "running"},
        {"Service": "api", "State": "running"},
    ]
    result = _parse_compose_ps_output(_ps_json_array(containers))
    assert len(result) == 2


def test_parse_ps_empty_output() -> None:
    assert _parse_compose_ps_output("") == []


def test_parse_ps_whitespace_only() -> None:
    assert _parse_compose_ps_output("   \n  ") == []


def test_parse_ps_skips_bad_lines() -> None:
    good = json.dumps({"Service": "db", "State": "running"})
    bad = "not-json"
    result = _parse_compose_ps_output(f"{bad}\n{good}")
    assert len(result) == 1
    assert result[0]["Service"] == "db"


def test_parse_ps_single_object_not_array() -> None:
    """A single non-array JSON object line is parsed as one container."""
    ct = {"Service": "db", "State": "running"}
    result = _parse_compose_ps_output(json.dumps(ct))
    assert len(result) == 1


# ---------------------------------------------------------------------------
# 5. Pattern filtering
# ---------------------------------------------------------------------------


def test_pattern_no_patterns_matches_all() -> None:
    assert _service_matches_any_pattern("alpha", "db", []) is True


def test_pattern_bare_env_matches_any_service() -> None:
    assert _service_matches_any_pattern("alpha", "db", ["alpha"]) is True
    assert _service_matches_any_pattern("alpha", "api", ["alpha"]) is True


def test_pattern_bare_env_no_match() -> None:
    assert _service_matches_any_pattern("beta", "db", ["alpha"]) is False


def test_pattern_env_svc_exact_match() -> None:
    assert _service_matches_any_pattern("alpha", "db", ["alpha/db"]) is True


def test_pattern_env_svc_no_match() -> None:
    assert _service_matches_any_pattern("alpha", "api", ["alpha/db"]) is False


def test_pattern_wildcard_svc() -> None:
    assert _service_matches_any_pattern("alpha", "db", ["alpha/*"]) is True
    assert _service_matches_any_pattern("alpha", "api", ["alpha/*"]) is True


def test_pattern_wildcard_env() -> None:
    assert _service_matches_any_pattern("alpha", "db", ["*/db"]) is True
    assert _service_matches_any_pattern("beta", "db", ["*/db"]) is True


def test_pattern_multiple_patterns() -> None:
    patterns = ["alpha/db", "beta/api"]
    assert _service_matches_any_pattern("alpha", "db", patterns) is True
    assert _service_matches_any_pattern("beta", "api", patterns) is True
    assert _service_matches_any_pattern("alpha", "api", patterns) is False


# ---------------------------------------------------------------------------
# 6. _envs_from_patterns
# ---------------------------------------------------------------------------


def test_envs_from_patterns_empty() -> None:
    assert _envs_from_patterns([]) == []


def test_envs_from_patterns_bare() -> None:
    assert _envs_from_patterns(["alpha"]) == ["alpha"]


def test_envs_from_patterns_env_svc() -> None:
    assert _envs_from_patterns(["alpha/db"]) == ["alpha"]


def test_envs_from_patterns_deduplicates() -> None:
    assert _envs_from_patterns(["alpha/db", "alpha/api"]) == ["alpha"]


def test_envs_from_patterns_wildcard_env_excluded() -> None:
    """Wildcard env segments cannot be resolved to a concrete env."""
    assert _envs_from_patterns(["*/db"]) == []


def test_envs_from_patterns_multiple_envs() -> None:
    result = _envs_from_patterns(["alpha/db", "beta/api"])
    assert result == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# 7. cmd_status integration via FakeComposeClient
# ---------------------------------------------------------------------------


def test_cmd_status_no_patterns_uses_winter_env(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No patterns → falls back to WINTER_ENV from process env (Phase 3 contract)."""
    # Phase 3: core injects WINTER_ENV; provider does NOT enumerate the filesystem.
    containers = [_container("db", "running")]
    fake = _fake_client_ps(containers)
    manifest = _make_manifest(services=["db"])
    with patch.dict("os.environ", {"WINTER_ENV": "alpha", "WINTER_PORT_BASE": "4020"}):
        rc = cmd_status(patterns=[], manifest=manifest, client=fake)
    assert rc == 0
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    env_names = [e["env"] for e in doc["envs"]]
    assert env_names == ["alpha"]
    # compose was called exactly once for the single injected scope
    assert len(fake.compose_calls) == 1


def test_cmd_status_no_patterns_empty_workspace(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No patterns + no WINTER_ENV in environment → empty envs list (Phase 3 contract)."""
    manifest = _make_manifest(services=["db"])
    fake = FakeComposeClient()
    # Ensure WINTER_ENV is not set so the fallback also finds nothing.
    env = {k: v for k, v in __import__("os").environ.items() if k != "WINTER_ENV"}
    with patch.dict("os.environ", env, clear=True):
        rc = cmd_status(patterns=[], manifest=manifest, client=fake)
    assert rc == 0
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc == {"envs": []}
    assert fake.compose_calls == []


def test_cmd_status_running_healthy(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """running + healthy → state=running, health=healthy."""
    containers = [_container("db", "running", "healthy", publishers=[{"PublishedPort": 5432}])]
    fake = _fake_client_ps(containers)
    manifest = _make_manifest(services=["db"])
    rc = cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    svc = doc["envs"][0]["services"][0]
    assert svc["state"] == "running"
    assert svc["health"] == "healthy"
    assert svc["ports"] == [5432]


def test_cmd_status_running_no_healthcheck(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """running + no health → state=running, health=unknown."""
    containers = [_container("api", "running", health_status=None)]
    fake = _fake_client_ps(containers)
    manifest = _make_manifest(services=["api"])
    rc = cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    svc = doc["envs"][0]["services"][0]
    assert svc["state"] == "running"
    assert svc["health"] == "unknown"


def test_cmd_status_running_unhealthy(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    containers = [_container("api", "running", "unhealthy")]
    fake = _fake_client_ps(containers)
    manifest = _make_manifest(services=["api"])
    rc = cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    svc = doc["envs"][0]["services"][0]
    assert svc["state"] == "running"
    assert svc["health"] == "unhealthy"


def test_cmd_status_running_starting(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """running + health=starting → state=running, health=unknown."""
    containers = [_container("api", "running", "starting")]
    fake = _fake_client_ps(containers)
    manifest = _make_manifest(services=["api"])
    rc = cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    svc = doc["envs"][0]["services"][0]
    assert svc["state"] == "running"
    assert svc["health"] == "unknown"


def test_cmd_status_exited(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    containers = [_container("db", "exited")]
    fake = _fake_client_ps(containers)
    manifest = _make_manifest(services=["db"])
    rc = cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    svc = doc["envs"][0]["services"][0]
    assert svc["state"] == "stopped"
    assert svc["health"] == "unknown"


def test_cmd_status_created(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    containers = [_container("db", "created")]
    fake = _fake_client_ps(containers)
    manifest = _make_manifest(services=["db"])
    rc = cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    svc = doc["envs"][0]["services"][0]
    assert svc["state"] == "stopped"
    assert svc["health"] == "unknown"


def test_cmd_status_declared_not_in_compose(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A declared service absent from compose ps output → stopped/unknown."""
    fake = _fake_client_ps([])  # compose returns nothing
    manifest = _make_manifest(services=["db", "api"])
    rc = cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    services = doc["envs"][0]["services"]
    assert len(services) == 2
    for svc in services:
        assert svc["state"] == "stopped"
        assert svc["health"] == "unknown"
        assert svc["handle"] is None


def test_cmd_status_array_encoding(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Compose ps JSON array encoding is parsed correctly."""
    containers = [_container("db", "running", "healthy")]
    fake = _fake_client_ps(containers, encoding="array")
    manifest = _make_manifest(services=["db"])
    rc = cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    svc = doc["envs"][0]["services"][0]
    assert svc["state"] == "running"
    assert svc["health"] == "healthy"


def test_cmd_status_document_shape(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Every required field is present; port_base comes from injected WINTER_PORT_BASE."""
    containers = [_container("db", "running", "healthy", publishers=[{"PublishedPort": 5432}])]
    fake = _fake_client_ps(containers)
    manifest = _make_manifest(services=["db"])
    with patch.dict("os.environ", {"WINTER_PORT_BASE": "4020"}):
        rc = cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)

    assert "envs" in doc
    env_doc = doc["envs"][0]
    assert env_doc["env"] == "alpha"
    assert env_doc["session"] is None
    assert env_doc["port_base"] == 4020  # from injected WINTER_PORT_BASE
    svc = env_doc["services"][0]
    assert "name" in svc
    assert "state" in svc
    assert "health" in svc
    assert "ports" in svc
    assert "handle" in svc
    assert "log_path" in svc
    assert "since" in svc


def test_cmd_status_handle_from_container_name(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    containers = [_container("db", "running", name="myapp-alpha-db-1")]
    fake = _fake_client_ps(containers)
    manifest = _make_manifest(services=["db"])
    rc = cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    svc = doc["envs"][0]["services"][0]
    assert svc["handle"] == "myapp-alpha-db-1"


def test_cmd_status_since_from_started_at(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ts = "2026-06-22T10:00:00Z"
    containers = [_container("db", "running", started_at=ts)]
    fake = _fake_client_ps(containers)
    manifest = _make_manifest(services=["db"])
    rc = cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    svc = doc["envs"][0]["services"][0]
    assert svc["since"] == ts


def test_cmd_status_pattern_filters_service(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``alpha/db`` pattern selects only db, not api."""
    containers = [
        _container("db", "running", "healthy"),
        _container("api", "running", "healthy"),
    ]
    fake = _fake_client_ps(containers)
    manifest = _make_manifest(services=["db", "api"])
    rc = cmd_status(patterns=["alpha/db"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    services = doc["envs"][0]["services"]
    assert len(services) == 1
    assert services[0]["name"] == "db"


def test_cmd_status_pattern_wildcard_service(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``alpha/*`` selects all services in alpha."""
    containers = [
        _container("db", "running"),
        _container("api", "running"),
    ]
    fake = _fake_client_ps(containers)
    manifest = _make_manifest(services=["db", "api"])
    rc = cmd_status(patterns=["alpha/*"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert len(doc["envs"][0]["services"]) == 2


def test_cmd_status_missing_manifest_fields(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Missing project_prefix → env entry with empty services."""
    manifest = DockerManifest(
        project_prefix=None, environment_compose_file=None, workspace_compose_file=None, services=()
    )
    fake = FakeComposeClient()
    rc = cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    env_doc = doc["envs"][0]
    assert env_doc["services"] == []
    assert fake.compose_calls == []


def test_cmd_status_empty_compose_output(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Empty compose ps output → all declared services appear as stopped."""
    result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    fake = FakeComposeClient(compose_results=[result])
    manifest = _make_manifest(services=["db"])
    rc = cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    svc = doc["envs"][0]["services"][0]
    assert svc["state"] == "stopped"


def test_cmd_status_compose_called_with_project_name(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """compose ps is called with the correct project name and --all flag."""
    fake = _fake_client_ps([])
    manifest = _make_manifest(prefix="proj", services=["db"])
    cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert len(fake.compose_calls) == 1
    call = fake.compose_calls[0]
    assert call.project == "proj-alpha"
    assert call.args == ["ps", "--all", "--format", "json"]


def test_cmd_status_uses_winter_service_prefix_when_no_manifest_override(
    tmp_workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """status derives COMPOSE_PROJECT_NAME from WINTER_SERVICE_PREFIX when the
    manifest has no project_prefix override (issue #5)."""
    monkeypatch.setenv("WINTER_SERVICE_PREFIX", "envprefix")
    fake = _fake_client_ps([])
    manifest = _make_manifest(prefix=None, services=["db"])
    rc = cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert rc == 0
    call = fake.compose_calls[0]
    assert call.project == "envprefix-alpha"


def test_cmd_status_manifest_override_takes_precedence_over_winter_service_prefix(
    tmp_workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit manifest project_prefix override wins over WINTER_SERVICE_PREFIX."""
    monkeypatch.setenv("WINTER_SERVICE_PREFIX", "envprefix")
    fake = _fake_client_ps([])
    manifest = _make_manifest(prefix="proj", services=["db"])
    cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    call = fake.compose_calls[0]
    assert call.project == "proj-alpha"


def test_cmd_status_multiple_ports(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Multiple publishers produce a list of ints."""
    pubs = [{"PublishedPort": 8080}, {"PublishedPort": 8443}]
    containers = [_container("web", "running", publishers=pubs)]
    fake = _fake_client_ps(containers)
    manifest = _make_manifest(services=["web"])
    rc = cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    ports = doc["envs"][0]["services"][0]["ports"]
    assert ports == [8080, 8443]


def test_cmd_status_log_path_is_null(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """log_path is always null for docker (no file logging in this phase)."""
    containers = [_container("db", "running")]
    fake = _fake_client_ps(containers)
    manifest = _make_manifest(services=["db"])
    cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    doc = json.loads(capsys.readouterr().out)
    assert doc["envs"][0]["services"][0]["log_path"] is None


# ---------------------------------------------------------------------------
# 8. CLI dispatch test for status
# ---------------------------------------------------------------------------


def test_cli_status_no_patterns_exits_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI status with no patterns exits 0 and emits valid JSON."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'project_prefix = "myapp"\nenvironment_compose_file = "compose.yaml"\nworkspace_compose_file = "workspace-compose.yaml"\n[[service]]\nname = "db"\n',
        encoding="utf-8",
    )
    with patch.dict(
        "os.environ",
        {
            "WINTER_EXT_CONFIG_DIR": str(config_dir),
            "WINTER_WORKSPACE_DIR": str(tmp_path),
        },
    ):
        rc = cli_main(["status"])
    assert rc == 0
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert "envs" in doc
    assert doc["envs"] == []


def test_cli_status_with_env_pattern(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI status with an env pattern exits 0 and emits valid JSON."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'project_prefix = "myapp"\nenvironment_compose_file = "compose.yaml"\nworkspace_compose_file = "workspace-compose.yaml"\n[[service]]\nname = "db"\n',
        encoding="utf-8",
    )
    alpha_dir = tmp_path / "alpha"
    alpha_dir.mkdir()
    (alpha_dir / ".winter.env").write_text("WINTER_ENV=alpha\nWINTER_PORT_BASE=4020\n", encoding="utf-8")

    with patch.dict(
        "os.environ",
        {
            "WINTER_EXT_CONFIG_DIR": str(config_dir),
            "WINTER_WORKSPACE_DIR": str(tmp_path),
        },
    ):
        rc = cli_main(["status", "alpha"])

    assert rc == 0
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert "envs" in doc


# ---------------------------------------------------------------------------
# Phase 3 contract: single-scope, no self-enumeration, injected WINTER_PORT_BASE
# ---------------------------------------------------------------------------


def test_cmd_status_single_scope_from_pattern(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Core passes ``alpha/*`` pattern; provider queries only the alpha scope."""
    containers = [_container("db", "running")]
    ps_stdout = _ps_json_lines(containers)
    fake = FakeComposeClient(
        compose_results=[
            subprocess.CompletedProcess([], 0, stdout=ps_stdout, stderr=""),
        ]
    )
    manifest = _make_manifest(services=["db"])

    with patch.dict("os.environ", {"WINTER_ENV": "alpha", "WINTER_PORT_BASE": "4020"}):
        rc = cmd_status(patterns=["alpha/*"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    # Only one scope reported — no filesystem enumeration
    assert len(doc["envs"]) == 1
    assert doc["envs"][0]["env"] == "alpha"
    assert len(fake.compose_calls) == 1


def test_cmd_status_reads_port_base_from_env_not_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """WINTER_PORT_BASE is read from the process environment, not the .winter.env file.

    The .winter.env file is ABSENT in this test.  A sentinel value (9999) is
    injected via os.environ.  The returned port_base must reflect the injected
    value, proving the provider does not self-source the file.
    """
    # Deliberately do NOT create alpha/.winter.env — if the provider tried to
    # read the file it would get None; the injected sentinel 9999 must win.
    manifest = _make_manifest(services=["db"])
    fake = _fake_client_ps([])

    with patch.dict("os.environ", {"WINTER_ENV": "alpha", "WINTER_PORT_BASE": "9999"}):
        rc = cmd_status(patterns=["alpha/*"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["envs"][0]["port_base"] == 9999


def test_cmd_status_injected_env_wins_over_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Injected WINTER_PORT_BASE wins even when the .winter.env file has a different value.

    The file declares WINTER_PORT_BASE=4020 but the injected env carries 7777.
    The status doc must show 7777, proving no self-sourcing on the status path.
    """
    alpha_dir = tmp_path / "alpha"
    alpha_dir.mkdir()
    (alpha_dir / ".winter.env").write_text(
        "WINTER_ENV=alpha\nWINTER_PORT_BASE=4020\n",
        encoding="utf-8",
    )
    manifest = _make_manifest(services=["db"])
    fake = _fake_client_ps([])

    with patch.dict("os.environ", {"WINTER_ENV": "alpha", "WINTER_PORT_BASE": "7777"}):
        rc = cmd_status(patterns=["alpha/*"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    # 7777 (injected) must win over 4020 (file); no self-sourcing occurred
    assert doc["envs"][0]["port_base"] == 7777


def test_cmd_status_no_patterns_no_env_var_returns_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No patterns and no WINTER_ENV in environment → empty envs list (graceful)."""
    manifest = _make_manifest(services=["db"])
    fake = FakeComposeClient()
    # Ensure WINTER_ENV is absent
    env = {k: v for k, v in __import__("os").environ.items() if k != "WINTER_ENV"}
    with patch.dict("os.environ", env, clear=True):
        rc = cmd_status(patterns=[], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc == {"envs": []}
    assert fake.compose_calls == []


def test_cmd_status_scope_qualified_pattern_filters_service(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Core pattern ``alpha/db`` limits the reported services to db only."""
    containers = [
        _container("db", "running"),
        _container("api", "running"),
    ]
    fake = _fake_client_ps(containers)
    manifest = _make_manifest(services=["db", "api"])

    with patch.dict("os.environ", {"WINTER_ENV": "alpha", "WINTER_PORT_BASE": "4020"}):
        rc = cmd_status(patterns=["alpha/db"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["envs"][0]["env"] == "alpha"
    svc_names = [s["name"] for s in doc["envs"][0]["services"]]
    assert svc_names == ["db"]


# ---------------------------------------------------------------------------
# Fix #2 — compose ps --all makes exited containers visible
# ---------------------------------------------------------------------------


def test_cmd_status_ps_args_include_all_flag(tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """compose ps is called with --all so exited containers are visible."""
    fake = _fake_client_ps([])
    manifest = _make_manifest(services=["db"])
    cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    call = fake.compose_calls[0]
    assert "--all" in call.args


def test_cmd_status_exited_container_maps_to_stopped_via_all(
    tmp_workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exited container returned by ps --all maps to state=stopped."""
    containers = [_container("db", "exited")]
    fake = _fake_client_ps(containers)
    manifest = _make_manifest(services=["db"])
    rc = cmd_status(patterns=["alpha"], manifest=manifest, client=fake)
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    svc = doc["envs"][0]["services"][0]
    assert svc["state"] == "stopped"
    assert svc["health"] == "unknown"


def test_poll_readiness_exited_container_is_not_ready() -> None:
    """An exited container (visible via --all) is detected as not-ready by the gate."""
    from docker_orchestrator.lifecycle import _poll_readiness

    exited_ct = {
        "Service": "db",
        "State": "exited",
        "Name": "myapp-alpha-db-1",
    }
    stdout = json.dumps(exited_ct)
    fake = FakeComposeClient(
        compose_results=[
            subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""),
        ]
    )
    ready, name = _poll_readiness("myapp-alpha", "compose.yaml", fake, {})
    assert ready is False
    assert name != ""


def test_poll_readiness_ps_args_include_all_flag() -> None:
    """_poll_readiness passes --all to compose ps."""
    from docker_orchestrator.lifecycle import _poll_readiness

    healthy_ct = {"Service": "db", "State": "running", "Name": "myapp-alpha-db-1"}
    stdout = json.dumps(healthy_ct)
    fake = FakeComposeClient(
        compose_results=[
            subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""),
        ]
    )
    _poll_readiness("myapp-alpha", "compose.yaml", fake, {})
    call = fake.compose_calls[0]
    assert "--all" in call.args


# ---------------------------------------------------------------------------
# Fix #6 — empty-config stderr diagnostic
# ---------------------------------------------------------------------------


def test_cli_describe_empty_config_dir_emits_diagnostic(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """describe with a config dir but no config.toml emits a stderr diagnostic."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    with patch.dict(
        "os.environ",
        {
            "WINTER_EXT_CONFIG_DIR": str(config_dir),
            "WINTER_WORKSPACE_DIR": str(tmp_path),
        },
    ):
        rc = cli_main(["describe"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "no config.toml" in captured.err
    doc = json.loads(captured.out)
    assert doc == {"services": []}


def test_cli_status_empty_config_dir_emits_diagnostic(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """status with a config dir but no config.toml emits a stderr diagnostic."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    with patch.dict(
        "os.environ",
        {
            "WINTER_EXT_CONFIG_DIR": str(config_dir),
            "WINTER_WORKSPACE_DIR": str(tmp_path),
        },
    ):
        rc = cli_main(["status"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "no config.toml" in captured.err
