# Contributing

## Commit messages

Conventional Commits with a scope:

    <type>(<scope>): <description>

    [optional body]

- Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `perf`, `style`, `ai`
- Scope: repo name or subsystem (e.g. `winter-service-docker`, `workflow`, `scaffold`)
- The `/wf-commit` skill from [winter-workflow](https://github.com/paul-gross/winter-workflow) generates commits in this format

## Pre-commit checks

Bash scripts: `bash -n <file>` catches syntax errors; `winter doctor` exercises the probe. No automated pre-commit hook.

Python (`src/docker_orchestrator/`, `tests/`): run these before pushing any Python changes. Requires Python 3.11+ (`tomllib` is stdlib from 3.11; the current dev env uses 3.12).

```bash
uv run pytest          # must be green
uv run ruff check .    # lint
uv run pyright         # typecheck
```

Or via mise tasks if configured:

```bash
mise run test
mise run lint
mise run typecheck
```

The `docker_orchestrator` runtime has no third-party dependencies (stdlib-only). The dev tooling (pytest, ruff, pyright) lives in `pyproject.toml`'s `[dependency-groups] dev` and is installed via `uv sync`.

## No-real-docker test approach

Unit tests never require a running docker daemon. All docker and `docker compose` calls go through the injectable `ComposeClient` seam (`src/docker_orchestrator/compose_client.py`). Tests use `FakeComposeClient` or `FakeRunner` from `tests/fakes.py` to record invocations and return canned results.

To test the doctor probe (which calls real docker binaries) or hooks, run them directly with appropriate env vars:

```bash
WINTER_WORKSPACE_DIR=$(pwd) bash workflow/doctor.sh
```

Expected output in this environment: both probes will report `fail` (docker daemon unavailable and/or compose v2 missing) — this is correct and expected behavior for the probe.

## Testing changed orchestrator code against a worktree

The installed extension (`winter-service-docker:/`) runs committed code. The PRIMARY door for exercising in-progress changes is the `--service-orchestrator` root flag, which sets `WINTER_EXT_DIR`/`WINTER_EXT_PREFIX` for you:

```bash
winter --service-orchestrator=/path/to/gamma/winter-service-docker service describe
winter --service-orchestrator=/path/to/gamma/winter-service-docker service status alpha
```

As a fallback, export the vars manually and invoke the entrypoint directly:

```bash
# Set the env vars winter would normally inject:
export WINTER_WORKSPACE_DIR=/path/to/workspace
export WINTER_EXT_DIR=/path/to/gamma/winter-service-docker
export WINTER_EXT_CONFIG_DIR="$WINTER_WORKSPACE_DIR/.winter/config/winter-service-docker"

# Run an action:
PYTHONPATH="$WINTER_EXT_DIR/src" python3 "$WINTER_EXT_DIR/workflow/service" describe

# Or run the doctor probe:
WINTER_WORKSPACE_DIR=$WINTER_WORKSPACE_DIR bash "$WINTER_EXT_DIR/workflow/doctor.sh"
```

## Scaffolder

The scaffolder (`src/docker_orchestrator/scaffold.py`) has no external dependencies and is covered by `tests/test_scaffold.py`. Run it directly for manual testing:

```bash
PYTHONPATH=src python3 -m docker_orchestrator.scaffold /tmp/wsd-test
```

## Delivery

- Default branch: `master`
- **Primary contributors** push directly to `master` whenever — no PR or review required.
- **Outside contributors** are welcome — open a PR against `master` and I'll review and merge.
