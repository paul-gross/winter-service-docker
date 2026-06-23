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

## Doctor probe

`workflow/doctor.sh` runs as part of `winter doctor`, checking that the docker daemon is reachable and compose v2 is installed. See `workspace:/ai/winter-cli/setup.md` for the doctor-probe contract.
