# Docker-specific provider contract for winter-service-docker

This extension implements winter's **service orchestrator provider** contract. The generic wire contract (action argv, exit codes, `status` JSON shape, `logs` NDJSON shape, `describe` JSON, `WINTER_*` dispatch vars) is documented in `workspace:/ai/winter-cli/usage/service.md` — read that file first. This doc covers only what is specific to the docker compose implementation.

For the doctor-probe contract (NDJSON shape, `pass`/`warn`/`fail` semantics, exit-code rules), see `workspace:/ai/winter-cli/setup.md`.

## Docker-state → winter-state/health mapping

`status` emits winter's env-keyed status document. This provider maps docker container state to the `state`/`health` fields as follows:

| Docker state | Container healthcheck | `state` | `health` |
|---|---|---|---|
| `running` | healthy | `running` | `healthy` |
| `running` | unhealthy | `running` | `unhealthy` |
| `running` | none | `running` | `unknown` |
| `exited` or `created` | any | `stopped` | `unknown` |
| absent / unknown | — | `stopped` | `unknown` |

`winter service up <env> --wait` polls this `health` field. Because docker healthchecks are real container probes (not tmux pane heuristics), `--wait` is a genuine readiness gate with this provider.

## COMPOSE_PROJECT_NAME namespacing

Every compose invocation sets `COMPOSE_PROJECT_NAME=<project_prefix>-<env>`, where:

- `<project_prefix>` is the `project_prefix` key in `config.toml`.
- `<env>` is the environment name (`alpha`, `beta`, …) or the reserved literal `workspace`.

This namespaces all containers, networks, and volumes per env so two feature environments never conflict on the same docker host. The workspace scope uses `<project_prefix>-workspace` as its compose project name.

## WSD_PORT_* port-substitution scheme

Host ports in `environment-compose.yaml` use `${WSD_PORT_<NAME>}` placeholders. At `up` time, the orchestrator reads `WINTER_PORT_BASE` from `<workspace>/<env>/.winter.env` and exports:

```
WSD_PORT_<NAME> = WINTER_PORT_BASE + <position>
```

where `<position>` is the 0-based index of the service's `[[service]]` entry among **project-scoped** entries in `config.toml` (declaration order; workspace-scoped entries are excluded because they have no `WINTER_PORT_BASE`). Reordering project entries reassigns ports. Two feature environments never collide because each env's `WINTER_PORT_BASE` is unique.

## Env-file sourcing

The orchestrator's env-file sourcing behaviour differs by action:

### `up`, `down`, `restart`, `logs` — provider sources the env file

For these actions the provider **sources the scope's winter env file** in a shell before exec'ing each `docker compose` invocation:

- feature env → `<workspace>/<env>/.winter.env`
- workspace scope → `<workspace>/.winter.workspace.env`

The file is sourced with `set -a` (allexport) and then the real command is exec'd, so every variable the file assigns is exported into compose's environment and available for `${VAR}` interpolation in `compose.yaml`. The file is sourced, not parsed, so it may contain shell arithmetic — e.g. `WTS_DB_PORT=$(( WINTER_PORT_BASE + 12 ))` — evaluated by the shell. This mirrors how winter-service-tmux `source`s the same file for each service pane, making the two orchestrators consume the env file identically. <!-- winter-lint:example --> When the file is absent, sourcing is skipped and compose runs with the unmodified process environment.

**Precedence over computed `WSD_PORT_*`/`COMPOSE_PROJECT_NAME`.** Both channels apply to the same project-scoped invocation: the computed values (`COMPOSE_PROJECT_NAME` and the `WSD_PORT_*` set) arrive as the subprocess environment, and the env file is then sourced *on top* of them. So if a key is assigned in both, **the sourced file wins**. Winter's own seeded `.winter.env`/`.winter.workspace.env` deliberately assign neither `COMPOSE_PROJECT_NAME` nor any `WSD_PORT_*`, so the namespacing and port-derivation invariants hold in practice — but a hand-edited env file that reassigns one of those keys would override winter's computed value. Don't put winter-owned keys in the env file.

This is the recommended path for **workspace** services: they have no `WSD_PORT_*` (see below), so they reference `${WINTER_PORT_BASE}` (and any other vars) straight from the sourced `.winter.workspace.env`.

### `status` — core sources the env file; provider reads injected vars

For `status`, env-file sourcing is the responsibility of **winter-cli core**, not this provider. Core invokes the provider once per scope as `status <scope>/*` (e.g. `status alpha/*` or `status workspace/*`) with the scope's env-file vars already present in the process environment. The authoritative list of injected variables is in `workspace:/ai/winter-cli/usage/service.md` (status wire contract); this provider reads them from `os.environ` and does **not** locate, read, or shell-source `.winter.env` on the status path. It still computes `COMPOSE_PROJECT_NAME` and `WSD_PORT_*` from the injected `WINTER_PORT_BASE`, but passes these as the subprocess environment to `docker compose ps` rather than using the bash source wrapper.

## `docker logs` flag pass-through

`winter service logs` appends the render options as CLI flags after the positional `<env>/<service>` patterns; the `logs` action parses them off argv (in `read_log_options`) and maps them onto `docker compose logs`:

```
<entrypoint> logs <pattern...> [-f|--follow] [-n|--tail <N|all>] \
  [--since <rfc3339>] [--until <rfc3339>] [-t|--timestamps]
```

- `-f`/`--follow` → `--follow`
- `-n`/`--tail <N|all>` → `--tail <N|all>` (carried as-is; docker accepts `all`)
- `--since <rfc3339>` → `--since <ts>` (consumed as-is; winter does the duration parsing)
- `--until <rfc3339>` → `--until <ts>`
- `-t`/`--timestamps` → accepted and discarded; `--timestamps` is **always** passed to docker so the `ts` field can be populated

Winter re-applies its own tail/time backstop, so faithfully streaming docker's output is sufficient.

## Workspace-scope model and named volumes

The workspace scope drives a separate `<project_prefix>-workspace` compose project. Services are partitioned by the per-service `scope` field in `config.toml`: `scope = "project"` (default, per-env) or `scope = "workspace"` (singleton shared across all envs). The loader splits `[[service]]` entries into the project partition and the workspace partition at parse time; every verb (`up`, `down`, `restart`, `logs`) calls `services_for_scope(env)` to select the appropriate partition (`status` is handled separately — see [Env-file sourcing](#env-file-sourcing)). Workspace-scoped services have `port_base = None` and receive no `WSD_PORT_*` substitution variables; instead, the orchestrator sources `<workspace>/.winter.workspace.env` (the index-0 band that `winter ws init` seeds) before each compose call, so `workspace-compose.yaml` references `${WINTER_PORT_BASE}` (e.g. `"${WINTER_PORT_BASE}:5432"` → `4000:5432`) directly from that file — see [Env-file sourcing](#env-file-sourcing). Names are globally unique across both scopes (enforced at load time). Named volumes declared in `workspace-compose.yaml` persist across `compose down`; `down workspace` is an authoritative `docker compose down` for those containers but does not remove volumes. Remove volumes explicitly with `docker volume rm ...` if a clean slate is needed.
