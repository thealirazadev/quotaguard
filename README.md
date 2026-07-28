# quotaguard

quotaguard is an API rate limiting and quota decision service. An API gateway or application
calls `POST /v1/check` once per request with an api key and a resource name; quotaguard answers
allow or deny in a single Redis round trip and returns ready-to-emit draft RateLimit header
values. Every decision runs as one atomic Redis Lua script covering a token bucket (burst), a
sliding window log (sustained rate), and a monthly quota with hard and soft thresholds - so
concurrent checks can never over-admit.

## The problem it solves

Hand-rolled rate limiting is usually wrong in the corners: check-then-consume races over-admit
under concurrency, app-server clocks disagree, float refill math drifts, and monthly usage lives
in whichever process counted it last. quotaguard centralizes the decision next to one Redis with
one clock (Redis TIME, read inside the script), integer microtoken arithmetic, and idempotent
monthly rollups persisted to SQLite - correctness properties that are specified and tested, not
assumed.

## Features

Plan and key management (the admin API and CLI below) is implemented. The check endpoint, the
quota lifecycle, and the Redis-down policies follow in later phases of `docs/phases.md`.

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

## Setup

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), and a Redis 7.x instance.

```bash
git clone https://github.com/thealirazadev/quotaguard.git
cd quotaguard
cp .env.example .env          # then set REDIS_URL and QG_ADMIN_TOKEN
uv sync                       # install pinned dependencies from uv.lock
uv run alembic upgrade head   # create the SQLite schema
uv run uvicorn app.main:app --workers 1
```

`GET /health` answers `{"status": "ok"}`. Every setting is listed in `.env.example` and in the
environment table in `docs/architecture.md`; `REDIS_URL` and `QG_ADMIN_TOKEN` are required and
have no defaults.

Run a local Redis with Docker if you do not have one:

```bash
docker run --rm -d -p 6379:6379 redis:7.4-alpine redis-server --maxmemory-policy noeviction
```

## Managing plans and keys

A plan holds the limits; a key belongs to a plan and may override any limit. Every admin route
requires the `X-Admin-Token` header, and every response is wrapped in `{"data": ...}`. Errors use
one envelope: `{"error": {"code": "...", "message": "..."}}`.

| Route | Purpose |
|-------|---------|
| `POST /admin/plans` | Create a plan (`409` on a duplicate slug) |
| `GET /admin/plans` | List plans, ordered by slug |
| `GET /admin/plans/{slug}` | One plan plus its `keys_count` |
| `PATCH /admin/plans/{slug}` | Update any mutable field |
| `POST /admin/keys` | Issue a key; the only response carrying the secret |
| `GET /admin/keys?plan={slug}` | List keys, optionally filtered by plan |
| `GET /admin/keys/{key_id}` | One key plus its resolved effective limits |
| `PATCH /admin/keys/{key_id}` | Set an override, or clear it with an explicit `null` |
| `POST /admin/keys/{key_id}/revoke` | Revoke permanently (`409` if already revoked) |

```bash
curl -X POST localhost:8000/admin/plans \
  -H "X-Admin-Token: $QG_ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"slug":"pro","name":"Pro","burst_capacity":100,"burst_refill_per_sec":50,
       "sustained_limit":5000,"sustained_window_seconds":3600,"monthly_quota":500000,
       "quota_soft_pct":80,"webhook_url":"https://ops.example.com/hooks/quota"}'
```

The same operations are available offline through the CLI, which imports the service layer
directly and needs no running server. Every command accepts `--json` and `-q`.

```bash
uv run python -m app.cli plan create --slug pro --name "Pro" --burst-capacity 100 \
  --burst-refill 50 --sustained-limit 5000 --sustained-window 3600 \
  --monthly-quota 500000 --soft-pct 80 --webhook-url https://ops.example.com/hooks/quota
uv run python -m app.cli plan list
uv run python -m app.cli key issue --plan pro --name "acme production"
uv run python -m app.cli key list
uv run python -m app.cli key override k_1a2b3c4d5e6f --sustained-limit 10000
uv run python -m app.cli key override k_1a2b3c4d5e6f --clear sustained_limit
uv run python -m app.cli key revoke k_1a2b3c4d5e6f
```

Exit codes: `0` success, `1` the operation failed, `2` a configuration or environment problem.

The issued secret (`qk_` plus 43 url-safe characters) is shown exactly once. Only its sha256 hash
and a 12-character display prefix are stored, so a lost secret can only be replaced by issuing a
new key. Numeric limits are validated on every write against the bounds in
`docs/architecture.md`; those bounds keep the admission arithmetic exact.

## Development

```bash
uv run ruff check .
uv run black --check .
TEST_REDIS_URL=redis://localhost:6379/15 uv run pytest
```

The test suite requires a real Redis: the Lua admission scripts are the product, so mocking Redis
would test the mock. CI runs lint and the full suite against a Redis 7 service container.

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

Phase 1 complete: the project skeleton, structured logging, the error envelope, the plans and
api_keys schema, admin authentication, the plan and key admin API, and the CLI. Phase 2 adds the
atomic check script and `POST /v1/check`. Implementation follows `docs/phases.md` one phase at a
time.
