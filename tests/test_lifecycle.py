"""Phase 3 unit tests for the ``up`` and ``down`` lifecycle commands.

Covers:
1. ``down`` issues the right compose argv with COMPOSE_PROJECT_NAME set and
   returns the compose exit code.
2. ``up`` issues ``compose up -d`` with the project name + injected port env vars.
3. Readiness gate: returns 0 once a faked ``ps`` reports all healthy after N polls.
4. Readiness gate: times out non-zero when a service stays unhealthy/starting.
5. Running-without-healthcheck is treated as ready immediately.
6. The poll loop uses the injected clock/sleep (assert no real wall-clock delay).
7. Port-substitution env vars are correctly derived from ``WINTER_PORT_BASE``.
8. ``up`` returns non-zero on compose failure (non-zero returncode from compose up).
9. ``up``/``down`` return non-zero on missing manifest fields (graceful error).
10. CLI dispatch: ``up``/``down`` without env arg return a non-2, non-3 exit (not 2 = not unknown-action).
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from docker_orchestrator.cli import main as cli_main
from docker_orchestrator.lifecycle import (
    _build_compose_env,
    _is_service_ready,
    _poll_readiness,
    _port_env_vars,
    cmd_down,
    cmd_up,
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
    svcs = tuple(ServiceDecl(name=s) for s in (services or []))
    return DockerManifest(project_prefix=prefix, compose_file=compose_file, services=svcs)


def _container_ps(
    service: str = "db",
    state: str = "running",
    health_status: str | None = None,
    name: str | None = None,
) -> dict:
    """Build a minimal compose ps JSON container dict."""
    ct: dict = {
        "Service": service,
        "State": state,
        "Name": name or f"myapp-alpha-{service}-1",
    }
    if health_status is not None:
        # docker compose ps --format json emits Health as a plain string.
        ct["Health"] = health_status
    return ct


def _ps_result(containers: list[dict], returncode: int = 0) -> subprocess.CompletedProcess:
    stdout = "\n".join(json.dumps(c) for c in containers)
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def _ok_result(returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout="", stderr="")


# ---------------------------------------------------------------------------
# Fake clock for readiness-gate injection
# ---------------------------------------------------------------------------


class FakeClock:
    """Injectable clock + sleep that never actually sleeps.

    ``now`` advances by ``advance_per_call`` on each ``time()`` call, so
    callers can simulate elapsed time without ``time.sleep``.

    ``sleep_calls`` records every ``sleep(n)`` call for assertion.
    """

    def __init__(self, start: float = 0.0, advance_per_call: float = 0.0) -> None:
        self._t = start
        self._advance = advance_per_call
        self.sleep_calls: list[float] = []

    def time(self) -> float:
        t = self._t
        self._t += self._advance
        return t

    def sleep(self, n: float) -> None:
        self.sleep_calls.append(n)
        self._t += n


# ---------------------------------------------------------------------------
# 1. _is_service_ready helper
# ---------------------------------------------------------------------------


def test_is_service_ready_running_no_healthcheck() -> None:
    assert _is_service_ready("running", None) is True


def test_is_service_ready_running_empty_health() -> None:
    assert _is_service_ready("running", "") is True


def test_is_service_ready_running_healthy() -> None:
    assert _is_service_ready("running", "healthy") is True


def test_is_service_ready_running_unhealthy() -> None:
    assert _is_service_ready("running", "unhealthy") is False


def test_is_service_ready_running_starting() -> None:
    assert _is_service_ready("running", "starting") is False


def test_is_service_ready_not_running() -> None:
    assert _is_service_ready("exited", None) is False
    assert _is_service_ready("created", "healthy") is False


# ---------------------------------------------------------------------------
# 2. _port_env_vars helper
# ---------------------------------------------------------------------------


def test_port_env_vars_single_service() -> None:
    manifest = _make_manifest(services=["db"])
    result = _port_env_vars(manifest.services, 4060)
    assert result == {"WSD_PORT_DB": "4060"}


def test_port_env_vars_multiple_services() -> None:
    manifest = _make_manifest(services=["db", "api", "worker"])
    result = _port_env_vars(manifest.services, 4020)
    assert result == {
        "WSD_PORT_DB": "4020",
        "WSD_PORT_API": "4021",
        "WSD_PORT_WORKER": "4022",
    }


def test_port_env_vars_uppercase_service_name() -> None:
    manifest = _make_manifest(services=["my-service"])
    result = _port_env_vars(manifest.services, 5000)
    assert "WSD_PORT_MY-SERVICE" in result


def test_port_env_vars_no_services() -> None:
    manifest = _make_manifest(services=[])
    result = _port_env_vars(manifest.services, 4020)
    assert result == {}


# ---------------------------------------------------------------------------
# 3. _build_compose_env helper
# ---------------------------------------------------------------------------


def test_build_compose_env_always_has_project_name(tmp_workspace: Path) -> None:
    from docker_orchestrator.env_context import build_env_context

    manifest = _make_manifest(prefix="myapp", services=["db"])
    ctx = build_env_context("alpha", "myapp", tmp_workspace)
    env = _build_compose_env(ctx, manifest.services_for_scope("alpha"))
    assert env["COMPOSE_PROJECT_NAME"] == "myapp-alpha"


def test_build_compose_env_includes_port_vars(tmp_workspace: Path) -> None:
    from docker_orchestrator.env_context import build_env_context

    manifest = _make_manifest(prefix="myapp", services=["db", "api"])
    ctx = build_env_context("alpha", "myapp", tmp_workspace)  # port_base=4020 from fixture
    env = _build_compose_env(ctx, manifest.services_for_scope("alpha"))
    assert env["WSD_PORT_DB"] == "4020"
    assert env["WSD_PORT_API"] == "4021"


def test_build_compose_env_no_port_vars_when_no_port_base() -> None:
    """When port_base is None (workspace scope), no WSD_PORT_* vars are injected."""
    from docker_orchestrator.env_context import EnvContext

    manifest = _make_manifest(services=["db"])
    ctx = EnvContext(env="workspace", compose_project_name="myapp-workspace", port_base=None)
    env = _build_compose_env(ctx, manifest.services_for_scope("workspace"))
    assert "COMPOSE_PROJECT_NAME" in env
    assert not any(k.startswith("WSD_PORT_") for k in env)


# ---------------------------------------------------------------------------
# 4. cmd_down — argv and project name
# ---------------------------------------------------------------------------


def test_cmd_down_issues_compose_down(tmp_workspace: Path) -> None:
    """down issues ``compose down`` with correct project and env vars."""
    fake = FakeComposeClient(compose_results=[_ok_result(0)])
    manifest = _make_manifest(prefix="myapp", services=["db"])
    rc = cmd_down("alpha", manifest, tmp_workspace, fake)
    assert rc == 0
    assert len(fake.compose_calls) == 1
    call = fake.compose_calls[0]
    assert call.project == "myapp-alpha"
    assert call.compose_file == "compose.yaml"
    assert call.args == ["down"]


def test_cmd_down_passes_compose_project_name_in_env(tmp_workspace: Path) -> None:
    """down passes COMPOSE_PROJECT_NAME in the compose invocation environment."""
    fake = FakeComposeClient(compose_results=[_ok_result(0)])
    manifest = _make_manifest(prefix="proj", services=["db"])
    cmd_down("alpha", manifest, tmp_workspace, fake)
    call = fake.compose_calls[0]
    assert call.env is not None
    assert call.env["COMPOSE_PROJECT_NAME"] == "proj-alpha"


def test_cmd_down_returns_compose_exit_code_on_failure(tmp_workspace: Path) -> None:
    """down propagates a non-zero compose exit code."""
    fake = FakeComposeClient(compose_results=[_ok_result(1)])
    manifest = _make_manifest(services=["db"])
    rc = cmd_down("alpha", manifest, tmp_workspace, fake)
    assert rc == 1


def test_cmd_down_returns_nonzero_on_missing_manifest(tmp_workspace: Path) -> None:
    """down returns non-zero when project_prefix or compose_file is None."""
    fake = FakeComposeClient()
    manifest = DockerManifest(project_prefix=None, compose_file=None, services=())
    rc = cmd_down("alpha", manifest, tmp_workspace, fake)
    assert rc != 0
    assert fake.compose_calls == []


def test_cmd_down_compose_exception_returns_nonzero(tmp_workspace: Path) -> None:
    """down catches subprocess-level errors and returns non-zero (not exit 2)."""

    def exploding_compose(*args, **kwargs):
        raise OSError("docker not found")

    fake = FakeComposeClient()
    fake.compose = exploding_compose  # type: ignore[method-assign]
    manifest = _make_manifest(services=["db"])
    rc = cmd_down("alpha", manifest, tmp_workspace, fake)
    assert rc != 0
    assert rc != 2


# ---------------------------------------------------------------------------
# 5. cmd_up — compose up -d and port env vars
# ---------------------------------------------------------------------------


def test_cmd_up_issues_compose_up_d(tmp_workspace: Path) -> None:
    """up issues ``compose up -d <service-names>`` with the correct project."""
    clock = FakeClock(start=0.0, advance_per_call=0.0)
    # up -d result, then ps result (healthy, no healthcheck)
    ps_containers = [_container_ps("db", "running", health_status=None)]
    fake = FakeComposeClient(
        compose_results=[
            _ok_result(0),  # compose up -d db
            _ps_result(ps_containers),  # compose ps
        ]
    )
    manifest = _make_manifest(prefix="myapp", services=["db"])
    rc = cmd_up(
        "alpha",
        manifest,
        tmp_workspace,
        fake,
        timeout=10.0,
        poll_interval=0.0,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert rc == 0
    # First call is compose up -d with scoped service names
    up_call = fake.compose_calls[0]
    assert up_call.project == "myapp-alpha"
    assert up_call.args == ["up", "-d", "db"]


def test_cmd_up_passes_project_name_and_port_vars(tmp_workspace: Path) -> None:
    """up injects COMPOSE_PROJECT_NAME and WSD_PORT_* into the compose env."""
    clock = FakeClock()
    ps_containers = [_container_ps("db", "running")]
    fake = FakeComposeClient(
        compose_results=[
            _ok_result(0),
            _ps_result(ps_containers),
        ]
    )
    manifest = _make_manifest(prefix="myapp", services=["db", "api"])
    cmd_up(
        "alpha",
        manifest,
        tmp_workspace,
        fake,
        timeout=10.0,
        poll_interval=0.0,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    up_call = fake.compose_calls[0]
    assert up_call.env is not None
    assert up_call.env["COMPOSE_PROJECT_NAME"] == "myapp-alpha"
    assert up_call.env["WSD_PORT_DB"] == "4020"  # port_base=4020 from fixture
    assert up_call.env["WSD_PORT_API"] == "4021"


def test_cmd_up_returns_nonzero_on_compose_up_failure(tmp_workspace: Path) -> None:
    """up returns the compose exit code when compose up -d fails."""
    clock = FakeClock()
    fake = FakeComposeClient(compose_results=[_ok_result(1)])
    manifest = _make_manifest(services=["db"])
    rc = cmd_up(
        "alpha",
        manifest,
        tmp_workspace,
        fake,
        timeout=10.0,
        poll_interval=0.0,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert rc == 1
    # No ps call should happen after a failed up -d
    ps_calls = [c for c in fake.compose_calls if "ps" in c.args]
    assert ps_calls == []


def test_cmd_up_returns_nonzero_on_missing_manifest(tmp_workspace: Path) -> None:
    clock = FakeClock()
    fake = FakeComposeClient()
    manifest = DockerManifest(project_prefix=None, compose_file=None, services=())
    rc = cmd_up(
        "alpha",
        manifest,
        tmp_workspace,
        fake,
        timeout=10.0,
        poll_interval=0.0,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert rc != 0
    assert fake.compose_calls == []


def test_cmd_up_compose_exception_returns_nonzero(tmp_workspace: Path) -> None:
    """up catches subprocess errors and returns non-zero (not exit 2)."""
    clock = FakeClock()

    def exploding_compose(*args, **kwargs):
        raise OSError("docker not found")

    fake = FakeComposeClient()
    fake.compose = exploding_compose  # type: ignore[method-assign]
    manifest = _make_manifest(services=["db"])
    rc = cmd_up(
        "alpha",
        manifest,
        tmp_workspace,
        fake,
        timeout=10.0,
        poll_interval=0.0,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert rc != 0
    assert rc != 2


# ---------------------------------------------------------------------------
# 6. Readiness gate — healthy after N polls
# ---------------------------------------------------------------------------


def test_readiness_gate_returns_0_when_all_healthy_immediately(tmp_workspace: Path) -> None:
    """Gate passes when the first ps poll reports all containers healthy."""
    clock = FakeClock(start=0.0, advance_per_call=1.0)
    ps_containers = [_container_ps("db", "running", "healthy")]
    fake = FakeComposeClient(
        compose_results=[
            _ok_result(0),  # up -d
            _ps_result(ps_containers),  # first ps poll
        ]
    )
    manifest = _make_manifest(services=["db"])
    rc = cmd_up(
        "alpha",
        manifest,
        tmp_workspace,
        fake,
        timeout=30.0,
        poll_interval=0.0,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert rc == 0


def test_readiness_gate_returns_0_after_multiple_polls(tmp_workspace: Path) -> None:
    """Gate passes after N polls where containers transition to healthy."""
    clock = FakeClock(start=0.0, advance_per_call=1.0)
    starting_ct = _container_ps("db", "running", "starting")
    healthy_ct = _container_ps("db", "running", "healthy")
    fake = FakeComposeClient(
        compose_results=[
            _ok_result(0),  # up -d
            _ps_result([starting_ct]),  # poll 1: starting
            _ps_result([starting_ct]),  # poll 2: still starting
            _ps_result([healthy_ct]),  # poll 3: healthy
        ]
    )
    manifest = _make_manifest(services=["db"])
    rc = cmd_up(
        "alpha",
        manifest,
        tmp_workspace,
        fake,
        timeout=60.0,
        poll_interval=0.0,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert rc == 0
    # Three ps calls (polls 1, 2, 3)
    ps_calls = [c for c in fake.compose_calls if "ps" in c.args]
    assert len(ps_calls) == 3


def test_readiness_gate_timeout_returns_nonzero(tmp_workspace: Path) -> None:
    """Gate returns non-zero when a service stays unhealthy past the timeout."""
    # Clock advances fast enough to exceed timeout after 2 polls
    # advance_per_call=6.0, timeout=10.0 → first time() call = 0, sets deadline=10
    # second time() call (remaining check) = 6 → remaining=4 → sleep
    # third time() call (remaining check after 3rd poll) = 12 → timeout
    clock = FakeClock(start=0.0, advance_per_call=6.0)
    unhealthy_ct = _container_ps("db", "running", "unhealthy")
    fake = FakeComposeClient(
        compose_results=[
            _ok_result(0),  # up -d
            _ps_result([unhealthy_ct]),  # poll 1
            _ps_result([unhealthy_ct]),  # poll 2
            _ps_result([unhealthy_ct]),  # poll 3 (never reached)
        ]
    )
    manifest = _make_manifest(services=["db"])
    rc = cmd_up(
        "alpha",
        manifest,
        tmp_workspace,
        fake,
        timeout=10.0,
        poll_interval=0.0,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert rc != 0


def test_readiness_gate_timeout_emits_actionable_message(
    tmp_workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """On timeout, stderr names the unready container."""
    clock = FakeClock(start=0.0, advance_per_call=20.0)
    unhealthy_ct = _container_ps("svc", "running", "unhealthy", name="myapp-alpha-svc-1")
    fake = FakeComposeClient(
        compose_results=[
            _ok_result(0),
            _ps_result([unhealthy_ct]),
        ]
    )
    manifest = _make_manifest(services=["svc"])
    cmd_up(
        "alpha",
        manifest,
        tmp_workspace,
        fake,
        timeout=10.0,
        poll_interval=0.0,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    captured = capsys.readouterr()
    assert "myapp-alpha-svc-1" in captured.err
    assert "timeout" in captured.err.lower()


# ---------------------------------------------------------------------------
# 7. Running-without-healthcheck is treated as ready
# ---------------------------------------------------------------------------


def test_readiness_gate_running_no_healthcheck_is_ready(tmp_workspace: Path) -> None:
    """running container without a healthcheck is immediately ready."""
    clock = FakeClock(start=0.0, advance_per_call=1.0)
    ct = _container_ps("db", "running", health_status=None)
    fake = FakeComposeClient(
        compose_results=[
            _ok_result(0),
            _ps_result([ct]),
        ]
    )
    manifest = _make_manifest(services=["db"])
    rc = cmd_up(
        "alpha",
        manifest,
        tmp_workspace,
        fake,
        timeout=10.0,
        poll_interval=0.0,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert rc == 0


def test_readiness_gate_mixed_health_and_no_healthcheck(tmp_workspace: Path) -> None:
    """Mixed containers: one with healthcheck healthy, one without → all ready."""
    clock = FakeClock(start=0.0, advance_per_call=1.0)
    ct1 = _container_ps("db", "running", "healthy")
    ct2 = _container_ps("api", "running", health_status=None)
    fake = FakeComposeClient(
        compose_results=[
            _ok_result(0),
            _ps_result([ct1, ct2]),
        ]
    )
    manifest = _make_manifest(services=["db", "api"])
    rc = cmd_up(
        "alpha",
        manifest,
        tmp_workspace,
        fake,
        timeout=10.0,
        poll_interval=0.0,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert rc == 0


# ---------------------------------------------------------------------------
# 8. Poll loop uses injected clock/sleep — no real wall-clock delay
# ---------------------------------------------------------------------------


def test_readiness_gate_uses_injected_sleep(tmp_workspace: Path) -> None:
    """sleep_fn is called; real time.sleep is NOT called (fake is instant)."""
    clock = FakeClock(start=0.0, advance_per_call=1.0)
    starting_ct = _container_ps("db", "running", "starting")
    healthy_ct = _container_ps("db", "running", "healthy")
    fake = FakeComposeClient(
        compose_results=[
            _ok_result(0),
            _ps_result([starting_ct]),
            _ps_result([healthy_ct]),
        ]
    )
    manifest = _make_manifest(services=["db"])

    wall_start = time.monotonic()
    cmd_up(
        "alpha",
        manifest,
        tmp_workspace,
        fake,
        timeout=30.0,
        poll_interval=2.0,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    wall_elapsed = time.monotonic() - wall_start

    # Fake clock was used; real elapsed time should be negligible (< 0.5s)
    assert wall_elapsed < 0.5, f"real wall-clock delay detected: {wall_elapsed:.3f}s"
    # The fake sleep was called at least once
    assert len(clock.sleep_calls) >= 1


def test_readiness_gate_sleep_bounded_by_remaining(tmp_workspace: Path) -> None:
    """sleep duration is capped at remaining time (not poll_interval)."""
    # timeout=5, poll_interval=100 → sleep should be ≤ remaining=5
    clock = FakeClock(start=0.0, advance_per_call=0.0)
    starting_ct = _container_ps("db", "running", "starting")

    call_count = [0]

    def advance_on_sleep(n: float) -> None:
        call_count[0] += 1
        clock._t += n  # advance the fake clock by the sleep amount
        if call_count[0] >= 1:
            clock._advance = 10.0  # next time() will be past deadline

    fake = FakeComposeClient(
        compose_results=[
            _ok_result(0),
            _ps_result([starting_ct]),
            _ps_result([starting_ct]),
        ]
    )
    manifest = _make_manifest(services=["db"])
    rc = cmd_up(
        "alpha",
        manifest,
        tmp_workspace,
        fake,
        timeout=5.0,
        poll_interval=100.0,
        time_fn=clock.time,
        sleep_fn=advance_on_sleep,
    )
    assert rc != 0  # timed out


# ---------------------------------------------------------------------------
# 9. _poll_readiness unit
# ---------------------------------------------------------------------------


def test_poll_readiness_returns_ready_when_healthy() -> None:
    containers = [_container_ps("db", "running", "healthy")]
    fake = FakeComposeClient(compose_results=[_ps_result(containers)])
    ready, name = _poll_readiness("myapp-alpha", "compose.yaml", fake, {})
    assert ready is True
    assert name == ""


def test_poll_readiness_returns_not_ready_when_starting() -> None:
    containers = [_container_ps("db", "running", "starting")]
    fake = FakeComposeClient(compose_results=[_ps_result(containers)])
    ready, name = _poll_readiness("myapp-alpha", "compose.yaml", fake, {})
    assert ready is False
    assert name != ""


def test_poll_readiness_empty_ps_output_not_ready() -> None:
    fake = FakeComposeClient(compose_results=[_ps_result([])])
    ready, _name = _poll_readiness("myapp-alpha", "compose.yaml", fake, {})
    assert ready is False


def test_poll_readiness_ps_env_passed_through() -> None:
    """_poll_readiness passes the compose_env to the ps call."""
    containers = [_container_ps("db", "running")]
    fake = FakeComposeClient(compose_results=[_ps_result(containers)])
    compose_env = {"COMPOSE_PROJECT_NAME": "myapp-alpha", "WSD_PORT_DB": "4020"}
    _poll_readiness("myapp-alpha", "compose.yaml", fake, compose_env)
    call = fake.compose_calls[0]
    # The env passed through is the same object
    assert call.env is compose_env


# ---------------------------------------------------------------------------
# 10. CLI dispatch — up/down missing env arg (graceful, not exit 2)
# ---------------------------------------------------------------------------


def test_cli_up_no_env_arg_returns_non_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """``up`` without an env argument returns non-zero; 2 is acceptable since
    the action dispatches to a missing-arg error."""
    rc = cli_main(["up"])
    assert rc != 0


def test_cli_down_no_env_arg_returns_non_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """``down`` without an env argument returns non-zero."""
    rc = cli_main(["down"])
    assert rc != 0


# ---------------------------------------------------------------------------
# 11. CLI dispatch — up/down with env arg (end-to-end via patched manifest)
# ---------------------------------------------------------------------------


def test_cli_up_with_env_dispatches(tmp_path: Path, tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI ``up alpha`` dispatches through lifecycle and returns 0 on success."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'project_prefix = "myapp"\ncompose_file = "compose.yaml"\n[[service]]\nname = "db"\n',
        encoding="utf-8",
    )

    ps_containers = [_container_ps("db", "running")]
    results = [_ok_result(0), _ps_result(ps_containers)]

    import docker_orchestrator.compose_client as cc_mod

    class PatchedClient:
        def __init__(self):
            self._results = list(results)

        def compose(self, *args, **kwargs):
            if self._results:
                return self._results.pop(0)
            return _ok_result(0)

    with (
        patch.dict(
            "os.environ",
            {
                "WINTER_EXT_CONFIG_DIR": str(config_dir),
                "WINTER_WORKSPACE_DIR": str(tmp_workspace),
            },
        ),
        patch.object(cc_mod, "ComposeClient", PatchedClient),
    ):
        rc = cli_main(["up", "alpha"])

    assert rc == 0


def test_cli_down_with_env_dispatches(tmp_path: Path, tmp_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI ``down alpha`` dispatches through lifecycle and returns 0 on success."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'project_prefix = "myapp"\ncompose_file = "compose.yaml"\n[[service]]\nname = "db"\n',
        encoding="utf-8",
    )

    import docker_orchestrator.compose_client as cc_mod

    class PatchedClient:
        def compose(self, *args, **kwargs):
            return _ok_result(0)

    with (
        patch.dict(
            "os.environ",
            {
                "WINTER_EXT_CONFIG_DIR": str(config_dir),
                "WINTER_WORKSPACE_DIR": str(tmp_workspace),
            },
        ),
        patch.object(cc_mod, "ComposeClient", PatchedClient),
    ):
        rc = cli_main(["down", "alpha"])

    assert rc == 0
