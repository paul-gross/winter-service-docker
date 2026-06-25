"""Tests for the 'catalog' action in docker_orchestrator.cli.

The catalog action reads the extension manifest and returns scope-qualified
service names:
  - ``workspace/<name>`` for workspace-scoped services
  - ``*/<name>``         for project-scoped (env-agnostic) services

Covers:
- Empty catalog when no config or empty manifest
- Project-only services → all names prefixed with ``*/``
- Workspace-only services → all names prefixed with ``workspace/``
- Mixed project + workspace services
- WINTER_EXT_CONFIG_DIR env var is honoured
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docker_orchestrator.cli import main


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    """Return a temporary extension config directory."""
    cfg_dir = tmp_path / ".winter" / "config" / "winter-service-docker"
    cfg_dir.mkdir(parents=True)
    return cfg_dir


def _write_manifest(cfg_dir: Path, content: str) -> None:
    (cfg_dir / "config.toml").write_text(content, encoding="utf-8")


def test_catalog_empty_when_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """No WINTER_EXT_CONFIG_DIR and no default config → empty services list."""
    monkeypatch.delenv("WINTER_EXT_CONFIG_DIR", raising=False)
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    # No config.toml → graceful empty response
    rc = main(["catalog"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    # Should emit argv echo to stderr (via main) and JSON to stdout
    obj = json.loads(out)
    assert obj["services"] == []


def test_catalog_project_services(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Project-scoped services are emitted as ``*/<name>``."""
    monkeypatch.delenv("WINTER_EXT_CONFIG_DIR", raising=False)
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    _write_manifest(
        tmp_config,
        """
project_prefix = "myapp"
environment_compose_file = "compose.yaml"
workspace_compose_file = "workspace-compose.yaml"

[[service]]
name = "backend"

[[service]]
name = "worker"
""",
    )
    rc = main(["catalog"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert "*/backend" in obj["services"]
    assert "*/worker" in obj["services"]


def test_catalog_workspace_services(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Workspace-scoped services are emitted as ``workspace/<name>``."""
    monkeypatch.delenv("WINTER_EXT_CONFIG_DIR", raising=False)
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    _write_manifest(
        tmp_config,
        """
project_prefix = "myapp"
environment_compose_file = "compose.yaml"
workspace_compose_file = "workspace-compose.yaml"

[[service]]
name = "db"
scope = "workspace"
""",
    )
    rc = main(["catalog"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert obj["services"] == ["workspace/db"]


def test_catalog_mixed_scopes(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Mixed project + workspace services."""
    monkeypatch.delenv("WINTER_EXT_CONFIG_DIR", raising=False)
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))
    _write_manifest(
        tmp_config,
        """
project_prefix = "myapp"
environment_compose_file = "compose.yaml"
workspace_compose_file = "workspace-compose.yaml"

[[service]]
name = "api"

[[service]]
name = "rabbitmq"
scope = "workspace"
""",
    )
    rc = main(["catalog"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert "workspace/rabbitmq" in obj["services"]
    assert "*/api" in obj["services"]


def test_catalog_respects_winter_ext_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """WINTER_EXT_CONFIG_DIR overrides the default config path."""
    custom_dir = tmp_path / "custom-config"
    custom_dir.mkdir()
    _write_manifest(
        custom_dir,
        """
project_prefix = "myapp"
environment_compose_file = "compose.yaml"
workspace_compose_file = "workspace-compose.yaml"

[[service]]
name = "cache"
scope = "workspace"
""",
    )
    monkeypatch.setenv("WINTER_EXT_CONFIG_DIR", str(custom_dir))
    monkeypatch.setenv("WINTER_WORKSPACE_DIR", str(tmp_path))

    rc = main(["catalog"])
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert obj["services"] == ["workspace/cache"]
