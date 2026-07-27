# quotaguard

quotaguard is a planned API rate limiting and quota decision service. An API gateway or
application will call `POST /v1/check` once per request with an api key and a resource name;
quotaguard answers allow or deny in a single Redis round trip and returns ready-to-emit draft
RateLimit header values. Every decision runs as one atomic Redis Lua script covering a token
bucket (burst), a sliding window log (sustained rate), and a monthly quota with hard and soft
thresholds - so concurrent checks can never over-admit.

## The problem it solves

Hand-rolled rate limiting is usually wrong in the corners: check-then-consume races over-admit
under concurrency, app-server clocks disagree, float refill math drifts, and monthly usage lives
in whichever process counted it last. quotaguard centralizes the decision next to one Redis with
one clock (Redis TIME, read inside the script), integer microtoken arithmetic, and idempotent
monthly rollups persisted to SQLite - correctness properties that are specified and tested, not
assumed.

## Planned features

All of the following is planned behavior; nothing is implemented yet.

- `POST /v1/check`: allow/deny plus limit, remaining, and reset per
  draft-ietf-httpapi-ratelimit-headers-07, in one `EVALSHA` round trip.
- Two short-window layers per plan: token bucket for burst, sliding window log for sustained
  rate, both scoped per (key, resource) and overridable per key.
- Monthly quotas keyed by UTC month with a hard cap and a soft threshold that fires exactly one
  webhook warning per key per month (persistent outbox, retries, optional HMAC signing).
- Plan and key management via a small admin API and CLI: create plans, issue keys (secret shown
  once, stored hashed), override limits per key, revoke.
- Idempotent monthly rollups from Redis to SQLite under an advisory lock, with counter restore
  after Redis data loss, plus a per-key usage report endpoint.
- Per-plan Redis-down policy: fail open with logging, or fail closed.

## Stack

- Python 3.12, FastAPI + Uvicorn
- Redis 7.x (required - the atomic Lua scripts are the core of the design)
- SQLite via SQLAlchemy 2 + Alembic (plans, keys, rollups, webhook outbox)
- httpx (webhook delivery and the test client), pytest with a real Redis in CI, Ruff, Black,
  managed with `uv` and a committed lockfile

## Documentation

| Document | Contents |
|----------|----------|
| [docs/PRD.md](docs/PRD.md) | Problem, goals, non-goals, users, success criteria |
| [docs/architecture.md](docs/architecture.md) | Stack, data model, the full Lua script spec, failure modes, invariants |
| [docs/rules.md](docs/rules.md) | Project-specific engineering rules |
| [docs/phases.md](docs/phases.md) | Build phases, expected commits, verification checklists |
| [docs/design.md](docs/design.md) | CLI UX, API response, and log line design |
| [docs/testing.md](docs/testing.md) | Test strategy, real-Redis CI setup, commands |
| [docs/api-contracts.md](docs/api-contracts.md) | Every endpoint, CLI command, webhook, and the error format |
| [docs/launch-checklist.md](docs/launch-checklist.md) | Pre-production checks and drills |
| [docs/memory.md](docs/memory.md) | Working log and decisions |

## Status

Planning stage: documentation only, no code yet. Implementation follows `docs/phases.md` one
phase at a time, starting with Phase 1 after the docs are approved.
