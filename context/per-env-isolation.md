# Per-env isolation and port substitution

## Two scope-pure compose files

Each workspace config declares **two compose files** in `config.toml`:

- `environment_compose_file` — per-env (project-scoped) services only; run under `<project_prefix>-<env>`.
- `workspace_compose_file` — workspace-scoped singleton services only; run under `<project_prefix>-workspace`.

Each file is independently runnable by hand. Winter-cli core injects `WINTER_PORT_BASE` and the scope's env-var band entries (`[env.workspace.vars]` / `[env.feature.vars]`) into the provider subprocess environment before `up`, `down`, and `status` invocations — see `winter-service-docker:/context/provider-contract.md#environment-variable-injection` for the full contract. To reproduce manually, source `winter env <scope>` first:

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

The orchestrator namespaces every compose project per env, so two feature environments never conflict on the same docker host — no authoring step is required. For the `<project_prefix>-<env>` naming scheme, see `winter-service-docker:/context/provider-contract.md#compose_project_name-namespacing`.

## WSD_PORT_* port-substitution convention

Publish host ports in `environment-compose.yaml` with `${WSD_PORT_<NAME>}` placeholders (where `<NAME>` is the upper-cased service name); the orchestrator derives a unique per-env value for each, so host ports never collide across envs. For the derivation formula (declaration-order offset, workspace exclusion, the reordering caveat), see `winter-service-docker:/context/provider-contract.md#wsd_port_-port-substitution-scheme`.

## Environment variable injection

The compose file can reference any injected variable directly — `${WINTER_PORT_BASE}`, `${DATABASE_URL}`, etc. — without shell arithmetic or file sourcing. Declare workspace-level variables in `[env.workspace.vars]` and per-env variables in `[env.feature.vars]` in the workspace `config.toml`. For which variables are injected on which actions, see `winter-service-docker:/context/provider-contract.md#environment-variable-injection`.

See `winter-service-docker:/workflow/config.toml.example` for the annotated schema.
