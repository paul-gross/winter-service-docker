#!/usr/bin/env sh
# on_env_destroy hook for winter-service-docker.
#
# Invoked by `winter ws destroy` for every standalone repo whose
# winter-ext.toml declares `[hooks] on_env_destroy`. The CLI runs this script
# with cwd set to the env root (the dir about to be torn down) and provides:
#   WINTER_EXT_DIR        — this extension's repo path
#   WINTER_EXT_PREFIX     — resolved symlink prefix (e.g. "wsd")
#   WINTER_WORKSPACE_DIR
#   WINTER_ENV
#   WINTER_ENV_INDEX
#   WINTER_PORT_BASE
#
# Best-effort: runs `docker compose down` for the env's compose project to clean
# up containers and networks. Guards on docker being available; never hard-fails
# env destroy (exits 0 even on docker errors).
#
# Idempotent: if the project is already stopped or never started, docker compose
# down is a no-op and exits 0.
set -u

: "${WINTER_ENV:?WINTER_ENV not set}"
: "${WINTER_WORKSPACE_DIR:?WINTER_WORKSPACE_DIR not set}"

# Read config to determine project_prefix and compose_file.
# Config dir is resolved via WINTER_EXT_CONFIG_DIR or falls back to the default.
if [ -n "${WINTER_EXT_CONFIG_DIR:-}" ]; then
  CONFIG_DIR="$WINTER_EXT_CONFIG_DIR"
else
  CONFIG_DIR="$WINTER_WORKSPACE_DIR/.winter/config/winter-service-docker"
fi

# Guard: if docker is not available, skip silently.
if ! command -v docker >/dev/null 2>&1; then
  echo "winter-service-docker: docker not found; skipping compose down for env '${WINTER_ENV}'." >&2
  exit 0
fi

# Guard: if config.toml is missing, skip with a warning.
CONFIG_FILE="$CONFIG_DIR/config.toml"
if [ ! -f "$CONFIG_FILE" ]; then
  echo "winter-service-docker: no config.toml at $CONFIG_DIR; skipping compose down for env '${WINTER_ENV}'." >&2
  exit 0
fi

# Extract project_prefix and compose_file from config.toml (simple grep; reliable for
# top-level scalar fields).
PROJECT_PREFIX=$(grep -E '^[[:space:]]*project_prefix[[:space:]]*=' "$CONFIG_FILE" \
  | sed 's/.*=[[:space:]]*//' | tr -d '"'"'"' ' | head -1)
COMPOSE_FILE=$(grep -E '^[[:space:]]*compose_file[[:space:]]*=' "$CONFIG_FILE" \
  | sed 's/.*=[[:space:]]*//' | tr -d '"'"'"' ' | head -1)

if [ -z "$PROJECT_PREFIX" ]; then
  echo "winter-service-docker: project_prefix not found in config.toml; skipping compose down for env '${WINTER_ENV}'." >&2
  exit 0
fi

if [ -z "$COMPOSE_FILE" ]; then
  echo "winter-service-docker: compose_file not found in config.toml; skipping compose down for env '${WINTER_ENV}'." >&2
  exit 0
fi

PROJECT_NAME="${PROJECT_PREFIX}-${WINTER_ENV}"

# Resolve compose_file: if not absolute, resolve relative to config dir.
case "$COMPOSE_FILE" in
  /*) COMPOSE_PATH="$COMPOSE_FILE" ;;
  *)  COMPOSE_PATH="$CONFIG_DIR/$COMPOSE_FILE" ;;
esac

if [ ! -f "$COMPOSE_PATH" ]; then
  echo "winter-service-docker: compose file not found at $COMPOSE_PATH; skipping compose down for env '${WINTER_ENV}'." >&2
  exit 0
fi

echo "winter-service-docker: running docker compose down for project '${PROJECT_NAME}'..." >&2
# Best-effort: || true ensures destroy never aborts on a docker error.
docker compose -p "$PROJECT_NAME" -f "$COMPOSE_PATH" down 2>&1 >&2 || true
