# quotaguard - Rules and Conventions

Binding for anyone implementing quotaguard. Specific to this project; read alongside
`docs/architecture.md` and the workspace rules.

## Conventions

### Preferred libraries and patterns
- HTTP layer: FastAPI, one `APIRouter` per resource in `app/routers/`. Routers parse/validate,
  call a service, shape the response. No business logic, no Redis calls, and no SQLAlchemy
  queries beyond trivial lookups in routers.
- Async discipline: `async def` only for routes and services that touch nothing but Redis (the
  check path). Anything touching SQLAlchemy is a plain `def` route (threadpool) or runs in the
  scheduler's threadpool executor. Never block the event loop with SQLite or httpx calls.
- Redis access: only through `app/redis_client.py`. Every command carries the configured
  timeouts. All writes to `qg:*` admission keys happen inside the Lua scripts - Python never
  mutates a bucket, window, quota counter, or soft flag directly (the lock and `restore.lua`
  are the scripted exceptions).
- Lua scripts: live as files in `app/lua/`, loaded once at startup, invoked by SHA with one
  NOSCRIPT reload-and-retry. The scripts are the contract surface of the product: any change to
  `check.lua` requires re-running the concurrency tests and owner sign-off, like a migration.
- Time: `redis.call('TIME')` inside scripts is the only clock for admission decisions, window
  scores, months, and resets. `datetime.now(timezone.utc)` is allowed only for SQLite audit
  columns, outbox scheduling, and log timestamps - never in admission math.
- Validation and config: Pydantic v2 models in `app/schemas/`; pydantic-settings in
  `app/config.py`, read once via a cached `get_settings()` dependency. Plan bounds from
  `docs/architecture.md` are enforced in the schema layer; they are load-bearing (Lua 2^53).
- Persistence: SQLAlchemy 2.0 in `app/models.py`, session-per-request via `get_db`. ORM/`select()`
  style only; no `text()` SQL with interpolated values, ever. The rollup upsert uses SQLite
  `ON CONFLICT DO UPDATE` with bound parameters.
- Background work: the two asyncio loops in `app/scheduler.py`, started and stopped by the
  lifespan hook. No Celery, no threads beyond the executor, no extra queues; the outbox table is
  the queue.
- IDs: api key secrets `qk_` + 43 url-safe chars; public key ids `k_` + 12 hex; request ids
  `uuid4().hex`. Timestamps timezone-aware UTC, ISO-8601; JSON fields snake_case.

### What to avoid
- No retry/circuit-breaker libraries (tenacity etc.); the webhook backoff and NOSCRIPT retry are
  a few explicit lines each and stay inspectable.
- No caching libraries for the key cache; it is a dict with expiries in `keycache.py`.
- No global mutable state except the key cache and the script SHA registry, each owned by its
  module.
- No `print()` outside `app/cli.py` (whose stdout is its interface); everywhere else, the
  structured logger.
- No auto-generated Alembic migrations committed unreviewed; write/verify each by hand.
- No new Redis key patterns outside the table in `docs/architecture.md`.

### Naming
- PEP 8: `snake_case` functions/variables/modules, `PascalCase` classes, `UPPER_SNAKE` constants.
- Request models end in `Request`, response models in `Out`. Error codes and log event keys are
  exact strings from `docs/api-contracts.md` and this file - never invent variants.
- Commit format: Conventional Commits, `type(scope): subject`, imperative, lower case, no
  trailing period, under ~70 chars. Scopes: `config`, `logging`, `errors`, `db`, `auth`,
  `plans`, `keys`, `keycache`, `redis`, `lua`, `check`, `quota`, `usage`, `rollup`, `webhooks`,
  `cli`, `health`, `ci`. One commit per feature/task, exactly as listed in `docs/phases.md`.

### Dependencies and migrations
- `uv` with `pyproject.toml`; every dependency pinned exact (`==`); `uv.lock` committed.
  Dependency changes are their own `build:` commit and need approval first. Only dependencies
  named in `docs/architecture.md` are pre-approved.
- Every schema change is an Alembic migration, forward-only; never edit an applied migration.

## Error handling and logging

### Failures to handle explicitly
- **Every Redis command on the check path**: connection error, timeout, NOSCRIPT, script error
  -> the plan's `redis_down_policy`, a `check.redis_down` WARNING, `degraded: true`. The check
  endpoint never 500s because Redis is down.
- **Webhook delivery**: timeout, connection error, non-2xx -> backoff retries to the cap, then
  `webhook.exhausted` at ERROR with the row kept. A delivery exception never kills the loop.
- **Rollup**: lock contention -> skip and log; a per-key failure logs with `key_id` and
  continues; the run reports counts. A crashed loop iteration is caught, logged, and retried
  next interval.
- **SQLite**: session errors roll back; admin/usage return 500 `internal_error` with detail in
  logs only.
- **CLI**: expected failures print `error: <one sentence>` to stderr and exit non-zero; never a
  traceback for expected failures.

### Friendly API errors vs detailed logs
- API error responses carry a short human message and a stable machine `code` - never stack
  traces, SQL, key secrets, or redis-py internals. Full detail goes to the log. A catch-all
  handler converts unhandled exceptions to `500 internal_error` with the standard body.
- One consistent JSON error format (see `docs/api-contracts.md`):
  `{"error": {"code": "validation_error", "message": "..."}}`. Codes: `validation_error` (422),
  `unauthorized` (401), `not_found` (404), `conflict` (409), `internal_error` (500).

### Structured logging from day one
- One JSON object per line: `timestamp`, `level`, `logger`, `message`, plus context
  (`request_id`, `route`, `status_code`, `duration_ms`, `key_id`, `resource`, `deny_layer`,
  `policy`, `month`, `error_code` as applicable). Request-id middleware echoes `X-Request-ID`.
- Fixed event keys: `check.denied`, `check.degraded`, `check.unknown_key`, `check.redis_down`,
  `quota.soft_crossed`, `rollup.completed`, `rollup.skipped_locked`, `rollup.restored`,
  `webhook.delivered`, `webhook.retry`, `webhook.exhausted`, `key.issued`, `key.revoked`,
  `key.overridden`, `plan.created`, `plan.updated`. Allowed checks log at DEBUG only - the hot
  path must not pay for INFO logging per request.

## Security

- **No hardcoded secrets** in source, tests, or fixtures. Real values in `.env` (git-ignored);
  `.env.example` kept current with dummies; CI secrets only via the CI secret store.
- **Key secrets are hashed**: sha256 at issue time; the plaintext secret appears only in the
  issue response and is never logged (log `key_id`/`key_prefix` instead).
- **Token comparisons are constant-time** (`hmac.compare_digest`) for `X-Admin-Token` and
  `X-Service-Token`; tokens never appear in logs or error bodies.
- **Server-side validation of all input**: check body, resource charset, plan bounds, override
  bounds, months parameter. Parameterized queries only (ORM-enforced).
- **Webhook signing**: when `QG_WEBHOOK_SECRET` is set, `X-QuotaGuard-Signature` is HMAC-SHA256
  over the exact body; the secret is never logged. Webhook URLs are operator-configured, so SSRF
  exposure is an accepted, documented trade-off.

## Simplicity and boundaries

- Build only what the current phase requires; no speculative flags or endpoints. Rule of three
  before any abstraction; no new wrapper/manager/utils modules without owner approval. A
  solution over ~150 lines needs written justification before continuing.
- Never modify `docs/PRD.md` or `docs/architecture.md` without flagging the change and getting
  sign-off. No wholesale file rewrites. Ambiguous task -> ask. Two failed fix attempts -> stop
  and report. Mid-phase scope -> classify with the owner (current phase, new phase, or Backlog).
- No emoji and no authorship attribution of any kind anywhere: code, comments, commits, docs.
