"""Tests for compose_ps health extraction.

Regression coverage for the fake-vs-reality gap: ``docker compose ps
--format json`` emits ``Health`` as a plain STRING, but the test fakes
originally modeled it as a nested ``{"Status": ...}`` dict — which let both the
readiness gate and status report stale health. ``extract_health`` must handle
the real string form first, and tolerate the dict form defensively.
"""

from __future__ import annotations

from docker_orchestrator.compose_ps import extract_health, map_docker_health


class TestExtractHealthStringForm:
    """The real docker compose ps --format json shape: Health is a string."""

    def test_healthy_string(self) -> None:
        assert extract_health({"State": "running", "Health": "healthy"}) == "healthy"

    def test_starting_string(self) -> None:
        assert extract_health({"State": "running", "Health": "starting"}) == "starting"

    def test_unhealthy_string(self) -> None:
        assert extract_health({"State": "running", "Health": "unhealthy"}) == "unhealthy"

    def test_empty_string_means_no_healthcheck(self) -> None:
        # No healthcheck declared → docker reports "" → treated as None.
        assert extract_health({"State": "running", "Health": ""}) is None

    def test_absent_health_key(self) -> None:
        assert extract_health({"State": "running"}) is None


class TestExtractHealthDictTolerance:
    """Defensive: some tooling nests Health as {"Status": ...}."""

    def test_nested_dict(self) -> None:
        assert extract_health({"Health": {"Status": "healthy"}}) == "healthy"

    def test_nested_dict_empty(self) -> None:
        assert extract_health({"Health": {"Status": ""}}) is None


class TestHealthMappingEndToEnd:
    """The string Health flows correctly through map_docker_health."""

    def test_running_starting_is_unknown_not_healthy(self) -> None:
        # The exact bug: a starting container must NOT map to healthy.
        h = extract_health({"State": "running", "Health": "starting"})
        assert map_docker_health("running", h) == "unknown"

    def test_running_healthy(self) -> None:
        h = extract_health({"State": "running", "Health": "healthy"})
        assert map_docker_health("running", h) == "healthy"

    def test_running_no_healthcheck_is_unknown(self) -> None:
        h = extract_health({"State": "running", "Health": ""})
        assert map_docker_health("running", h) == "unknown"
