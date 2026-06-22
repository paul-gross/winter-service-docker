#!/usr/bin/env sh
# on_env_init hook for winter-service-docker.
#
# Invoked by `winter ws init` for every standalone repo whose winter-ext.toml
# declares `[hooks] on_env_init`. The CLI runs this script with cwd set to
# the env root and provides:
#   WINTER_EXT_DIR        — this extension's repo path
#   WINTER_EXT_PREFIX     — resolved symlink prefix (e.g. "wsd")
#   WINTER_WORKSPACE_DIR
#   WINTER_ENV
#   WINTER_ENV_INDEX
#   WINTER_PORT_BASE
#
# This hook is intentionally minimal: docker-based services are started
# explicitly via `winter service up <env>` after the env is initialized.
# No containers are auto-started here to avoid unintended resource usage.
set -eu

: "${WINTER_ENV:?WINTER_ENV not set}"

# Friendly note; nothing else needed at env-init time.
echo "winter-service-docker: env '${WINTER_ENV}' initialized. Run 'winter service up ${WINTER_ENV}' to start services." >&2
