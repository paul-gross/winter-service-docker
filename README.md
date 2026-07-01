# ❄️ winter-service-docker

A [winter](https://github.com/paul-gross/winter) extension that adds docker compose-based service orchestration to a workspace. Each feature environment gets its own isolated compose project, so multiple envs can run the full application stack side-by-side on the same docker host without port or namespace conflicts.

📚 **Documentation:** <https://paul-gross.github.io/winter-docs/>

## ✨ Features

- **Per-env compose isolation** — every feature environment gets its own compose project named `<prefix>-<env>` (e.g. `myapp-alpha`, `myapp-beta`). Containers, networks, and volumes are automatically namespaced; no manual configuration needed.
- **Conflict-free parallel runs** — published host ports use `${WSD_PORT_<NAME>}` substitution resolved from `WINTER_PORT_BASE`, so alpha and beta each get unique ports without collision.
- **Workspace-scoped singletons** — shared infrastructure (databases, registries, brokers) runs in a separate `<prefix>-workspace` compose project. Declare a service with `scope = "workspace"` in `config.toml` (defaults to `"project"`) and drive it with `winter service up/down workspace`.
- **Genuine readiness gate** — because this provider reports real container health, `winter service up <env> --wait` blocks until all containers are healthy before returning.
- **Winter service integration** — `winter service up/down/status/restart/logs <env>` drives the full lifecycle. Coexists with `winter-service-tmux` under the multi-provider contract.
- **Built-in doctor probe** — `winter doctor` checks that the docker daemon is reachable and compose v2 is installed, with actionable remediation on failure.
- **Starter scaffolder** — `python3 -m docker_orchestrator.scaffold <dest>` generates starter `environment-compose.yaml`, `workspace-compose.yaml`, and `config.toml` demonstrating the `${WSD_PORT_*}` convention and named volumes.
- **Injectable seam** — all docker/compose calls go through a `ComposeClient` interface; unit tests use a fake (no real daemon required).

## 🚀 Installation & Setup

1. **Scaffold a starter config:**

   ```bash
   PYTHONPATH=src python3 -m docker_orchestrator.scaffold \
       <workspace-root>/.winter/config/winter-service-docker/
   ```

   This creates three files: `environment-compose.yaml` (per-env services), `workspace-compose.yaml` (workspace singletons), and `config.toml`.

2. **Edit `config.toml`** — update `environment_compose_file` and `workspace_compose_file` if needed, and list your `[[service]]` entries. The compose project prefix is controlled entirely by the workspace's `WINTER_SERVICE_PREFIX` (from `service_prefix` in `.winter/config.toml`) — nothing to configure here. (`project_prefix` is a purely optional per-provider override for the rare case where this provider needs a prefix different from the rest of the workspace; see `context/provider-contract.md#compose_project_name-namespacing`.)

3. **Edit `environment-compose.yaml`** — per-env services; use `${WSD_PORT_<NAME>}` for published ports.

4. **Edit `workspace-compose.yaml`** — workspace singleton services; use fixed ports or reference `${WINTER_WORKSPACE_PORT_BASE}` (injected by winter-cli core; available via `source <(winter env workspace)` for manual runs).

5. **Register the extension** in workspace `.winter/config.toml`:

   ```toml
   [capabilities]
   service = "winter-service-docker"

   [[standalone_repository]]
   name = "winter-service-docker"
   url  = "git@github.com:paul-gross/winter-service-docker.git"
   path = ".winter/ext/service-docker"
   ```

   The legacy root-level key `service_orchestrator = "winter-service-docker"` is still accepted as a deprecated alias.

6. **Start services:**

   ```bash
   winter service up workspace   # optional: start shared singletons first
   winter service up alpha       # start per-env services for the alpha env
   winter service status alpha   # check service health
   winter service logs alpha     # stream logs
   ```

Commit `config.toml`, `environment-compose.yaml`, and `workspace-compose.yaml` to source — they are the project's service config and belong in version control.

See [`index.md`](./index.md) for workspace-runtime rules, the port-substitution convention, and workspace scope. See `context/provider-contract.md` for the docker-specific wire contract.

## 🔧 Manual parity

Each scope-pure compose file is independently runnable by hand. The orchestrator runs compose with the scope's environment injected by winter-cli core (not `--env-file`), so `${VAR}` references in the compose file resolve against the injected vars. To reproduce the same environment by hand, source it from `winter env <scope>`:

```bash
# Per-env services (e.g. alpha env, WINTER_SERVICE_PREFIX=myapp):
source <(winter env alpha)
docker compose -p myapp-alpha \
    -f .winter/config/winter-service-docker/environment-compose.yaml \
    up -d

# Workspace singleton services:
source <(winter env workspace)
docker compose -p myapp-workspace \
    -f .winter/config/winter-service-docker/workspace-compose.yaml \
    up -d
```

See `context/provider-contract.md#environment-variable-injection` for the full injection contract (which variables per scope, and precedence rules). Replace `myapp` with your workspace's resolved `WINTER_SERVICE_PREFIX` (or your `config.toml`'s `project_prefix` override, if set).

## License

MIT.
