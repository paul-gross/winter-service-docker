# Winter service orchestration via docker compose

Docker compose-based service orchestration for winter workspaces. Maps `winter service` verbs onto `docker compose` commands, with per-env isolation derived from winter's env-index and port registry. Multiple feature environments run their own compose projects side-by-side without port or namespace conflicts.

## Path notation

Files in this extension are addressed with the `winter-service-docker:` prefix — for example, `winter-service-docker:/index.md`. Resolve to the on-disk path via the `# Winter Extensions` block in workspace `CLAUDE.md`.

## Feature environment setup steps

This extension needs a project-specific `config.toml` and two compose files (`environment-compose.yaml` for per-env services, `workspace-compose.yaml` for workspace singletons) wired to your project's services. After `winter ws init` clones the extension, walk the user through [context/workflow-setup.md](./context/workflow-setup.md) to scaffold and populate `workspace:/.winter/config/winter-service-docker/`. Without these, `winter service up <env>` has no services to start.

## What this extension provides

`winter-service-docker` implements winter's `service` capability slot using `docker compose`. Declare it in `.winter/config.toml` alongside (or instead of) `winter-service-tmux`. Because this provider reports real container health via docker's healthcheck, `winter service up <env> --wait` is a genuine readiness gate. See `README.md` for installation steps.

| Topic | Read when… |
|-------|------------|
| [Per-env isolation and port substitution](./context/per-env-isolation.md) | …configuring `COMPOSE_PROJECT_NAME` namespacing or `WSD_PORT_*` placeholders in `environment-compose.yaml` |
| [Workspace-scoped singleton services](./context/workspace-singletons.md) | …running shared services (db, broker) once for the whole workspace via `winter service … workspace` |
| [Testing changed orchestrator code](./context/dev-loop.md) | …exercising in-progress changes via `--service-orchestrator` or the manual fallback, or checking the doctor probe contract |
| [Provider wire contract](./context/provider-contract.md) | …understanding docker-state → winter-state mapping, port scheme internals, or `docker logs` flag pass-through |
