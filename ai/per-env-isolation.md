# Per-env isolation and port substitution

## COMPOSE_PROJECT_NAME namespacing

Every compose invocation sets `COMPOSE_PROJECT_NAME=<project_prefix>-<env>` (e.g. `myapp-alpha`, `myapp-beta`). This namespaces all containers, networks, and volumes per env so two feature environments never conflict on the same docker host.

## WSD_PORT_* port-substitution convention

Published host ports in `compose.yaml` should use `${WSD_PORT_<NAME>}` placeholders (where `<NAME>` is the upper-cased service name). `winter-service-docker` derives per-env ports from `WINTER_PORT_BASE` (read from `<workspace>/<env>/.winter.env`) so host ports are unique across envs.

The offset is the 0-based declaration order among **project-scoped** `[[service]]` entries in `config.toml` — i.e. `WSD_PORT_<NAME> = WINTER_PORT_BASE + <position>` (workspace-scoped entries are excluded from port assignment because they have no `WINTER_PORT_BASE`). **Reordering project entries reassigns ports.** Example: with `WINTER_PORT_BASE=4060` (gamma env), the first declared project service gets `4060`, the second `4061`. Alpha (port_base=4020) and beta (4040) get different host ports automatically.

See `winter-service-docker:/workflow/config.toml.example` for the annotated schema.
