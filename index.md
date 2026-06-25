# Winter service orchestration via docker compose

Docker compose-based service orchestration for winter workspaces. Maps `winter service` verbs onto `docker compose` commands, with per-env isolation derived from winter's env-index and port registry. Multiple feature environments run their own compose projects side-by-side without port or namespace conflicts.

## Path notation

Files in this extension are addressed with the `winter-service-docker:` prefix — for example, `winter-service-docker:/index.md`. Resolve to the on-disk path via the `# Winter Extensions` block in workspace `CLAUDE.md`.

## What this extension provides

`winter-service-docker` implements winter's `service` capability slot using `docker compose`. Declare it in `.winter/config.toml` alongside (or instead of) `winter-service-tmux`. Because this provider reports real container health via docker's healthcheck, `winter service up <env> --wait` is a genuine readiness gate. See `README.md` for installation steps.

| Topic | Read when… |
|-------|------------|
| [Per-env isolation and port substitution](./ai/per-env-isolation.md) | …configuring `COMPOSE_PROJECT_NAME` namespacing or `WSD_PORT_*` placeholders in `environment-compose.yaml` |
| [Workspace-scoped singleton services](./ai/workspace-singletons.md) | …running shared services (db, broker) once for the whole workspace via `winter service … workspace` |
| [Testing changed orchestrator code](./ai/dev-loop.md) | …exercising in-progress changes via `--service-orchestrator` or the manual fallback, or checking the doctor probe contract |
| [Provider wire contract](./ai/provider-contract.md) | …understanding docker-state → winter-state mapping, port scheme internals, or `docker logs` flag pass-through |
