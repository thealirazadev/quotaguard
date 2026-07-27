# quotaguard - Testing

## Strategy

- **pytest** is the framework. A **real Redis is required** for the test suite - the Lua
  scripts, their atomicity, and their TTL behavior are the product, and mocking Redis would test
  the mock. Tests use a dedicated database index (`redis://localhost:6379/15` by default,
  overridable via `TEST_REDIS_URL`) and `FLUSHDB` between tests. Nothing else is real: no
  network beyond localhost Redis, no webhook endpoints (a local httpx `MockTransport` or an
  in-process receiver), no secrets.
- **Unit tests** for pure Python: header rendering (draft-07 strings, layer selection, ties),
  ARGV marshalling and tuple decoding, key hashing and secret format, webhook backoff math
  (bounds, cap, jitter range), key cache TTL and invalidation, CLI table formatting.
- **Mirror-math tests** guard the Lua spec: the refill formula and the civil-calendar conversion
  are reimplemented in a few lines of Python and cross-checked - refill over randomized
  capacities/rates/elapsed values (asserting the conservative-rounding invariant), and
  civil-from-days against Python's `calendar`/`datetime` over a wide range of epochs including
  month and leap-year boundaries. Script behavior itself is then asserted against real Redis.
- **Integration tests** drive the script and the HTTP API: `check.lua` via the script registry
  (bucket drain and refill with short real sleeps sized so timing jitter cannot flip outcomes,
  window trim and the cardinality bound, quota hard stop, soft-flag SETNX firing exactly once,
  deny-path write-freeness by before/after key inspection, TTLs present), and the API through
  `httpx.AsyncClient` over `ASGITransport` with a temporary SQLite database migrated by
  `alembic upgrade head` (so migrations and constraints are genuinely exercised).
- **The concurrency test is the money test**: N=50 checks fired concurrently against a remaining
  budget of 10 must admit exactly 10, repeated for each layer being the binding constraint. It
  runs in CI on every push and is mandatory after any change to `check.lua`.
- **E2E-ish tests**: full check flow (HTTP -> cache -> EVALSHA -> headers) including cache-miss
  resolution from SQLite; fail-open/fail-closed by pointing the Redis client at a closed port;
  rollup CLI against seeded Redis + SQLite proving idempotency and restore.
- **Manual QA** covers what automation cannot: `redis-cli monitor` single-round-trip checks,
  real webhook receivers, and the checklists in `docs/phases.md`.

### Test layout

- `tests/conftest.py` - fixtures: `redis` (flushed test DB), `db` (tmp SQLite +
  `alembic upgrade head`), `client` (ASGITransport, overridden settings), `make_plan`/`make_key`
  factories, `check()` helper posting a valid check.
- `tests/test_admin_plans.py`, `tests/test_admin_keys.py`, `tests/test_auth.py`,
  `tests/test_cli.py` - Phase 1 surface.
- `tests/test_lua_check.py` - script behavior against Redis (per-layer, TTLs, deny purity).
- `tests/test_refill_math.py`, `tests/test_civil_dates.py` - mirror-math property checks.
- `tests/test_concurrency.py` - the over-admission tests.
- `tests/test_check_api.py`, `tests/test_headers.py`, `tests/test_keycache.py` - Phase 2 HTTP.
- `tests/test_webhooks.py`, `tests/test_rollup.py`, `tests/test_usage.py` - Phase 3.
- `tests/test_failure_policy.py`, `tests/test_latency.py` - Phase 4.

Coverage target: every documented error `code`, every deny `reason`, every log event key, and
every CLI exit code produced by at least one test. Meaningful paths over percentages.

## CI

GitHub Actions, two jobs on every push/PR: `lint` (ruff + black) and `test` (pytest) with a
Redis service container:

```yaml
services:
  redis:
    image: redis:7.4-alpine
    ports: ["6379:6379"]
    options: >-
      --health-cmd "redis-cli ping" --health-interval 5s
      --health-timeout 3s --health-retries 10
```

The test job exports `TEST_REDIS_URL=redis://localhost:6379/15` and needs no secrets. CI is
green only when lint, format, and the full suite (concurrency tests included) pass.

## Exact commands

```bash
docker run --rm -d -p 6379:6379 redis:7.4-alpine   # local Redis if none is running
uv sync                                            # install from pyproject + uv.lock
uv run alembic upgrade head                        # apply migrations (dev database)
uv run ruff check .                                # lint
uv run black --check .                             # format check (black . to apply)
uv run pytest                                      # full suite (needs Redis)
uv run pytest tests/test_concurrency.py            # the money test alone
```

## Definition of done for any feature

A feature (one commit per `docs/phases.md`) is done only when, from a clean checkout with Redis
running: `uv sync`, `uv run ruff check .`, `uv run black --check .`,
`uv run python -c "import app.main"`, `uv run alembic upgrade head`, and `uv run pytest` all
succeed. Any change touching `app/lua/` additionally requires the concurrency tests re-run and
called out in the commit. If the same failure survives two fix attempts, stop and report per
`docs/rules.md`.
