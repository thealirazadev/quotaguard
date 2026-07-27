# quotaguard - Phases

Rule: phase N+1 does not start until the owner approves phase N. Within a phase, commit in the
listed order - one commit per feature/task, Conventional Commits.

Ordering rationale: Phase 1 builds the configuration surface (plans, keys, overrides, revoke)
that every check needs, plus the project skeleton and CI with a real Redis from day one. Phase 2
is the product core: the atomic Lua check script and the check endpoint - the senior
differentiators (single-round-trip atomicity, integer refill math, in-script clock and month
derivation, bounded window memory) all land here and may not slip. Phase 3 closes the quota
lifecycle: soft-threshold webhooks, idempotent rollups, counter restore, and usage reports.
Phase 4 hardens the failure surface: the per-plan Redis-down policies, degraded responses, and
operational logging.

---

## Phase 1 - Foundation and the admin surface

**Goal**: a fresh checkout installs, migrates, lints, and tests green in CI with a real Redis
service container; plans and keys are fully manageable via admin API and CLI.

### Tasks
- Scaffold: `pyproject.toml` (pinned deps), `uv.lock`, Ruff/Black config, `.gitignore`,
  `.env.example` mirroring the env table in `docs/architecture.md`.
- `app/config.py` settings; `app/logging.py` JSON logging + request-id middleware;
  `app/errors.py` envelope + handlers; `app/main.py` app factory; `GET /health`.
- Alembic migration `0001_init`: `plans` and `api_keys` exactly as specified in
  `docs/architecture.md`, including uniques and the CHECK on `redis_down_policy`.
- Auth dependencies: `require_admin` (`X-Admin-Token`, constant-time) and
  `require_service_token` (no-op when `QG_SERVICE_TOKEN` unset).
- Admin API: plan create/list/get/update; key issue (secret shown once, hash stored), list,
  get, override (set and clear), revoke. Validation bounds enforced everywhere.
- CLI (`python -m app.cli`): `plan create`, `plan list`, `key issue`, `key list`,
  `key override`, `key revoke`; table output per `docs/design.md`.
- CI workflow: `ruff check`, `black --check`, `pytest` with the Redis 7 service container.

### Commits
1. `build: scaffold project with pyproject, uv lock, ruff and black config`
2. `chore: add env example and gitignore`
3. `feat(config): add settings module reading environment variables`
4. `feat(logging): add structured json logging and request id middleware`
5. `feat(errors): add error envelope, app error types, and exception handlers`
6. `feat(app): create fastapi app and register health route`
7. `feat(db): add plan and api key models with initial migration`
8. `feat(auth): add admin and service token dependencies`
9. `feat(plans): add plan admin endpoints with validation bounds`
10. `feat(keys): add key issue, list, and detail endpoints with hashed secrets`
11. `feat(keys): add override and revoke endpoints`
12. `feat(cli): add plan and key commands`
13. `build(ci): add workflow with lint, tests, and redis service container`
14. `test(phase1): cover admin api, cli, auth, and validation bounds`
15. `docs: add setup and admin instructions to readme`

### Verification checklist
- [ ] `uv sync`, `uv run alembic upgrade head` on a fresh clone; `uv run uvicorn app.main:app`
      starts clean; `GET /health` returns `{"status": "ok", ...}`.
- [ ] `uv run ruff check .`, `uv run black --check .`, `uv run pytest` all green locally and in
      CI (CI proves the service container works even though Redis is barely used yet).
- [ ] Create a plan via API and another via CLI; both appear in `GET /admin/plans` and
      `plan list`; a duplicate slug returns `409 conflict`; out-of-bounds numbers return `422`.
- [ ] Issue a key: the response contains the full `qk_` secret exactly once; the database row
      holds only hash + prefix; `key list` shows the prefix, never the secret.
- [ ] Override `sustained_limit` for one key, then clear it; `GET /admin/keys/{key_id}` reflects
      both states. Revoke the key; a second revoke returns `409`.
- [ ] Admin routes without `X-Admin-Token` (or with a wrong one) return `401`; `/health` stays
      open; with `QG_SERVICE_TOKEN` unset nothing else changes.
- [ ] All errors use the single envelope; logs are JSON lines with request ids; no secrets or
      tracebacks anywhere in responses or logs.

---

## Phase 2 - The atomic check path

**Goal**: `POST /v1/check` answers allow/deny with correct draft-07 header values in one Redis
round trip, with the full three-layer Lua script and proof of no over-admission.

### Tasks
- `app/redis_client.py`: pool from `REDIS_URL`, timeouts from `REDIS_TIMEOUT_MS`, script
  registry (load `app/lua/*.lua` at startup, EVALSHA with one NOSCRIPT reload-and-retry).
- `app/lua/check.lua` implemented exactly per the spec in `docs/architecture.md`: KEYS/ARGV,
  TIME-derived clock, civil-calendar month, microtoken refill, window trim, quota + soft SETNX,
  all-or-nothing consume, the 15-element return tuple.
- `services/keycache.py`: TTL cache keyed by sha256(api_key), resolving key + effective limits +
  plan policy; in-process invalidation wired into key/plan admin mutations.
- `POST /v1/check`: request validation (resource charset, cost bounds), unknown/revoked keys
  answered in-band, tuple decode, `RateLimit`/`RateLimit-Policy`/`Retry-After` rendering, layer
  detail in the JSON body.
- Tests: script behavior against real Redis (burst drain/refill, window trim and bounds, quota
  hard stop, soft SETNX exactly-once), mirror-math and civil-date cross-checks, and the
  concurrency over-admission test.

### Commits
1. `feat(redis): add redis client with timeouts and script registry`
2. `feat(lua): add atomic check script for bucket, window, and quota`
3. `feat(keycache): add ttl key cache with in process invalidation`
4. `feat(check): add check endpoint decoding the script tuple`
5. `feat(check): render draft ratelimit headers and retry after`
6. `test(phase2): cover refill math, window bounds, quota, and concurrency`
7. `docs: document check integration in readme`

### Verification checklist
- [ ] With a plan of capacity 5, refill 1/s: 5 rapid checks allow, the 6th denies with
      `reason: "burst"` and a sane `Retry-After`; after ~2 seconds two more checks allow
      (refill observed); `remaining` never goes negative.
- [ ] With sustained limit 3 per 2s window: the 4th check inside the window denies with
      `reason: "sustained"`; after the window slides it allows again; `ZCARD` of the window key
      never exceeds 3 (inspect with `redis-cli`).
- [ ] Set a monthly quota of 10 and drive 10 admitted checks: the 11th denies with
      `reason: "quota"`; `qg:q:{key_id}:{month}` equals 10 and carries a TTL; a denied check
      changes no Redis value (compare `HGETALL`/`ZCARD`/`GET` before and after).
- [ ] Concurrency: 50 parallel checks against a remaining budget of 10 admit exactly 10
      (the pytest concurrency test, and once manually with a parallel curl loop).
- [ ] Header strings match draft-07 syntax exactly (`RateLimit: limit=..., remaining=...,
      reset=...`); `RateLimit-Policy` lists burst, sustained, monthly in order; on allow the
      most constrained layer is reflected; two resources on one key limit independently.
- [ ] Unknown key and revoked key return 200 `allowed: false` with the documented reasons and no
      Redis writes; malformed body, bad resource, cost 0 or 1001 return `422`.
- [ ] Revoke a key: checks flip to `revoked_key` immediately (same process invalidation).
- [ ] `redis-cli monitor` during one check shows a single EVALSHA and nothing else.
- [ ] Lint, format, full pytest suite, and CI green.

---

## Phase 3 - Quota lifecycle: webhooks, rollups, usage

**Goal**: crossing the soft threshold produces exactly one delivered webhook per key per month;
monthly usage is durably rolled up, restorable, and reportable.

### Tasks
- Migration `0002`: `rollups` and `webhook_outbox` with their unique constraints.
- Enqueue: a check returning `soft_crossed=1` inserts the outbox row (payload built per
  `docs/api-contracts.md`); insert failures are logged, never fail the check response.
- Delivery loop: due rows POSTed with timeout and optional HMAC signature; 2xx marks delivered;
  failures backoff `min(30 * 2^(n-1), 3600)` seconds with 0.8-1.2 jitter up to
  `WEBHOOK_MAX_ATTEMPTS`, then `webhook.exhausted`.
- Rollup: advisory lock (`SET NX PX` + `unlock.lua`), SCAN `qg:q:*`, monotonic upsert, restore
  via `restore.lua` for counters below their rollup, reconciliation of missed soft webhooks;
  scheduler loop plus `rollup run` CLI command.
- `GET /v1/usage/{key_id}` (admin token): live current month + rollup history; `usage` CLI.

### Commits
1. `feat(db): add rollups and webhook outbox migration`
2. `feat(webhooks): enqueue soft threshold warnings from the check path`
3. `feat(webhooks): add delivery loop with retries and signing`
4. `feat(rollup): add advisory locked rollup with idempotent upserts`
5. `feat(rollup): restore quota counters from rollups after redis loss`
6. `feat(rollup): reconcile missed soft threshold webhooks`
7. `feat(usage): add usage report endpoint`
8. `feat(cli): add usage and rollup commands`
9. `test(phase3): cover rollup idempotency, webhooks, and usage`
10. `docs: document quota lifecycle in readme`

### Verification checklist
- [ ] Set quota 10, soft 80%, webhook to a local echo server: the check that crosses 8 returns
      `soft_threshold_crossed: true` exactly once; the echo server receives exactly one signed
      POST with the documented payload; further checks and even a re-crossing after restore fire
      nothing (flag + unique constraint).
- [ ] Point the webhook at a dead port: attempts increment on the backoff schedule (temporarily
      set `WEBHOOK_MAX_ATTEMPTS=2` to observe exhaustion); `webhook.exhausted` logged; the row
      keeps `last_error`; the check path latency is unaffected throughout.
- [ ] `rollup run` twice in a row: identical `rollups` rows (idempotent); run it while the
      scheduler loop fires: one side logs `rollup.skipped_locked`; concurrent CLI + scheduler
      never double-write.
- [ ] `FLUSHDB`, then `rollup run`: the current month counter is restored to the persisted
      value; live checks during the restore lose no increments (restore is delta-based INCRBY).
- [ ] Crash simulation: set the soft flag manually in Redis without an outbox row; the next
      rollup inserts the missing row (reconciliation).
- [ ] `GET /v1/usage/{key_id}?months=3` matches Redis for the current month and rollups for
      prior months; unknown key returns `404`; the CLI `usage` table matches the endpoint.
- [ ] Lint, format, full pytest suite, and CI green.

---

## Phase 4 - Failure policy and hardening

**Goal**: Redis outages degrade exactly as configured per plan, health reports honestly, and the
check path meets its latency budget.

### Tasks
- Fail-open/fail-closed in `services/checker.py` for every Redis failure class (connect,
  timeout, script error, NOSCRIPT-retry-failed); `degraded: true` responses per
  `docs/api-contracts.md`; recovery without restart.
- `GET /health` gains a `redis` field (`ok`/`down`) without ever failing the endpoint.
- Log events completed per `docs/rules.md` (allowed checks at DEBUG only); latency smoke test
  asserting the single-round-trip budget on the check path.
- README finalized (install, run, gateway integration, ops); `docs/testing.md` commands
  verified as written.

### Commits
1. `feat(check): add fail open and fail closed redis policies`
2. `feat(health): report redis status in health response`
3. `feat(logging): add check and scheduler log events`
4. `test(phase4): cover degraded paths and policy behavior`
5. `test(phase4): add latency smoke test for the check path`
6. `docs: finalize readme with integration and operations guide`

### Verification checklist
- [ ] Stop Redis: a fail-open plan's check returns `allowed: true, degraded: true` with null
      header fields; a fail-closed plan's returns `allowed: false, reason: "redis_unavailable"`;
      both answer within the timeout budget and log `check.redis_down` WARNING once per check.
- [ ] Start Redis again: checks recover to normal decisions without restarting quotaguard
      (NOSCRIPT path reloads the script; verify in logs).
- [ ] `GET /health` shows `redis: "down"` during the outage with HTTP 200, `redis: "ok"` after.
- [ ] Kill Redis mid-burst: no 500s from `/v1/check` at any point; SQLite-backed admin routes
      keep working throughout.
- [ ] Latency smoke test passes (p50 of local checks a few milliseconds; exactly one Redis
      command per check confirmed once more via `monitor`).
- [ ] Log review of a full manual run: only documented event keys, DEBUG silence at INFO level
      for allowed checks, no secrets, no tracebacks.
- [ ] Lint, format, full pytest suite, CI green; README instructions reproduce from a clean
      clone end to end.

---

## Phase verification (run at the end of every phase)

- [ ] `uv sync`, `uv run alembic upgrade head` on a fresh database, `uv run uvicorn app.main:app`
      starts with no warnings or errors.
- [ ] `uv run ruff check .`, `uv run black --check .`, `uv run pytest` clean (Redis running).
- [ ] Unhappy paths: malformed JSON -> `422`; wrong/missing tokens -> `401`; unknown ids ->
      `404`; duplicate slug / double revoke -> `409`; Redis stopped -> degraded per policy,
      never a 500 from `/v1/check`.
- [ ] Empty states: fresh database and empty Redis produce well-formed responses everywhere
      (empty lists, zero usage), not errors.
- [ ] Restart the process and confirm plans, keys, rollups, and outbox rows survive; Redis state
      keeps working with the restarted process (scripts reloaded).

## Backlog

- `quotaguard check` CLI command for local smoke-testing a key without curl - deferred, YAGNI
  until an operator asks.
- Per-key resource allowlist (reject unknown resources at check time) - deferred; resources are
  gateway-controlled in v1.
- Prometheus metrics endpoint - deferred; structured logs carry the same signals in v1.
- Postgres support - deliberately out; SQLite fits the single-instance design (see
  `docs/architecture.md`).
