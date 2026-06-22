"""Per-env context derivation for winter-service-docker.

Given an env name (or the reserved ``workspace`` scope) plus the workspace root,
this module computes:

- ``COMPOSE_PROJECT_NAME`` = ``<project_prefix>-<env>``  (or ``<prefix>-workspace``)
- ``port_base`` by reading ``<workspace>/<env>/.winter.env`` for ``WINTER_PORT_BASE``

Port derivation:
    ``WINTER_PORT_BASE`` from the env's ``.winter.env`` file is the base for all
    published host ports.  Callers can use ``published_port(base, offset)`` to
    compute a specific port.

The ``workspace`` scope is the reserved name for the workspace-scoped singleton
session.  Its ``.winter.env`` does not exist, so ``port_base`` will be ``None``
for workspace contexts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

WORKSPACE_SCOPE = "workspace"
_WINTER_ENV_FILE = ".winter.env"


def _parse_env_file(text: str) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file into a dict.

    Blank lines and ``#``-comments are skipped.  A leading ``export `` token is
    stripped.  Single and double quotes wrapping the value are removed.
    """
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


def read_port_base(workspace_root: Path, env: str) -> int | None:
    """Read ``WINTER_PORT_BASE`` from ``<workspace>/<env>/.winter.env``.

    Returns the integer port base, or ``None`` when the file is absent or the
    key is not present.  The workspace scope has no env file, so this always
    returns ``None`` for ``env == "workspace"``.
    """
    if env == WORKSPACE_SCOPE:
        return None
    env_file = workspace_root / env / _WINTER_ENV_FILE
    if not env_file.exists():
        return None
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return None
    parsed = _parse_env_file(text)
    raw = parsed.get("WINTER_PORT_BASE")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def compose_project_name(project_prefix: str, env: str) -> str:
    """Compute ``COMPOSE_PROJECT_NAME`` for *env* under *project_prefix*.

    Returns ``<prefix>-<env>`` for all env names, including the reserved
    ``workspace`` scope (which yields ``<prefix>-workspace``).
    """
    return f"{project_prefix}-{env}"


def published_port(port_base: int, offset: int) -> int:
    """Compute a published host port from *port_base* and *offset*.

    Example: ``published_port(4060, 2) == 4062``.
    """
    return port_base + offset


@dataclass(frozen=True)
class EnvContext:
    """Resolved per-env context for docker compose invocations.

    Fields:
        env: The env name (e.g. ``"alpha"`` or ``"workspace"``).
        compose_project_name: The value for ``COMPOSE_PROJECT_NAME``.
        port_base: ``WINTER_PORT_BASE`` from the env's ``.winter.env``, or ``None``
            when the file is absent (workspace scope or uninitialized env).
    """

    env: str
    compose_project_name: str
    port_base: int | None


def build_env_context(
    env: str,
    project_prefix: str,
    workspace_root: Path,
) -> EnvContext:
    """Build an ``EnvContext`` for *env* using *project_prefix* and *workspace_root*.

    Reads the env's ``.winter.env`` file to populate ``port_base``; missing file
    or missing key yields ``port_base=None``.
    """
    project_name = compose_project_name(project_prefix, env)
    port_base = read_port_base(workspace_root, env)
    return EnvContext(
        env=env,
        compose_project_name=project_name,
        port_base=port_base,
    )
