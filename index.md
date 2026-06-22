# Winter service orchestration via docker compose

Docker compose-based service orchestration for winter workspaces. Maps `winter service` verbs onto `docker compose` commands, with per-env isolation derived from winter's env-index and port registry. Multiple feature environments run their own compose projects side-by-side without port or namespace conflicts.

## Path notation

Files in this extension are addressed with the `winter-service-docker:` prefix — for example, `winter-service-docker:/index.md`. Resolve to the on-disk path via the `# Winter Extensions` block in workspace `CLAUDE.md`.

## What this extension provides

`winter-service-docker` implements winter's `service` capability slot using `docker compose`. Declare it in `.winter/config.toml` alongside (or instead of) `winter-service-tmux` — the two coexist under the multi-provider contract. See `workspace:/ai/winter-cli/usage/service.md` for the full dispatch contract; `ai/provider-contract.md` (in-repo) documents the docker-specific wire details this provider must satisfy.

Because this provider reports real container health via docker's healthcheck, `winter service up <env> --wait` is a genuine readiness gate here — it polls until all containers report healthy before returning.

See `README.md` for installation and registration steps.

## Per-env isolation: COMPOSE_PROJECT_NAME

Every compose invocation sets `COMPOSE_PROJECT_NAME=<project_prefix>-<env>` (e.g. `myapp-alpha`, `myapp-beta`). This namespaces all containers, networks, and volumes per env so two feature environments never conflict on the same docker host.

## WSD_PORT_* port-substitution convention

Published host ports in `compose.yaml` should use `${WSD_PORT_<NAME>}` placeholders (where `<NAME>` is the upper-cased service name). `winter-service-docker` derives per-env ports from `WINTER_PORT_BASE` (read from `<workspace>/<env>/.winter.env`) so host ports are unique across envs.

The offset is the 0-based `[[service]]` declaration order in `config.toml` — i.e. `WSD_PORT_<NAME> = WINTER_PORT_BASE + <position>`. **Reordering entries reassigns ports.** Example: with `WINTER_PORT_BASE=4060` (gamma env), the first declared service gets `4060`, the second `4061`. Alpha (port_base=4020) and beta (4040) get different host ports automatically.

See `winter-service-docker:/workflow/config.toml.example` for the annotated schema.

## Workspace-scoped singleton services

The workspace scope uses `<project_prefix>-workspace` as its compose project name. Workspace services live in a separate compose project and are driven by `winter service up/down workspace` — no per-service `scope` field is needed in `config.toml`.

```bash
winter service up workspace          # start all workspace services
winter service down workspace        # stop all workspace services (authoritative compose down)
winter service status workspace      # list workspace service states
winter service restart workspace/db  # restart a single workspace service
```

`winter service up <env>` does **not** auto-start the workspace scope. Run `winter service up workspace` first, or use `winter service up <env>` (which ensures the workspace scope is up before starting the env). `down <env>` intentionally leaves the workspace project running; only `down workspace` tears it down.

Named volumes declared in the workspace `compose.yaml` persist across `compose down`. Remove them explicitly with `docker volume rm ...` if you want a clean slate.

## Doctor probe

`workflow/doctor.sh` runs as part of `winter doctor`, checking that the docker daemon is reachable and compose v2 is installed. See `workspace:/ai/winter-cli/setup.md` for the doctor-probe contract.

## Testing changed orchestrator code against a worktree

The PRIMARY door for exercising in-progress changes is the `--service-orchestrator` override, which sets `WINTER_EXT_DIR`/`WINTER_EXT_PREFIX` for you:

```bash
winter --service-orchestrator=/path/to/gamma/winter-service-docker service describe
winter --service-orchestrator=/path/to/gamma/winter-service-docker service status alpha
```

As a fallback, export the vars manually and invoke the entrypoint directly:

```bash
export WINTER_WORKSPACE_DIR=/path/to/workspace
export WINTER_EXT_DIR=/path/to/gamma/winter-service-docker
export WINTER_EXT_CONFIG_DIR="$WINTER_WORKSPACE_DIR/.winter/config/winter-service-docker"
PYTHONPATH="$WINTER_EXT_DIR/src" python3 "$WINTER_EXT_DIR/workflow/service" describe
```

See `CONTRIBUTING.md` for the full dev-loop (lint, typecheck, test, unit-test how-to).
