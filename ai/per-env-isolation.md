# Per-env isolation and port substitution

## Two scope-pure compose files

Each workspace config declares **two compose files** in `config.toml`:

- `environment_compose_file` — per-env (project-scoped) services only; run under `<project_prefix>-<env>`.
- `workspace_compose_file` — workspace-scoped singleton services only; run under `<project_prefix>-workspace`.

Each file is independently runnable by hand. The orchestrator sources the winter env file in a shell before invoking compose — see `winter-service-docker:/ai/provider-contract.md#env-file-sourcing` for the exact mechanism and precedence rules. To reproduce:

```bash
# Feature env (e.g. alpha):
set -a; . alpha/.winter.env; set +a
docker compose -p myapp-alpha -f environment-compose.yaml up -d

# Workspace singletons:
set -a; . .winter.workspace.env; set +a
docker compose -p myapp-workspace -f workspace-compose.yaml up -d
```

The orchestrator selects the correct file by scope at runtime so `up/down/restart/logs` always operate on the scope-pure file — no per-service-name masking is needed.

## COMPOSE_PROJECT_NAME namespacing

Every compose invocation sets `COMPOSE_PROJECT_NAME=<project_prefix>-<env>` (e.g. `myapp-alpha`, `myapp-beta`). This namespaces all containers, networks, and volumes per env so two feature environments never conflict on the same docker host.

## WSD_PORT_* port-substitution convention

Published host ports in `environment-compose.yaml` should use `${WSD_PORT_<NAME>}` placeholders (where `<NAME>` is the upper-cased service name). `winter-service-docker` derives per-env ports from `WINTER_PORT_BASE` (read from `<workspace>/<env>/.winter.env`) so host ports are unique across envs.

The offset is the 0-based declaration order among **project-scoped** `[[service]]` entries in `config.toml` — i.e. `WSD_PORT_<NAME> = WINTER_PORT_BASE + <position>` (workspace-scoped entries are excluded from port assignment because they have no `WINTER_PORT_BASE`). **Reordering project entries reassigns ports.** Example: with `WINTER_PORT_BASE=4060` (gamma env), the first declared project service gets `4060`, the second `4061`. Alpha (port_base=4020) and beta (4040) get different host ports automatically.

## Env-file sourcing

`WSD_PORT_*` is not the only way to feed ports into `environment-compose.yaml`. Before every compose invocation the orchestrator also **sources** the scope's winter env file, so a compose file can reference any variable that file defines — `${WINTER_PORT_BASE}`, a project-seeded `${WTS_DB_PORT}`, `${DATABASE_URL}`, etc. See `winter-service-docker:/ai/provider-contract.md#env-file-sourcing` for the full contract (which file per scope, sourcing-vs-parsing, precedence).

See `winter-service-docker:/workflow/config.toml.example` for the annotated schema.
