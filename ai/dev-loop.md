# Testing changed orchestrator code against a worktree

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

## Env-file sourcing in the loop

When a scope env file exists (`<env>/.winter.env`, or `<workspace>/.winter.workspace.env` for the workspace scope), every `docker compose` call is wrapped in a `bash -c` that sources it first — see `winter-service-docker:/ai/provider-contract.md#env-file-sourcing`. Two consequences when exercising changes:

- To confirm sourcing took effect, reference a sourced var in `compose.yaml` (e.g. `"${WINTER_PORT_BASE}:5432"`) and check the published host port with `docker compose ... ps` or `docker ps`.
- A malformed env file (bad shell syntax) fails inside the `bash -c` wrapper **before** compose runs — the error surfaces as a shell/sourcing error on the verb's stderr, not a compose error. If a verb fails with a shell message and no compose output, suspect the env file.

## Doctor probe

`workflow/doctor.sh` runs as part of `winter doctor`, checking that the docker daemon is reachable and compose v2 is installed. See `workspace:/ai/winter-cli/setup.md` for the doctor-probe contract.
