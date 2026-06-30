# Workflow setup walkthrough

This guide is an interactive walkthrough that produces `workspace:/.winter/config/winter-service-docker/config.toml`, `environment-compose.yaml`, and `workspace-compose.yaml` — the three files that configure `winter-service-docker` for every feature worktree in the workspace. `config.toml` declares which services winter manages and how they map to compose projects. The two compose files define the actual container definitions for per-env and workspace-scoped services respectively.

Run it on a fresh workspace, or any time you want to add services, change the project prefix, or adjust which scope a service belongs to.

**Idempotent:** safe to re-run at any time. Before each step, check the current state of the relevant file. If the step is already done, **say so explicitly** ("`backend` is already declared in `config.toml` — skipping") and move on. Don't silent skip.

## Caller contract

This guide is invoked by the `ws-setup` service-orchestration sub-guide (`workspace:/context/setup-service-orchestration.md`, Step 4) after it has already discovered and assigned services. The caller provides:

- **The set of services assigned to docker**, each with a **scope**: `"project"` (per-env) or `"workspace"`.

This guide does not re-discover or re-assign services. It wires the assigned set into docker's config and compose files.

## How to run this guide

This is a guided walkthrough, not a script. Your job is to teach the user how their docker compose wiring works while configuring it. Be verbose, be explicit, and be patient.

**Follow the pacing rules of the setup process you're engaged in.** This guide is normally entered mid-walkthrough from `/ws-setup`, so honor that walkthrough's pacing throughout — one question per turn, speak before acting, narrate actions, don't pause between steps, and show what you found rather than silent-skipping.

## Why this matters

When an agent runs `winter service up <env>`, the orchestrator reads `config.toml` to know which compose project to drive and which services to expect. `environment-compose.yaml` defines the containers for per-env services; `workspace-compose.yaml` defines the containers for shared singletons. Without these files, `winter service up` has nothing to start.

The two-compose-file model keeps scopes clean: per-env services run under `<project_prefix>-<env>` with ports auto-derived from `WINTER_PORT_BASE` via `${WSD_PORT_<NAME>}` placeholders, so each named environment gets unique host ports without collision. Workspace singletons run once under `<project_prefix>-workspace`, using fixed ports or `${WINTER_WORKSPACE_PORT_BASE}`.

## Prerequisites

Before running this guide:
- The workspace has been set up via `/ws-setup` (or the underlying `winter ws init` has been run).
- The `winter-service-docker` extension is installed (its standalone clone exists and is wired into the workspace).
- The caller has provided the set of services assigned to docker and their scopes.
- Read `winter-service-docker:/workflow/config.toml.example` to understand the manifest schema you'll be writing.
- Read `winter-service-docker:/context/per-env-isolation.md` to understand the two-compose-file model.

## Opening preamble (always send first)

Before doing anything, send a short orientation message, then continue straight into the first step:

> "I'll walk you through wiring your services into docker's compose files and `config.toml` inside `workspace:/.winter/config/winter-service-docker/`. Here's how docker's model works: per-env services live in `environment-compose.yaml` and run under a separate compose project per feature env (`<project_prefix>-<env>`); host ports use `${WSD_PORT_<NAME>}` placeholders so no two named environments collide. Workspace singletons live in `workspace-compose.yaml` and run once under `<project_prefix>-workspace`. `config.toml` ties it all together for winter. Stop me or ask questions at any time."

Don't wait for a "go" signal — just begin.

## Steps

### Check for existing config

**Explain first:** "Before changing anything, I need to know what's already there. If `workspace:/.winter/config/winter-service-docker/config.toml` already exists, I'll skip the scaffolder and edit the existing files in place — scaffolding after a `config.toml` exists would error. If it doesn't exist yet, I'll scaffold the starter files first."

Check the current state from the workspace root:

```bash
if [[ -f .winter/config/winter-service-docker/config.toml ]]; then
  cat .winter/config/winter-service-docker/config.toml
else
  echo "(no config.toml — will scaffold)"
fi
```

**If `config.toml` does not exist**, scaffold the starter files. The scaffold command must be run from the extension worktree (e.g. `<named-environment>/winter-service-docker`) so that `PYTHONPATH=src` resolves correctly, and the destination is anchored at the workspace root:

```bash
# Run from the extension worktree (e.g. <named-environment>/winter-service-docker):
PYTHONPATH=src python3 -m docker_orchestrator.scaffold \
    <workspace-root>/.winter/config/winter-service-docker/
```

Replace `<workspace-root>` with the absolute path to the workspace root (the directory containing `.winter/`). This creates three starter files: `config.toml`, `environment-compose.yaml`, and `workspace-compose.yaml`. Tell the user: "Scaffold created three starter files in `workspace:/.winter/config/winter-service-docker/`. I'll now edit them for your project."

**If `config.toml` already exists**, report what you found — `project_prefix`, `environment_compose_file`, `workspace_compose_file`, and the declared services. Do not scaffold. Tell the user: "`config.toml` already exists with `project_prefix = "<value>"` and `<n>` service(s) declared (`<name-list>`). I'll edit the existing files."

### Set project_prefix

**Explain first:** "`project_prefix` is the namespace for all compose projects created by this extension. Each env's project is named `<project_prefix>-<env>` (e.g. `myapp-<named-environment>`), and the workspace scope uses `<project_prefix>-workspace`. Pick something short and descriptive — typically the project or application name. Once set, changing it renames all compose projects (containers, networks, volumes); existing running containers would be orphaned."

Suggest a prefix derived from the primary project name (e.g. initials or an obvious short name). Then ask **one** question:

**"I suggest `<derived>` as the project prefix (compose projects would be `<derived>-<named-environment>`, `<derived>-workspace`). Confirm, or enter a different prefix?"**

- "confirm" / "yes" / the same value: use it.
- different value: use the value the user provides.

Edit `workspace:/.winter/config/winter-service-docker/config.toml` and set `project_prefix = "<confirmed>"`. Confirm: "`project_prefix` set to `<confirmed>`."

### Wire per-env services

**Explain first:** "Now I'll wire the per-env services (scope `"project"`) into `environment-compose.yaml` and declare them in `config.toml`. Per-env services run under `<project_prefix>-<env>` — one isolated compose project per feature env. Published host ports must use `${WSD_PORT_<NAME>}` placeholders (where `<NAME>` is the upper-cased service name); the orchestrator substitutes `WINTER_PORT_BASE + <position>` at runtime, where `<position>` is the 0-based declaration order among project-scoped `[[service]]` entries. This ensures no two named environments use the same host port."

If there are **no** per-env services in the assigned set, tell the user "No per-env services assigned — skipping `environment-compose.yaml`." and move on to workspace services.

Skip any service already declared in `config.toml` with a matching block in `environment-compose.yaml` — show what you found.

**Resolve the wiring yourself — don't interrogate the user field by field.** For each remaining per-env service, infer its container wiring from the project: existing `Dockerfile`(s), any `docker-compose.yml` / `compose.yaml`, `.env.example`, the README, and conventional defaults for the service's role. Resolve its **image** (and tag), the **internal port** the container listens on (Dockerfile `EXPOSE`, framework default, or compose file), and any **environment variables** the compose definition needs (e.g. a `DATABASE_URL` pointing at a workspace `db` service).

Then **present the full proposed wiring** and ask **one** question:

**"Here's how I'll wire your per-env services — for each, the `environment-compose.yaml` service block (image, `${WSD_PORT_<NAME>}:<internal-port>` mapping, env vars) and its `config.toml` `[[service]]` entry. Confirm, or tell me what to change?"**

- "confirm": apply (below).
- changes: fold in the user's corrections and re-present the proposal until they confirm.

On confirmation, **update `config.toml` and `environment-compose.yaml`** to add the services, following the schema in `winter-service-docker:/workflow/config.toml.example` and `winter-service-docker:/context/per-env-isolation.md` — no need to spell out each field here. Honor the two winter-specific rules the schema docs assume: per-env host ports use `${WSD_PORT_<NAME>}` (`<NAME>` upper-cased), and a `[[service]]` entry's **declaration order** sets its port offset (`position 0` → `WINTER_PORT_BASE + 0`), so order them deliberately. Then summarise: "Per-env services wired: `<name-list>`."

### Wire workspace-scoped services

**Explain first:** "Workspace-scoped services (scope `"workspace"`) run once under `<project_prefix>-workspace`, shared across all feature envs. They live in `workspace-compose.yaml`. Because workspace services have no per-env `WINTER_PORT_BASE`, the `${WSD_PORT_*}` mechanism does NOT apply — use fixed host ports or reference `${WINTER_WORKSPACE_PORT_BASE}` (injected by winter-cli core for the workspace scope) in the compose definition."

If there are **no** workspace-scoped services in the assigned set, tell the user "No workspace-scoped services assigned — skipping `workspace-compose.yaml`." and move on.

Skip any service already declared with `scope = "workspace"` and a matching block in `workspace-compose.yaml` — show what you found.

**Resolve the wiring yourself.** For each remaining workspace service, infer its **image** (and tag — e.g. an official `postgres:16-alpine` / `rabbitmq:3-management`), the **host port** it publishes, any **environment variables**, and whether it needs a **named volume** for persistence (databases and brokers usually do) — from the project's existing compose files, `.env.example`, README, and conventional defaults for that service.

Then **present the full proposed wiring** and ask **one** question:

**"Here's how I'll wire your workspace services — for each, the `workspace-compose.yaml` block and its `config.toml` `[[service]]` entry. Confirm, or tell me what to change?"**

- "confirm": apply.
- changes: fold in the user's corrections and re-present until they confirm.

On confirmation, **update `config.toml` and `workspace-compose.yaml`** to add the services, following the schema in `winter-service-docker:/workflow/config.toml.example` and the workspace-scope guidance in `winter-service-docker:/context/workspace-singletons.md`. Honor the winter-specific rule: workspace services have no per-env `WINTER_PORT_BASE`, so use fixed host ports or `${WINTER_WORKSPACE_PORT_BASE}` — never `${WSD_PORT_*}` — and give each a `scope = "workspace"` entry. Then summarise: "Workspace-scoped services wired: `<name-list>`. Drive them with `winter service up/down workspace`."

### Final report

Summarise everything that happened in a single message:

- `config.toml` location: `workspace:/.winter/config/winter-service-docker/config.toml` (scaffolded + edited / edited in place / unchanged)
- `project_prefix` value
- `environment-compose.yaml`: `workspace:/.winter/config/winter-service-docker/environment-compose.yaml` — per-env services declared (names and `${WSD_PORT_*}` assignments, or "none")
- `workspace-compose.yaml`: `workspace:/.winter/config/winter-service-docker/workspace-compose.yaml` — workspace services declared (names and ports, or "none")
- `[[service]]` entries in `config.toml`: list all, with scope
- Any manual steps still pending (e.g. adjusting container images, adding healthchecks)

End with:

> "Docker wiring complete. You can re-run this guide any time — it's idempotent and will only apply changes that are still needed. Start services with `winter service up workspace` (if you have workspace-scoped services) followed by `winter service up <env>`."
