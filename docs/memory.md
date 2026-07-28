# quotaguard - Project Memory

Running log of what is done, in progress, and decided. Update after every meaningful chunk of
work; log every non-obvious decision with its reason. Keep entries short and dated.

## Completed

- 2026-07-27 - Planning documentation created (README, PRD, architecture, rules, phases, design,
  testing, api-contracts, launch-checklist, memory). No code yet; implementation follows
  `docs/phases.md` starting with Phase 1 after owner review.
- 2026-07-28 - Phase 1 complete: scaffold (`pyproject.toml` with exact pins, committed `uv.lock`,
  Ruff/Black config, `.gitignore`, `.env.example`), `app/config.py`, `app/logging.py`,
  `app/errors.py`, `app/main.py` with `GET /health`, `plans`/`api_keys` models and migration
  `0001_init`, admin and service token dependencies, the plan and key admin API (create, list,
  detail, update, issue, override, revoke), the CLI, the CI workflow with a Redis 7 service
  container, 70 tests, and the README setup and admin sections. Fifteen commits as listed in
  `docs/phases.md`, plus one `fix(errors)` commit for a defect found during verification.

## Project status

- Phase 1 done and verified locally; awaiting owner approval before Phase 2. Phase 2 is the
  atomic check path: `app/redis_client.py`, `app/lua/check.lua` exactly per the spec in
  `docs/architecture.md`, the key cache, `POST /v1/check`, and the concurrency tests. No Redis
  code exists yet by design: nothing in Phase 1 reads or writes a `qg:*` key, and a test asserts
  that (`test_admin_writes_touch_no_redis_keys`).

### Verified on 2026-07-28

- Fresh clone into a temporary directory: `uv sync`, `uv run alembic upgrade head`, and
  `uv run uvicorn app.main:app --workers 1` all clean; `GET /health` returned `{"status": "ok"}`.
- `uv run ruff check .`, `uv run black --check .`, and `uv run pytest` (70 passed) green against
  a real Redis 7.4 container. The Redis-dependent test was confirmed to fail when pointed at a
  closed port, so it genuinely exercises the container rather than skipping.
- Manual checklist against the running server: plan created via API and another via CLI (both
  appear in `GET /admin/plans` and `plan list`), duplicate slug 409, out-of-bounds 422, key
  issued with the secret shown once (absent from the database file and from `key list`),
  override set then cleared with the detail route reflecting both, revoke then second revoke 409,
  missing and wrong admin tokens 401, `/health` open, malformed JSON 422, unknown route 404,
  `X-Request-ID` echoed, plans and keys surviving a process restart.
- Log review of the manual run: JSON lines only, request ids present, no secrets, no tokens, no
  tracebacks.

### Not verified

- The GitHub Actions workflow has not run yet (it first executes on push). Its steps mirror the
  commands verified locally, and the Redis service block is copied from `docs/testing.md`.

## Decisions log

- 2026-07-27 - Header semantics pinned to draft-ietf-httpapi-ratelimit-headers-07 (the
  `RateLimit: limit=..., remaining=..., reset=...` dictionary form). Later revisions moved to
  partitioned named policies; -07 maps 1:1 onto the check response fields and matches what most
  deployed gateways emit today. Upgrading later is a header-rendering change only, because the
  raw integers always accompany the strings in the JSON body.
- 2026-07-27 - The check script derives the month from Redis TIME and builds the quota/flag key
  names from prefixes passed in KEYS, so those keys are not fully declared to EVALSHA. Declaring
  them client-side would require an application clock to name the month, violating the one-clock
  rule. The cost is Redis Cluster incompatibility, which is acceptable because single-node Redis
  is an explicit non-goal boundary.
- 2026-07-27 - `cost` applies to the token bucket and the monthly quota but the sliding window
  always counts one request. The window bounds request rate (and its ZSET memory equals its
  limit); letting cost multiply window entries would break the memory bound and conflate work
  units with request counts. Short-window state is scoped per (key, resource) so one hot
  endpoint cannot starve the rest of a key's traffic, while the monthly quota stays per key
  because that is the unit operators reason about.
- 2026-07-27 - Bucket refill advances the stored timestamp by `ceil(add * 1e6 / rate)` instead
  of jumping to `now`, carrying the fractional-token remainder as un-elapsed time. Combined with
  flooring the credit, every rounding lands on the under-credit side: the limiter can be
  microscopically stingy (under one microtoken per refill) but can never over-admit, and the
  plan validation bounds keep all Lua arithmetic exact below 2^53.
- 2026-07-28 - Timestamps are rendered by one helper (`models.to_iso_utc`) instead of pydantic's
  default. SQLite returns naive datetimes for `DateTime(timezone=True)` columns, so a missing
  tzinfo is read as UTC and emitted with the `Z` suffix that `docs/api-contracts.md` shows.
  Without this, responses would carry `+00:00` or, worse, be read as local time.
- 2026-07-28 - PATCH bodies use `model_dump(exclude_unset=True)` so an absent field and an
  explicit `null` are different requests. An explicit `null` clears exactly the fields that are
  nullable (`webhook_url` on a plan, any override on a key); `null` on anything else is a 422
  rather than a database constraint error. This is what makes "clear the override" expressible
  in one endpoint, as `docs/api-contracts.md` requires.
- 2026-07-28 - The CLI registers `--json` and `-q` on both the top-level parser and every leaf
  subparser, with `argparse.SUPPRESS` as the leaf default so a value given before the subcommand
  is not overwritten by the leaf's default. `docs/design.md` calls them global flags, and
  argparse otherwise only accepts them ahead of the subcommand.
- 2026-07-28 - The CLI sends structured logs to stderr while everything else logs to stdout, so
  `--json` output stays a clean machine interface. This is the reason `configure_logging` takes
  an optional stream.
- 2026-07-28 - Tests use one session-scoped temporary SQLite file, migrated once with
  `alembic upgrade head` and emptied between tests, rather than a database per test. The engine
  is built from settings at import time, so a per-test database would mean rebuilding it; the
  migration still runs for real, which is the point of exercising it.
- 2026-07-28 - `GET /health` returns only `{"status": "ok"}` in Phase 1. The `redis` field is
  Phase 4 work (`feat(health): report redis status in health response`), and adding it early
  would have meant a Redis client with no failure policy behind it.
- 2026-07-28 - Local test Redis runs on port 6380 (`redis:7.4-alpine`, `maxmemory-policy
  noeviction`) because 6379 was already taken on the development machine. CI keeps 6379 and
  `TEST_REDIS_URL` overrides the default in both places.
- 2026-07-27 - Rollups upsert with `used = max(existing, read)` and Redis counters are restored
  after data loss via a delta-based `INCRBY` inside `restore.lua` (never SET), so overlapping
  rollups, replayed runs, and concurrent live traffic can never lower a persisted value or lose
  increments. `redis_down_policy` defaults to `fail_open` because a limiter outage should not
  take down the customer's API; plans guarding paid quotas opt into `fail_closed`.
