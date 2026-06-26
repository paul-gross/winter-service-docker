# Per-env isolation and port substitution

## Two scope-pure compose files

Each workspace config declares **two compose files** in `config.toml`:

- `environment_compose_file` — per-env (project-scoped) services only; run under `<project_prefix>-<env>`.
- `workspace_compose_file` — workspace-scoped singleton services only; run under `<project_prefix>-workspace`.

Each file is independently runnable by hand. Winter-cli core injects `WINTER_PORT_BASE` and all `[env.vars]` entries into the provider subprocess environment before `up`, `down`, and `status` invocations — see `winter-service-docker:/ai/provider-contract.md#environment-variable-injection` for the full contract. To reproduce manually, source `winter env <scope>` first:

```bash
# Feature env (e.g. alpha):
source <(winter env alpha)
docker compose -p myapp-alpha -f environment-compose.yaml up -d

# Workspace singletons:
source <(winter env workspace)
docker compose -p myapp-workspace -f workspace-compose.yaml up -d
```

The orchestrator selects the correct file by scope at runtime so `up/down/restart/logs` always operate on the scope-pure file — no per-service-name masking is needed.

## COMPOSE_PROJECT_NAME namespacing

Every compose invocation sets `COMPOSE_PROJECT_NAME=<project_prefix>-<env>` (e.g. `myapp-alpha`, `myapp-beta`). This namespaces all containers, networks, and volumes per env so two feature environments never conflict on the same docker host.

## WSD_PORT_* port-substitution convention

Published host ports in `environment-compose.yaml` should use `${WSD_PORT_<NAME>}` placeholders (where `<NAME>` is the upper-cased service name). `winter-service-docker` derives per-env ports from `WINTER_PORT_BASE` (injected into the provider subprocess environment by winter-cli core) so host ports are unique across envs.

The offset is the 0-based declaration order among **project-scoped** `[[service]]` entries in `config.toml` — i.e. `WSD_PORT_<NAME> = WINTER_PORT_BASE + <position>` (workspace-scoped entries are excluded from port assignment because they have no `WINTER_PORT_BASE`). **Reordering project entries reassigns ports.** Example: with `WINTER_PORT_BASE=4060` (gamma env), the first declared project service gets `4060`, the second `4061`. Alpha (port_base=4020) and beta (4040) get different host ports automatically.

## Environment variable injection

Winter-cli core injects the full env map into the provider subprocess for `up`, `down`, and `status`. The injected set includes `WINTER_PORT_BASE`, `WINTER_WORKSPACE_PORT_BASE`, and any custom `[env.vars]` entries from the workspace `config.toml`. For `restart` and `logs`, core injects only the four base extension vars; these actions operate on already-provisioned containers and do not need `WINTER_PORT_BASE`. The compose file can reference any of these directly — `${WINTER_PORT_BASE}`, `${DATABASE_URL}`, etc. — without shell arithmetic or file sourcing. Declare workspace-level variables in `[env.vars]` so they are available to all providers for `up`/`down`/`status`.

See `winter-service-docker:/ai/provider-contract.md#environment-variable-injection` for the full contract.

See `winter-service-docker:/workflow/config.toml.example` for the annotated schema.
