"""Config/manifest loader for winter-service-docker.

Reads the extension manifest from ``WINTER_EXT_CONFIG_DIR`` (a ``config.toml``).
Falls back to ``<workspace>/.winter/config/winter-service-docker/`` when the env
var is unset.

Manifest schema (``config.toml``):

    project_prefix = "myapp"          # required — prefix for COMPOSE_PROJECT_NAME
    compose_file   = "compose.yaml"   # required — path to the user-supplied compose file
                                      #   (relative to the config dir or absolute)

    [[service]]
    name = "backend"                  # optional list of declared services

Config dir is resolved at call time; the reader is stateless and re-reads on
every ``load()`` call (no caching — callers may invoke once and hold the result).

Missing config dir or absent ``config.toml`` → ``DockerManifest`` with empty
service list and ``None`` for required fields (graceful degradation used by
``describe`` to emit ``{"services": []}``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ServiceDecl:
    """A single service entry from ``[[service]]`` in ``config.toml``."""

    name: str


@dataclass(frozen=True)
class DockerManifest:
    """The parsed extension manifest.

    ``project_prefix`` and ``compose_file`` are ``None`` when the config file is
    absent or the field is missing — callers that require these fields should
    check and emit a useful error.
    """

    project_prefix: str | None
    compose_file: str | None
    services: tuple[ServiceDecl, ...] = field(default_factory=tuple)


_DEFAULT_CONFIG_SUBDIR = ".winter/config/winter-service-docker"
_CONFIG_FILE = "config.toml"


def resolve_config_dir(workspace_root: Path | None = None) -> Path | None:
    """Return the config directory from ``WINTER_EXT_CONFIG_DIR`` or the default.

    Returns ``None`` when neither source can supply a usable directory.
    """
    ext_config_dir = os.environ.get("WINTER_EXT_CONFIG_DIR")
    if ext_config_dir:
        return Path(ext_config_dir)
    if workspace_root is not None:
        return workspace_root / _DEFAULT_CONFIG_SUBDIR
    ws_dir = os.environ.get("WINTER_WORKSPACE_DIR")
    if ws_dir:
        return Path(ws_dir) / _DEFAULT_CONFIG_SUBDIR
    return None


def load(config_dir: Path | None = None) -> DockerManifest:
    """Load the extension manifest from *config_dir*.

    If *config_dir* is ``None``, ``resolve_config_dir()`` is called to locate
    the directory.  A missing directory or absent ``config.toml`` returns a
    ``DockerManifest`` with ``None`` prefix/file and empty services (graceful).

    Raises ``ValueError`` on malformed TOML or unexpected schema types.
    """
    if config_dir is None:
        config_dir = resolve_config_dir()

    if config_dir is None:
        return DockerManifest(project_prefix=None, compose_file=None)

    config_path = config_dir / _CONFIG_FILE
    if not config_path.exists():
        return DockerManifest(project_prefix=None, compose_file=None)

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read manifest {config_path}: {exc}") from exc

    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"malformed TOML in {config_path}: {exc}") from exc

    project_prefix: str | None = doc.get("project_prefix") or None

    # Resolve compose_file: relative paths are anchored to the config dir (where
    # config.toml lives), NOT the process cwd. winter dispatches the provider
    # with cwd at the workspace root, so a raw relative path handed to
    # ``docker compose -f`` would resolve against the workspace root and miss the
    # file. Absolute paths pass through unchanged.
    compose_file_raw = doc.get("compose_file") or None
    compose_file: str | None = None
    if compose_file_raw is not None:
        compose_path = Path(compose_file_raw)
        compose_file = str(
            compose_path if compose_path.is_absolute() else config_dir / compose_path
        )

    raw_services: list[dict] = doc.get("service", [])  # type: ignore[type-arg]
    services: list[ServiceDecl] = []
    for i, raw in enumerate(raw_services):
        name = raw.get("name")
        if not name or not isinstance(name, str):
            raise ValueError(f"[[service]] entry #{i} is missing a valid 'name' field")
        services.append(ServiceDecl(name=name))

    return DockerManifest(
        project_prefix=project_prefix,
        compose_file=compose_file,
        services=tuple(services),
    )
