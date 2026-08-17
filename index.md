# Winter service orchestration via docker compose

Implements winter's `service` capability slot with `docker compose`, deriving per-env isolation from winter's env-index and port registry so feature environments run side-by-side without port or namespace conflicts; because it reports real container health, `winter service up <env> --wait` is a genuine readiness gate.

Read [context/index.md](./context/index.md) when setting up, operating, or developing this orchestrator — the hub for its setup steps, isolation model, and reference.
