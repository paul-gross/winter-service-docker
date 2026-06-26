# Testing changed orchestrator code against a worktree

The PRIMARY door for exercising in-progress changes is the `--service-orchestrator` override, which sets `WINTER_EXT_DIR`/`WINTER_EXT_PREFIX` for you:

```bash
winter --service-orchestrator=/path/to/gamma/winter-service-docker service describe
winter --service-orchestrator=/path/to/gamma/winter-service-docker service status alpha
```

**Verifying the `status` path requires the feature core too.** `status` env enumeration lives in winter-cli core, not this provider — core computes each scope's environment and injects it on `up`, `down`, and `status`. If you are verifying a change that touches the status path, also point at the feature core with `--winter` (see `workspace:/ai/winter-cli/root-flags.md`):

```bash
winter --winter=./alpha/winter --service-orchestrator=./alpha/winter-service-docker service status alpha
```

As a fallback, export the vars manually and invoke the entrypoint directly:

```bash
export WINTER_WORKSPACE_DIR=/path/to/workspace
export WINTER_EXT_DIR=/path/to/gamma/winter-service-docker
export WINTER_EXT_CONFIG_DIR="$WINTER_WORKSPACE_DIR/.winter/config/winter-service-docker"
PYTHONPATH="$WINTER_EXT_DIR/src" python3 "$WINTER_EXT_DIR/workflow/service" describe
```

See `CONTRIBUTING.md` for the full dev-loop (lint, typecheck, test, unit-test how-to).

## Environment injection in the loop

winter-cli core computes each scope's environment and injects it into the provider subprocess on `up`, `down`, and `status`; `restart` and `logs` receive only the four base extension vars and operate on already-provisioned containers. The provider reads the vars from `os.environ` and passes them to `docker compose` — see `winter-service-docker:/ai/provider-contract.md#environment-variable-injection`. To confirm an injected var reached compose, reference it in `environment-compose.yaml` (e.g. `"${WINTER_PORT_BASE}:5432"`) and check the published host port with `docker compose ... ps` or `docker ps`.

## Doctor probe

`workflow/doctor.sh` runs as part of `winter doctor`, checking that the docker daemon is reachable and compose v2 is installed. See `workspace:/ai/winter-cli/configuration/doctor.md` for the doctor-probe contract.
