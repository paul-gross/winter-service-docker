"""Shared pytest fixtures for docker_orchestrator tests.

Provides:
- ``tmp_workspace``: a ``tmp_path``-based workspace root with a seeded
  ``alpha/.winter.env`` so port-base tests don't need to build paths manually.
- ``config_dir``: a ``tmp_path``-based config dir ready for a ``config.toml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def tmp_workspace(tmp_path: Path) -> Path:
    """Return a temporary workspace root with a seeded alpha env.

    Creates ``<root>/alpha/.winter.env`` with ``WINTER_PORT_BASE=4020`` and
    ``WINTER_ENV=alpha`` so env-context tests have a real file to read.
    """
    alpha_dir = tmp_path / "alpha" / ".winter"
    alpha_dir.mkdir(parents=True)
    (alpha_dir.parent / ".winter.env").write_text(
        "WINTER_ENV=alpha\nWINTER_ENV_INDEX=1\nWINTER_PORT_BASE=4020\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """Return a temporary config directory (empty — tests write their own config.toml)."""
    d = tmp_path / "config"
    d.mkdir()
    return d
