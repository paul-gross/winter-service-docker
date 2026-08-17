# Winter service orchestration via docker compose — hub

## Feature environment setup steps

After `winter ws init` clones the extension, walk the user through [workflow-setup.md](./workflow-setup.md) to scaffold and populate `workspace:/.winter/config/winter-service-docker/` — the `config.toml` and compose files every feature worktree needs.

## Reference

| Topic | Read when… |
|-------|------------|
| [Per-env isolation and port substitution](./per-env-isolation.md) | …configuring `COMPOSE_PROJECT_NAME` namespacing or `WSD_PORT_*` placeholders in `environment-compose.yaml` |
| [Workspace-scoped singleton services](./workspace-singletons.md) | …running or declaring shared services (db, broker) once for the whole workspace via `winter service … workspace` |
| [Testing changed orchestrator code](./dev-loop.md) | …exercising in-progress changes via `--service-orchestrator` or the manual fallback, or checking the doctor probe contract |
| [Provider wire contract](./provider-contract.md) | …understanding docker-state → winter-state mapping, port scheme internals, or `docker logs` flag pass-through |
| [README.md](../README.md) | …installing the extension or binding the `service` capability slot in `.winter/config.toml` |
