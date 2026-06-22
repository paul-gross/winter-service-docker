"""Starter-config scaffolder for winter-service-docker.

Writes two files into a destination directory:
  - ``compose.yaml``   — a demo compose file using ``${WSD_PORT_*}`` port substitution
  - ``config.toml``    — a starter extension manifest for ``winter-service-docker``

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

_COMPOSE_YAML = """\
# Starter compose.yaml for winter-service-docker.
#
# Port substitution uses ${WSD_PORT_<NAME>} placeholders.  winter-service-docker
# reads each declared service's port offset from config.toml and resolves these
# at runtime from the env's WINTER_PORT_BASE so two envs never collide on the
# same host port.
#
# Named volumes persist data across `docker compose down` and survive
# `winter ws destroy` (managed separately; the destroy hook runs compose down
# but does not remove volumes).

services:
  backend:
    image: your-backend-image:latest
    ports:
      - "${WSD_PORT_BACKEND}:8080"
    environment:
      DATABASE_URL: "postgresql://app:app@db:5432/app"
    depends_on:
      db:
        condition: service_healthy

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
      POSTGRES_DB: app
    ports:
      - "${WSD_PORT_DB}:5432"
    volumes:
      - db-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app"]
      interval: 5s
      timeout: 3s
      retries: 10

volumes:
  db-data:
    # Named volume: persists across compose down; shared by all containers in
    # this project. Use the workspace compose project for a singleton
    # db that persists across all feature envs (see index.md).
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
# e.g. "myapp" produces "myapp-alpha", "myapp-beta"
project_prefix = "myapp"

# Path to the compose file consumed by this orchestrator.
# Relative paths are resolved relative to this config dir.
# Absolute paths are used as-is.
compose_file = "compose.yaml"

# ---------------------------------------------------------------------------
# Services — declare every service you want winter to manage.
#
# Fields:
#   name  (required) — unique identifier used in `winter service restart` and
#                      `winter service logs` patterns.
#
# Port substitution (WSD_PORT_<NAME> convention):
#   Published host ports in compose.yaml should use ${WSD_PORT_<NAME>}
#   placeholders where <NAME> is the upper-cased service name.
#   winter-service-docker resolves WSD_PORT_<NAME> = WINTER_PORT_BASE + <position>
#   at runtime, where <position> is the 0-based declaration order of the
#   [[service]] entry in this file.  Reordering entries reassigns ports.
#   Example: backend (position 0) → WINTER_PORT_BASE + 0
#            db      (position 1) → WINTER_PORT_BASE + 1
# ---------------------------------------------------------------------------

[[service]]
name = "backend"

[[service]]
name = "db"
"""

# ---------------------------------------------------------------------------
# Scaffold logic
# ---------------------------------------------------------------------------

_FILES: dict[str, str] = {
    "compose.yaml": _COMPOSE_YAML,
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
            raise FileExistsError(
                f"Files already exist (use --force to overwrite): {paths}"
            )

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
        f"  2. Edit {dest / 'compose.yaml'} — configure your images and ports.\n"
        f"  3. Register: add '[capabilities] service = \"winter-service-docker\"'\n"
        f"     to workspace:/.winter/config.toml.\n"
        f"  4. Run: winter service up <env>"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
