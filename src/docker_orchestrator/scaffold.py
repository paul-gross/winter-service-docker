"""Starter-config scaffolder for winter-service-docker.

Writes three files into a destination directory:
  - ``environment-compose.yaml`` — per-env services using ``${WSD_PORT_*}`` port substitution
  - ``workspace-compose.yaml``   — workspace-scoped singleton services
  - ``config.toml``              — a starter extension manifest for ``winter-service-docker``

Usage::

    python3 -m docker_orchestrator.scaffold <dest-dir> [--force]

``--force`` overwrites existing files; without it the command refuses to clobber.

The generated files are intended as a starting point.  Edit them for your project
before running ``winter service up``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Template strings
# ---------------------------------------------------------------------------

_ENVIRONMENT_COMPOSE_YAML = """\
# Starter environment-compose.yaml for winter-service-docker.
#
# This file contains ONLY per-env (project-scoped) services.  It is run by
# winter under the <project_prefix>-<env> compose project name so each feature
# environment gets isolated containers, networks, and ports.
#
# Port substitution uses ${WSD_PORT_<NAME>} placeholders.  winter-service-docker
# resolves WSD_PORT_<NAME> = WINTER_PORT_BASE + <position> at runtime, where
# <position> is the 0-based declaration order among [[service]] entries in
# config.toml (workspace-scoped entries are excluded).  Two envs never collide.
#
# You can reproduce what winter does by hand:
#   source <(winter env alpha)
#   docker compose -p myapp-alpha \\
#       -f environment-compose.yaml \\
#       up -d

services:
  backend:
    image: your-backend-image:latest
    ports:
      - "${WSD_PORT_BACKEND}:8080"
    environment:
      DATABASE_URL: "postgresql://app:app@db:5432/app"
"""

_WORKSPACE_COMPOSE_YAML = """\
# Starter workspace-compose.yaml for winter-service-docker.
#
# This file contains ONLY workspace-scoped singleton services.  It is run by
# winter under the <project_prefix>-workspace compose project name, once for
# the whole workspace rather than once per feature env.
#
# Workspace-scoped services get no WSD_PORT_* variables (that per-env auto-
# derivation needs a per-env WINTER_PORT_BASE, which the workspace scope has not).
# Use fixed host ports (or omit port publishing) instead, or reference
# ${WINTER_WORKSPACE_PORT_BASE} (injected by core for the workspace scope).
#
# Named volumes declared here persist across `docker compose down` and survive
# `winter ws destroy` (the destroy hook runs compose down but does not remove
# volumes).
#
# You can reproduce what winter does by hand:
#   source <(winter env workspace)
#   docker compose -p myapp-workspace \\
#       -f workspace-compose.yaml \\
#       up -d

services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
      POSTGRES_DB: app
    ports:
      - "5432:5432"   # fixed port — workspace scope gets no WSD_PORT_* variable
    volumes:
      - db-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app"]
      interval: 5s
      timeout: 3s
      retries: 10

volumes:
  db-data:
    # Named volume: persists across compose down; shared across all feature envs
    # because db runs in the workspace compose project (not per-env).
"""

_CONFIG_TOML = """\
# Declarative manifest for winter-service-docker (TOML format).
# Place this file at workspace:/.winter/config/winter-service-docker/config.toml.
#
# The config dir is resolved via WINTER_EXT_CONFIG_DIR (set by winter on every
# orchestrator dispatch) or falls back to
# <workspace-root>/.winter/config/winter-service-docker/ when unset.
#
# Validate with:
#   winter ext verify <ext-dir>

# Prefix for COMPOSE_PROJECT_NAME: <project_prefix>-<env>
# e.g. "myapp" produces "myapp-alpha", "myapp-beta", "myapp-workspace"
project_prefix = "myapp"

# Path to the compose file for per-env (project-scoped) services.
# Contains only project-scoped services; independently runnable by hand.
# Relative paths are resolved relative to this config dir.
# Absolute paths are used as-is.
environment_compose_file = "environment-compose.yaml"

# Path to the compose file for workspace-scoped singleton services.
# Contains only workspace-scoped services; independently runnable by hand.
# Relative paths are resolved relative to this config dir.
# Absolute paths are used as-is.
workspace_compose_file = "workspace-compose.yaml"

# ---------------------------------------------------------------------------
# Services — declare every service you want winter to manage.
#
# Fields:
#   name  (required) — unique identifier used in `winter service restart` and
#                      `winter service logs` patterns.  Names are globally
#                      unique across both scopes.
#   scope (optional, default "project") — "project" runs the service in the
#                      per-env <project_prefix>-<env> compose project (declared
#                      in environment_compose_file); "workspace" makes it a
#                      workspace-scoped singleton that runs in
#                      <project_prefix>-workspace (declared in
#                      workspace_compose_file, shared across all feature envs).
#                      Drive workspace services with `winter service up/down workspace`.
#
# Port substitution (WSD_PORT_<NAME> convention):
#   Published host ports in environment-compose.yaml should use ${WSD_PORT_<NAME>}
#   placeholders where <NAME> is the upper-cased service name.
#   winter-service-docker resolves WSD_PORT_<NAME> = WINTER_PORT_BASE + <position>
#   at runtime, where <position> is the 0-based declaration order among
#   PROJECT-scoped [[service]] entries (workspace-scoped entries are excluded
#   from port assignment because they have no WINTER_PORT_BASE).
#   Reordering project entries reassigns ports.
#   Example: backend (project, position 0) → WINTER_PORT_BASE + 0
#   Workspace-scoped services must use fixed host ports (or omit port
#   publishing) — no WSD_PORT_* is emitted for them.
# ---------------------------------------------------------------------------

[[service]]
name = "backend"
# scope = "project"  # default — runs per-env in <project_prefix>-<env>

[[service]]
# Workspace-scoped singleton: runs once in <project_prefix>-workspace,
# shared across all feature envs.  No WSD_PORT_DB is emitted — use a
# fixed host port in workspace-compose.yaml instead.
name = "db"
scope = "workspace"
"""

# ---------------------------------------------------------------------------
# Scaffold logic
# ---------------------------------------------------------------------------

_FILES: dict[str, str] = {
    "environment-compose.yaml": _ENVIRONMENT_COMPOSE_YAML,
    "workspace-compose.yaml": _WORKSPACE_COMPOSE_YAML,
    "config.toml": _CONFIG_TOML,
}


def scaffold(dest: Path, *, force: bool = False) -> list[Path]:
    """Write starter files into *dest*.

    Returns the list of paths written.  Raises ``FileExistsError`` if any
    target file already exists and *force* is ``False``.
    """
    dest.mkdir(parents=True, exist_ok=True)

    # Pre-flight: check for collisions before writing any file.
    if not force:
        existing = [dest / name for name in _FILES if (dest / name).exists()]
        if existing:
            paths = ", ".join(str(p) for p in existing)
            raise FileExistsError(f"Files already exist (use --force to overwrite): {paths}")

    written: list[Path] = []
    for name, content in _FILES.items():
        target = dest / name
        target.write_text(content, encoding="utf-8")
        written.append(target)
    return written


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m docker_orchestrator.scaffold",
        description=(
            "Generate a starter compose.yaml and config.toml for winter-service-docker. "
            "Place config.toml at workspace:/.winter/config/winter-service-docker/config.toml."
        ),
    )
    p.add_argument(
        "dest",
        metavar="DEST_DIR",
        help="Directory to write compose.yaml and config.toml into (created if absent).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Overwrite existing files without prompting.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    dest = Path(args.dest)

    try:
        written = scaffold(dest, force=args.force)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for path in written:
        print(f"wrote {path}")
    print(
        f"\nNext steps:\n"
        f"  1. Edit {dest / 'config.toml'} — set project_prefix and list your services.\n"
        f"  2. Edit {dest / 'environment-compose.yaml'} — per-env services and ports.\n"
        f"  3. Edit {dest / 'workspace-compose.yaml'} — workspace singleton services.\n"
        f"  4. Register: add '[capabilities] service = \"winter-service-docker\"'\n"
        f"     to workspace:/.winter/config.toml.\n"
        f"  5. Run: winter service up workspace && winter service up <env>"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
