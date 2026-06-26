"""Shared pytest fixtures for docker_orchestrator tests.

Provides:
- ``tmp_workspace``: a ``tmp_path``-based workspace root with an ``alpha/``
  subdirectory so tests have a realistic workspace path structure.
- ``config_dir``: a ``tmp_path``-based config dir ready for a ``config.toml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def tmp_workspace(tmp_path: Path) -> Path:
    """Return a temporary workspace root with an alpha env directory.

    Creates ``<root>/alpha/`` so env-context tests have a realistic path
    structure.  Tests that need a specific ``WINTER_PORT_BASE`` value must
    use ``monkeypatch.setenv("WINTER_PORT_BASE", ...)`` directly — the
    value is now injected by winter-cli core via the process environment,
    not read from a per-env file.
    """
    alpha_dir = tmp_path / "alpha"
    alpha_dir.mkdir(parents=True)
    return tmp_path


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """Return a temporary config directory (empty — tests write their own config.toml)."""
    d = tmp_path / "config"
    d.mkdir()
    return d
