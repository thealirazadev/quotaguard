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
- 2026-07-29 - Phase 2 complete: `app/redis_client.py` (pool with explicit timeouts, script
  registry with one NOSCRIPT reload-and-retry, preloaded by a lifespan hook),
  `app/lua/check.lua` implemented exactly per `docs/architecture.md` (Redis TIME as the only
  clock, in-script civil-calendar month, integer microtoken refill, window trim, quota plus soft
  SETNX, all-or-nothing consume, the 15-element tuple), `app/services/keycache.py` with
  invalidation wired into every plan and key mutation, `POST /v1/check` with resource and cost
  validation and in-band unknown/revoked answers, draft-07 header rendering, and 73 new tests
  (143 total). Seven commits as listed in `docs/phases.md`.
- 2026-07-29 - Phase 2 defect review: two key cache fixes (an invalidation lost to an in-flight
  lookup, and an unbounded cache) plus one missing test on the check route's service token, and
  two findings flagged for the owner because fixing them means changing
  `docs/architecture.md`. Three commits; 146 tests. See "Phase 2 defect review" below.

## Project status

- Phases 1 and 2 done and verified locally and in CI; awaiting owner approval before Phase 3.
- Open, and deliberately so: `POST /v1/check` has no Redis-down policy yet. A Redis failure on
  the check path currently surfaces as `500 internal_error` through the catch-all handler (the
  standard envelope, full detail logged, no traceback in the response). Fail-open and
  fail-closed per plan, the `degraded: true` body, and `check.redis_down` are Phase 4
  (`feat(check): add fail open and fail closed redis policies`), so the phase-verification line
  "Redis stopped -> degraded per policy" is not satisfied yet by design. Confirmed by pointing a
  server at a closed port: 500 with the envelope, `redis.exceptions.ConnectionError` in the log.
- Also Phase 3 and later, as planned: nothing consumes `soft_crossed` beyond logging
  `quota.soft_crossed` and reporting it in the response (the outbox row is Phase 3), and
  `GET /health` still reports only `{"status": "ok"}` (the `redis` field is Phase 4).

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

### Verified on 2026-07-29

Phase 2 checklist, run against the Redis 7.4 container and a real uvicorn process (one worker,
its own SQLite file and Redis database index):

- Capacity 5 at 1 token/s: five checks allowed with `remaining` 4 down to 0, the sixth denied
  with `reason: "burst"` and `Retry-After: 1`; after 2.2 seconds two more allowed and the third
  denied again. `remaining` never went below 0 anywhere.
- Sustained 3 per 2 second window: the fourth check denied with `reason: "sustained"` and
  `retry 2`, the fifth with `retry 1`; `ZCARD qg:s:{key_id}:search` read 1, 2, 3, 3, 3 through
  the sequence and dropped back to 1 after the window slid, with a TTL of 61584 ms.
- Monthly quota 10: ten allowed, the eleventh denied with `reason: "quota"` and
  `retry_after == reset`; `qg:q:{key_id}:2026-07` held `10` with TTL 4163130 (month end plus the
  45 day grace). `HGETALL` on the bucket, `ZCARD` on the window, and both month keys were
  byte-identical before and after the denied check.
- Concurrency: `pytest tests/test_concurrency.py` (six tests, each layer binding, plus the HTTP
  path) and a manual `xargs -P 50` curl loop against a fresh quota-10 key: 10 allowed, 40
  denied, counter 10, ZCARD 10. As a control that the test can actually fail, the same 50-way
  harness was run against a naive check-then-consume implementation in the scratchpad: it
  admitted all 50.
- Headers: `RateLimit: limit=100, remaining=97, reset=1` and
  `RateLimit-Policy: 100;w=2, 5000;w=3600, 500000;w=2678400` on the pro plan, the policy always
  in burst, sustained, monthly order; on allow the most constrained layer is reflected (burst at
  97/100 over sustained at 4999/5000); two resources on one key limited independently while the
  monthly counter advanced for both.
- Unknown key and revoked key: 200 with `allowed: false`, `reason` `unknown_key` /
  `revoked_key`, `layers: null`, `headers: {}`, and `KEYS qg:*` empty afterwards. Revoking
  flipped the answer on the very next request (in-process invalidation). Bad resource charset,
  `cost` 0, `cost` 1001, a missing field, and malformed JSON all returned 422
  `validation_error`.
- `redis-cli monitor` during one check: exactly one client command, `EVALSHA <sha> 4 ...`;
  everything else on the log is `[14 lua]`, the script's own calls inside that single round trip.
  The stored `ts` and the ZSET score are plain integer strings, so no value reached Redis in
  scientific notation.
- `uv run ruff check .`, `uv run black --check .`, and `uv run pytest` (143 passed) green
  locally, and CI green on every pushed Phase 2 commit (run 30392416743: lint clean, 143 passed
  against the Redis 7.4 service container).
- Fresh clone into a temporary directory: `cp .env.example .env`, `uv sync`,
  `uv run alembic upgrade head`, `uv run pytest` (143 passed), and
  `uv run uvicorn app.main:app --workers 1` started clean with no warnings; `GET /health`,
  `GET /admin/plans`, and `GET /admin/keys` returned well-formed empty states.
- Process restart: plans and keys survived, checks kept working against the live Redis state,
  and after `SCRIPT FLUSH` the next check answered correctly with one
  `lua script missing from the redis cache, reloading once` WARNING (the NOSCRIPT path).
- Log review of the manual run: JSON lines only, request ids present, event keys limited to
  `check.denied`, `check.unknown_key`, `quota.soft_crossed`, `key.issued`, `key.revoked`,
  `plan.created`; zero occurrences of `qk_`, of the admin token, or of a traceback.

### Not verified

- Redis-down behaviour on `/v1/check` is not implemented yet (see Project status); what was
  observed is the 500 fallback, not the documented degraded response.
- The refill cross-check against Redis asserts a range rather than an exact value, because the
  script reads its own TIME after the test seeds the bucket. The bound is the mirror at the
  seeded elapsed time and the mirror 250 ms later; an implementation that credited more than the
  elapsed time would still fail it.

## Phase 2 defect review, 2026-07-29

A second pass over the Phase 2 diff (`6c24c74..2d80f58`) read against `docs/architecture.md`,
hunting concurrency, retry, and partial-failure faults first. Three commits came out of it; two
further findings are recorded for the owner instead of changed.

### Fixed

- `fix(keycache): drop a lookup that raced an invalidation`. `resolve()` wrote its lookup result
  into the cache unconditionally. An admin mutation landing while the threadpool lookup was in
  flight cleared the cache and was then overwritten by the pre-mutation snapshot, so a revoked
  key kept resolving as live for a whole `KEY_CACHE_TTL_SECONDS`. That contradicts
  `docs/architecture.md` ("the in-process cache is invalidated immediately by the admin
  mutation") and the Phase 2 checklist line "revoke a key: checks flip to `revoked_key`
  immediately". A store now carries the invalidation generation it was read under and is dropped
  if that generation moved. The counter is only ever compared for equality and never decreases,
  so a lost increment from two threads racing cannot resurrect a stale write.
  `tests/test_keycache.py::test_a_revoke_during_a_lookup_is_not_masked_by_the_cache` revokes from
  inside a patched `_load` and fails on the old code.
- `fix(keycache): bound the cache against unknown key floods`. The cache had no size bound.
  `POST /v1/check` hashes whatever secret it is handed and caches the miss, so an unauthenticated
  caller sending distinct random secrets added one entry each, retained until an admin mutation
  happened to clear the cache: unbounded process memory driven by an open route. Measured at
  20,000 entries for 20,000 bogus secrets. The cache is now dropped whole past
  `MAX_CACHE_ENTRIES` (10,000), the same thing invalidation already does, so the worst case is
  one SQLite lookup per active key afterwards rather than growth without end.
- `test(check): assert the check route enforces the service token`. `require_service_token` was
  only exercised as a bare function, so deleting it from the check router would not have failed a
  single test. The new test overrides `get_settings` with a configured token and drives the real
  route; verified by removing the router dependency (test fails) and restoring it (test passes).

### Flagged for the owner, not changed

Both would mean editing `docs/architecture.md`, which is source of truth.

- A `cost` larger than the effective `burst_capacity` can never be admitted, but the script still
  reports `retry_after = ceil((cost * 1e6 - tokens) / rate_utok)` exactly as the architecture
  specifies. Measured with capacity 3 and cost 10: `Retry-After: 7`, and after sleeping 7 seconds
  the answer is the same deny with the same `Retry-After`, so a client honouring the header
  retries forever. An honest answer needs a rule for the impossible-cost case (deny with
  `retry_after` null, or reject the cost at validation time against the key's capacity).
- The failure table says every Redis failure on a check consumes nothing. That holds for a
  connect failure or a `NOSCRIPT`, but not for a socket timeout: `REDIS_TIMEOUT_MS` can expire
  after the script has already run and consumed, and the reply is simply lost. The Phase 4
  fail-open path would then allow a request that was also charged, and fail-closed would deny one
  that was charged. Worth stating in the table before Phase 4 implements the policies. Confirmed
  separately that redis-py 5.2.1 does not retry commands (`retry_on_error` empty, `Retry` with 0
  retries) and disconnects on timeout and on cancellation, so there is no silent double-consume
  from a client-side retry or a client disconnect.

### Checked and found sound

- `app/lua/check.lua` against the step-by-step spec: clock, civil-calendar month and month end,
  refill clamps and the `ts` advance, the exclusive window trim, the quota read, deny ordering,
  the all-or-nothing consume, every TTL, the soft `SETNX`, and all fifteen tuple elements. The
  one arithmetic gap found is cosmetic: `add * 1e6` in the `ts` advance can exceed 2^53 for
  capacities above about 576,000 tokens, but the absolute error after dividing by `rate_utok`
  stays under 1e-4 microseconds, far below the one-microtoken bound the design claims.
- Rate behaviour over real time, measured rather than reasoned about: capacity 1 at 1/s admitted
  3 in 3.0 seconds; capacity 5 at 100/s admitted 100 in 1.0 second; sustained 5 per 1 second
  admitted 15 in 3.0 seconds. Every result is at or under the theoretical maximum.
- Redis key names cannot be made to collide across keys, resources, or layers: `key_id` is a
  fixed-format `k_` plus 12 hex and the layer letter comes before it, so the validated resource
  charset (which does include `:`) cannot reach another key's namespace or the rollup lock.
- Error paths: no bare external call. The lifespan preload, the pool close, the SQLite lookup,
  the `NOSCRIPT` reload, and the tuple decode all handle and log their failure. No api key
  secret, admin token, or service token reaches a log line or an error body; validation messages
  carry the field name and the pydantic message only, never the submitted value.

Suite after the three commits: `ruff check`, `black --check`, and `pytest` (146 passed) all green
against the local Redis 7.2.4, run after each commit.

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
- 2026-07-29 - Every number the script hands to `redis.call` goes through
  `string.format('%.0f', value)`. Lua numbers are doubles and the default conversion switches to
  scientific notation well below microsecond epoch magnitudes, which would have written ZSET
  scores, ZSET members, and the stored bucket state as `1.785e+15`. This is the single most
  likely silent corruption in the script and it is now visible in `redis-cli monitor` output.
- 2026-07-29 - The civil-calendar helpers in `check.lua` are wrapped in `-- <civil>` and
  `-- </civil>` sentinel comments. `tests/test_civil_dates.py` extracts that block verbatim,
  appends a small harness, and EVALs it on the real Redis for a few hundred epochs including
  every month boundary from 2020 to 2039, both leap-day cases, and the 2100 non-leap century.
  Redis TIME cannot be faked from a test, so without the extraction the only alternative was to
  test a Python copy of the algorithm, which would prove nothing about the shipped Lua.
- 2026-07-29 - The sustained `reset` is read after the consume, not before. For an allowed check
  into an empty window the oldest member is the one just added, so `reset` is the full window
  length instead of 0; reporting 0 would tell a caller the limit resets immediately when in fact
  its own request occupies a slot for a whole window. Deny values are unaffected (a deny writes
  no member).
- 2026-07-29 - The key cache stores misses as well as hits. Without it, a flood of bogus keys
  would be one SQLite query per request on the hot path. A real secret is 32 random bytes, so it
  cannot collide with a cached miss, and the staleness bound is the same
  `KEY_CACHE_TTL_SECONDS`.
- 2026-07-29 - Invalidation clears the whole cache rather than one entry. The cache is keyed by
  sha256 of the secret while admin mutations know only `key_id`, so targeted invalidation would
  need a reverse index for an operation that happens a few times a day and costs at most one
  SQLite lookup per active key afterwards.
- 2026-07-29 - `keycache.py` imports `app.services.keys` as a module rather than importing
  `hash_secret` directly. `keys.py` calls `keycache.invalidate()`, so the two modules import
  each other; a module import defers the attribute lookup to call time and keeps one definition
  of the secret hash.
- 2026-07-29 - The check route is `async def`, so its cache-miss SQLite lookup runs through
  Starlette's `run_in_threadpool`. Reading the database on the event loop would block every
  other in-flight check, which is exactly what `docs/rules.md` forbids.
- 2026-07-29 - Revoked keys log `check.denied` with `deny_layer: "revoked_key"` rather than a
  new event key. `docs/rules.md` fixes the event key list and forbids inventing variants, and
  `deny_layer` is already a documented context field.
- 2026-07-29 - Tests set `REDIS_TIMEOUT_MS=5000` and rebuild the application's Redis pool per
  test (the `script` fixture). The 100 ms production default is a latency budget, not a
  correctness bound, and the 50-way concurrency tests would flake on a loaded runner; the pool
  is rebuilt because `redis.asyncio` connections bind to the event loop that created them and
  pytest-asyncio gives each test a fresh loop.
- 2026-07-29 - `test_mirror_never_over_credits_and_stays_in_bounds` asserts strict conservatism
  (credit never exceeds the exact rational amount) over a fixed seed, so the test is
  deterministic. The one floating-point step in the refill, `(elapsed / 1e6) * rate_utok`, has
  an absolute error under 5e-4 microtokens after the full-bucket clamp, so a knife-edge floor
  crossing is theoretically possible but does not occur for the sampled space; 300,000 random
  samples produced zero over-credits.
- 2026-07-27 - Rollups upsert with `used = max(existing, read)` and Redis counters are restored
  after data loss via a delta-based `INCRBY` inside `restore.lua` (never SET), so overlapping
  rollups, replayed runs, and concurrent live traffic can never lower a persisted value or lose
  increments. `redis_down_policy` defaults to `fail_open` because a limiter outage should not
  take down the customer's API; plans guarding paid quotas opt into `fail_closed`.
