# Docker-specific provider contract for winter-service-docker

This extension implements winter's **service orchestrator provider** contract. Read the canonical contract first — `workspace:/context/winter-cli/contracts/service-orchestrator.md` owns the generic wire contract; consult it whenever you need the action argv, exit codes, the `status`/`describe` JSON shapes, the `logs` NDJSON shape, or the `WINTER_*` dispatch-var rules. This doc is the docker-specific overlay on top of that contract — it covers only what is specific to the docker compose implementation.

For the doctor-probe contract (NDJSON shape, `pass`/`warn`/`fail` semantics, exit-code rules), see `workspace:/context/winter-cli/configuration/doctor.md#probe-output-contract`.

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

Every compose invocation sets `COMPOSE_PROJECT_NAME=<prefix>-<env>`, where:

- `<prefix>` is resolved by `docker_orchestrator.env_context.resolve_project_prefix`: the optional `project_prefix` key in `config.toml`, when set, is an explicit per-provider override that takes precedence; otherwise `<prefix>` falls back to `WINTER_SERVICE_PREFIX`, injected into the process environment by winter-cli core (resolved from the workspace-level `service_prefix` config, defaulting to `"winter"`). `WINTER_SERVICE_PREFIX` is a base extension var, present on every dispatch action (`up`, `down`, `status`, `restart`, `logs`, `describe`, `catalog`), so `project_prefix` is purely optional — set it only if a workspace wants this provider specifically to use a different prefix than the rest of the workspace (e.g. to avoid a naming collision), not as a fallback for any action.
- `<env>` is the environment name (`alpha`, `beta`, …) or the reserved literal `workspace`.

This namespaces all containers, networks, and volumes per env so two feature environments never conflict on the same docker host. The workspace scope uses `<prefix>-workspace` as its compose project name.

## WSD_PORT_* port-substitution scheme

Host ports in `environment-compose.yaml` use `${WSD_PORT_<NAME>}` placeholders. The orchestrator reads `WINTER_PORT_BASE` from the process environment (injected by winter-cli core before invoking the provider subprocess) and computes:

```
WSD_PORT_<NAME> = WINTER_PORT_BASE + <position>
```

where `<position>` is the 0-based index of the service's `[[service]]` entry among **project-scoped** entries in `config.toml` (declaration order; workspace-scoped entries are excluded because they have no `WINTER_PORT_BASE`). Reordering project entries reassigns ports. Two feature environments never collide because each env's `WINTER_PORT_BASE` is unique.

## Environment variable injection

Which `WINTER_*` vars (and computed env-band entries) core injects on each action is owned by `workspace:/context/winter-cli/contracts/service-orchestrator.md#always-present-environment-variables` — `WINTER_SERVICE_PREFIX` is one of the base extension vars, present on every action (`up`, `down`, `status`, `restart`, `logs`, `describe`, `catalog`); the scope vars (`WINTER_ENV`, `WINTER_ENV_INDEX`, `WINTER_PORT_BASE`, `WINTER_WORKSPACE_PORT_BASE`, plus the band entries) additionally land on `up`/`down`/`status` only. This provider's docker-specific behavior over that contract:

- It reads the injected vars from the process environment via `os.environ` — it does not locate, open, parse, or shell-source any per-env file — and passes them through as the subprocess environment to `docker compose` alongside the computed `COMPOSE_PROJECT_NAME` and `WSD_PORT_*` values. Arbitrary workspace variables referenced in the compose file (e.g. `${DATABASE_URL}`, `${WTS_DB_PORT}`) interpolate the same way; declare them in `[env.feature.vars]` / `[env.workspace.vars]` in the workspace `config.toml`.
- Because `restart`/`logs` run without `WINTER_PORT_BASE`, `docker compose` may emit benign `"variable is not set"` warnings for `${WSD_PORT_*}`/`${WINTER_PORT_BASE}` references in the compose file; these are expected and safe to ignore — `restart`/`logs` act on already-created containers and do not re-publish ports.
- `restart`/`logs` still receive `WINTER_SERVICE_PREFIX` (it's a base extension var, not a scope var), so `COMPOSE_PROJECT_NAME` resolves the same way for those actions as it does for `up`/`down`/`status` — no `project_prefix` override is needed.

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

The workspace scope drives a separate `<prefix>-workspace` compose project. Services are partitioned by the per-service `scope` field in `config.toml`: `scope = "project"` (default, per-env) or `scope = "workspace"` (singleton shared across all envs). The loader splits `[[service]]` entries into the project partition and the workspace partition at parse time; every verb (`up`, `down`, `restart`, `logs`, `status`) calls `services_for_scope(env)` to select the appropriate partition. Workspace-scoped services have `port_base = None` and receive no `WSD_PORT_*` substitution variables; however, core injects `WINTER_WORKSPACE_PORT_BASE` into the process environment for the workspace scope, so `workspace-compose.yaml` can reference `${WINTER_WORKSPACE_PORT_BASE}` (e.g. `"${WINTER_WORKSPACE_PORT_BASE}:5432"` → `4000:5432`) directly. Names are globally unique across both scopes (enforced at load time). Named volumes declared in `workspace-compose.yaml` persist across `compose down`; `down workspace` is an authoritative `docker compose down` for those containers but does not remove volumes. Remove volumes explicitly with `docker volume rm ...` if a clean slate is needed.

**Empty-scope no-op.** When `services_for_scope(env)` returns no services for a scope — e.g. a workspace-only manifest (`environment_compose_file` unset, every service `scope = "workspace"`) invoked for a feature env — `up` and `down` exit `0` without running compose, emitting a `nothing to start` / `nothing to tear down` diagnostic to stderr. This is a deliberate success, not a swallowed failure: an orchestrator fanning `up <env>` across scopes must read the empty per-env scope as intentionally skipped. `up` and `down` differ on one point: `down` still runs `compose down` when `environment_compose_file` **is** configured even with zero declared services (to reap containers a prior `up` against an earlier manifest may have started), whereas `up` never starts services it doesn't declare.

**Service-segment patterns (partial up/down).** `<env>` may additionally be a scope-qualified `<scope>/<svc-pattern>` glob (e.g. `alpha/api`, `alpha/wor*`), expanded against the scope's declared services the same way `restart`/`logs`/`status` patterns are. A bare scope or `<scope>/*` is unchanged from the whole-project behavior described above. A concrete or glob service segment targets only the matched subset:

- `up` passes the matched names straight to `compose up -d <svc>...` — compose targets services natively on `up`, so this is a normal partial start.
- `down` cannot target services at the project level (`compose down` is project-scoped), so a partial `down` instead issues `compose stop <svc>...` followed by `compose rm -f <svc>...` against only the matched subset. This is deliberately narrower than whole-scope `down`: it leaves unmatched containers running, and — unlike `compose down` — leaves the project's networks and named volumes untouched regardless of which services were matched.
- The `up --wait` readiness gate (the `compose ps` polling described above) also scopes to the matched subset when a partial pattern is given, so `--wait` reports readiness for the requested services only, not the whole scope.
