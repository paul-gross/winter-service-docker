"""Config/manifest loader for winter-service-docker.

Reads the extension manifest from ``WINTER_EXT_CONFIG_DIR`` (a ``config.toml``).
Falls back to ``<workspace>/.winter/config/winter-service-docker/`` when the env
var is unset.

Manifest schema (``config.toml``):

    project_prefix          = "myapp"                  # optional override
    environment_compose_file = "environment-compose.yaml"  # required — per-env services
    workspace_compose_file   = "workspace-compose.yaml"    # required — workspace singletons

``project_prefix`` is optional. When set, it is an explicit per-provider
override for the ``COMPOSE_PROJECT_NAME`` prefix, taking precedence over the
core-injected ``WINTER_SERVICE_PREFIX`` environment variable (see
``docker_orchestrator.env_context.resolve_project_prefix``). The prefix is
controlled by the workspace-level ``service_prefix`` config and is present on
every dispatch action (including ``restart``/``logs``); ``project_prefix``
should be left unset except as a hand-edited escape hatch for a per-provider
prefix collision, where this provider needs a different prefix than the rest
of the workspace.

    [[service]]
    name = "backend"                  # optional list of declared services
    scope = "project"                 # "project" (default) or "workspace"

Back-compat: The legacy single ``compose_file`` key is detected and rejected
with a clear migration message directing the user to split into two files.

Config dir is resolved at call time; the reader is stateless and re-reads on
every ``load()`` call (no caching — callers may invoke once and hold the result).

Missing config dir or absent ``config.toml`` → ``DockerManifest`` with empty
service list and ``None`` for all fields (graceful degradation used by
``describe`` to emit ``{"services": []}``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from docker_orchestrator.env_context import WORKSPACE_SCOPE

_VALID_SCOPES = ("project", "workspace")


@dataclass(frozen=True)
class ServiceDecl:
    """A single service entry from ``[[service]]`` in ``config.toml``."""

    name: str


@dataclass(frozen=True)
class DockerManifest:
    """The parsed extension manifest.

    ``project_prefix`` is an optional per-provider override for the
    ``COMPOSE_PROJECT_NAME`` prefix (see module docstring); it is ``None``
    when not set in ``config.toml``, in which case callers fall back to the
    core-injected ``WINTER_SERVICE_PREFIX`` via
    ``docker_orchestrator.env_context.resolve_project_prefix``.

    ``environment_compose_file`` and ``workspace_compose_file`` are ``None``
    when the config file is absent or the field is missing — callers that
    require these fields should check and emit a useful error.

    ``services`` holds project-scoped services (``scope = "project"``).
    ``workspace_services`` holds workspace-scoped services (``scope = "workspace"``).
    Names are unique across both partitions (global namespace enforced at load time).
    """

    project_prefix: str | None
    environment_compose_file: str | None
    workspace_compose_file: str | None
    services: tuple[ServiceDecl, ...] = field(default_factory=tuple)
    workspace_services: tuple[ServiceDecl, ...] = field(default_factory=tuple)

    def services_for_scope(self, env: str) -> tuple[ServiceDecl, ...]:
        """Return the services relevant to *env*.

        Returns ``workspace_services`` when *env* is the reserved ``"workspace"``
        scope; returns ``services`` for any other env name.
        """
        if env == WORKSPACE_SCOPE:
            return self.workspace_services
        return self.services

    def compose_file_for_scope(self, env: str) -> str | None:
        """Return the compose file path for *env*'s scope.

        Returns ``workspace_compose_file`` when *env* is the reserved
        ``"workspace"`` scope; returns ``environment_compose_file`` otherwise.
        """
        if env == WORKSPACE_SCOPE:
            return self.workspace_compose_file
        return self.environment_compose_file

    def all_service_names(self) -> list[str]:
        """Return all service names in declaration order: project services first, then workspace.

        This is the canonical enumeration used by ``describe`` to list every
        managed service regardless of scope.
        """
        return [s.name for s in self.services] + [s.name for s in self.workspace_services]


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


def _resolve_file_path(raw: str | None, config_dir: Path) -> str | None:
    """Resolve a config-relative or absolute path to an absolute string.

    Relative paths are anchored to *config_dir* (where ``config.toml`` lives),
    NOT the process cwd.  Absolute paths pass through unchanged.  Returns
    ``None`` when *raw* is ``None`` or empty.
    """
    if not raw:
        return None
    p = Path(raw)
    return str(p if p.is_absolute() else config_dir / p)


def load(config_dir: Path | None = None) -> DockerManifest:
    """Load the extension manifest from *config_dir*.

    If *config_dir* is ``None``, ``resolve_config_dir()`` is called to locate
    the directory.  A missing directory or absent ``config.toml`` returns a
    ``DockerManifest`` with all ``None`` fields and empty services (graceful).

    Raises ``ValueError`` on malformed TOML, unexpected schema types, or when
    the legacy ``compose_file`` key is present (migration error).
    """
    if config_dir is None:
        config_dir = resolve_config_dir()

    if config_dir is None:
        return DockerManifest(project_prefix=None, environment_compose_file=None, workspace_compose_file=None)

    config_path = config_dir / _CONFIG_FILE
    if not config_path.exists():
        return DockerManifest(project_prefix=None, environment_compose_file=None, workspace_compose_file=None)

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read manifest {config_path}: {exc}") from exc

    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"malformed TOML in {config_path}: {exc}") from exc

    project_prefix: str | None = doc.get("project_prefix") or None

    # Back-compat: reject the legacy single compose_file key with a migration message.
    if "compose_file" in doc:
        raise ValueError(
            f"config.toml at {config_path} uses the legacy 'compose_file' key, "
            "which is no longer supported. "
            "Migrate by splitting your compose file into two scope-pure files and "
            "replacing 'compose_file' with:\n"
            '  environment_compose_file = "environment-compose.yaml"  '
            "# per-env services\n"
            '  workspace_compose_file   = "workspace-compose.yaml"    '
            "# workspace singletons\n"
            "Run 'python3 -m docker_orchestrator.scaffold <dest>' to generate "
            "starter files as a reference."
        )

    # Resolve compose files: relative paths are anchored to the config dir (where
    # config.toml lives), NOT the process cwd. winter dispatches the provider
    # with cwd at the workspace root, so a raw relative path handed to
    # ``docker compose -f`` would resolve against the workspace root and miss the
    # file. Absolute paths pass through unchanged.
    environment_compose_file = _resolve_file_path(doc.get("environment_compose_file") or None, config_dir)
    workspace_compose_file = _resolve_file_path(doc.get("workspace_compose_file") or None, config_dir)

    raw_services: list[dict] = doc.get("service", [])  # type: ignore[type-arg]
    services: list[ServiceDecl] = []
    workspace_services: list[ServiceDecl] = []
    seen_names: set[str] = set()
    for i, raw in enumerate(raw_services):
        name = raw.get("name")
        if not name or not isinstance(name, str):
            raise ValueError(f"[[service]] entry #{i} is missing a valid 'name' field")
        scope = raw.get("scope", "project")
        if scope not in _VALID_SCOPES:
            raise ValueError(f"[[service]] entry '{name}' has invalid scope {scope!r}; allowed: 'project', 'workspace'")
        if name in seen_names:
            raise ValueError(f"[[service]] entry '{name}' has a duplicate name")
        seen_names.add(name)
        decl = ServiceDecl(name=name)
        if scope == "workspace":
            workspace_services.append(decl)
        else:
            services.append(decl)

    return DockerManifest(
        project_prefix=project_prefix,
        environment_compose_file=environment_compose_file,
        workspace_compose_file=workspace_compose_file,
        services=tuple(services),
        workspace_services=tuple(workspace_services),
    )
