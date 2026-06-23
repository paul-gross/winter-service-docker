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
- **Starter scaffolder** — `python3 -m docker_orchestrator.scaffold <dest>` generates a starter `compose.yaml` and `config.toml` demonstrating the `${WSD_PORT_*}` convention and named volumes.
- **Injectable seam** — all docker/compose calls go through a `ComposeClient` interface; unit tests use a fake (no real daemon required).

## 🚀 Installation & Setup

1. **Scaffold a starter config:**

   ```bash
   PYTHONPATH=src python3 -m docker_orchestrator.scaffold \
       <workspace-root>/.winter/config/winter-service-docker/
   ```

2. **Edit `config.toml`** — set `project_prefix`, point `compose_file` at your compose file, and list your `[[service]]` entries.

3. **Edit `compose.yaml`** — configure your images and use `${WSD_PORT_<NAME>}` for published ports.

4. **Register the extension** in workspace `.winter/config.toml`:

   ```toml
   [capabilities]
   service = "winter-service-docker"

   [[standalone_repository]]
   name = "winter-service-docker"
   url  = "git@github.com:paul-gross/winter-service-docker.git"
   path = ".winter/ext/service-docker"
   ```

   The legacy root-level key `service_orchestrator = "winter-service-docker"` is still accepted as a deprecated alias.

5. **Start services:**

   ```bash
   winter service up workspace   # optional: start shared singletons first
   winter service up alpha       # start per-env services for the alpha env
   winter service status alpha   # check service health
   winter service logs alpha     # stream logs
   ```

Commit `config.toml` and `compose.yaml` to source — they are the project's service config and belong in version control.

See [`index.md`](./index.md) for workspace-runtime rules, the port-substitution convention, and workspace scope. See `ai/provider-contract.md` for the docker-specific wire contract.

## License

MIT.
