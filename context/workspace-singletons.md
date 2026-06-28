# Workspace-scoped singleton services

Some services — a shared database, a container registry, a message broker — should run once for the whole workspace rather than once per feature env. The orchestrator supports this via a dedicated `<project_prefix>-workspace` compose project, separate from every per-env project.

## Driving the workspace scope

Use `winter service` with the reserved `workspace` target:

```bash
winter service up workspace          # start all workspace services
winter service down workspace        # stop all workspace services (authoritative compose down)
winter service status workspace      # list workspace service states
winter service restart workspace/db  # restart a single workspace service
```

`winter service up <env>` does **not** auto-start the workspace scope. Run `winter service up workspace` first, or use `winter service up <env>` (which ensures the workspace scope is up before starting the env). `down <env>` intentionally leaves the workspace project running; only `down workspace` tears it down.

The `workspace` token is an exact reserved name — `work*` globs do NOT match it.

Named volumes declared in `workspace-compose.yaml` persist across `compose down` — see `winter-service-docker:/context/provider-contract.md#workspace-scope-model-and-named-volumes` for the persistence model and how to clear them.

## Declaring workspace services

Add `scope = "workspace"` to a `[[service]]` entry in `workspace:/.winter/config/winter-service-docker/config.toml`. `scope` is the only field that distinguishes a workspace singleton from a per-env service — it defaults to `"project"` when omitted, and the `name` field is declared the same way as for any project service:

```toml
[[service]]
name  = "db"
scope = "workspace"

[[service]]
name  = "broker"
scope = "workspace"
```

Key points:
- **Global name namespace** — names are unique across both scopes; a project and a workspace service may not share a name.
- **Ports come from the injected `WINTER_WORKSPACE_PORT_BASE`** — workspace services get no `WSD_PORT_*`; reference `${WINTER_WORKSPACE_PORT_BASE}` directly in `workspace-compose.yaml` (e.g. `ports: ["${WINTER_WORKSPACE_PORT_BASE}:5432"]`), and declare any additional workspace service ports in the workspace `config.toml` `[env.workspace.vars]` table. For why workspace services are excluded from `WSD_PORT_*` derivation and how the scope env is injected, see `winter-service-docker:/context/provider-contract.md#workspace-scope-model-and-named-volumes`.
- **Workspace services are excluded from per-env `up`** — `winter service up alpha` starts only project-scoped services; workspace services are never included in a per-env compose invocation.
- **Validation** — the loader enforces globally unique names across both scopes and rejects unknown scope values at parse time.

See `winter-service-docker:/workflow/config.toml.example` for the annotated schema reference.
