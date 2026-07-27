# quotaguard - Architecture

## Overview

quotaguard is a single FastAPI process with two backing stores. Redis holds all admission state
(buckets, window logs, month counters, soft-threshold flags, the rollup lock) and executes every
admission decision inside one Lua script, so a decision is atomic and needs exactly one round
trip. SQLite (SQLAlchemy + Alembic) holds configuration and history: plans, api keys with
per-key overrides, monthly rollups, and the webhook outbox. The hot path (`POST /v1/check`)
touches only Redis plus an in-process key cache; SQLite is touched on admin calls, usage reports,
cache misses, and background jobs (two asyncio loops: rollup and webhook delivery).

## Flow

```
Gateway: POST /v1/check
     -> resolve key (in-process cache, TTL 5s; miss -> SQLite by sha256(api_key))
        unknown/revoked -> 200 allowed=false (reason unknown_key / revoked_key)
     -> EVALSHA check.lua   (ONE round trip; Redis TIME is the only clock)
          |- token bucket   qg:b:{key_id}:{resource}   burst layer
          |- sliding window qg:s:{key_id}:{resource}   sustained layer
          |- monthly quota  qg:q:{key_id}:{YYYY-MM}    hard + soft threshold
          |- all three allow -> consume from all three; else consume nothing
     -> decode tuple -> 200 JSON decision + draft RateLimit headers
        soft_crossed=1 -> insert webhook_outbox row (unique key+month+kind)
        Redis error -> plan policy: fail_open (allow, degraded) | fail_closed (deny)

Admin (X-Admin-Token) or CLI: plans/keys CRUD, overrides, revoke -> SQLite; cache invalidated

Background (asyncio loops in the same process)
  rollup loop  -> lock qg:lock:rollup -> SCAN qg:q:* -> upsert rollups (monotonic)
                  -> restore low Redis counters -> reconcile missed soft webhooks
  webhook loop -> due outbox rows -> POST plan.webhook_url -> 2xx | retry backoff | exhausted
```

### Why one Lua script (and not pipelines or app-side math)

A pipeline is atomic in execution but cannot branch on intermediate results, so check-then-consume
becomes a race: two concurrent checks both read remaining=1 and both admit. App-side math has the
same race plus a second clock. A single Lua script executes serially on the Redis event loop:
it reads the clock once (`TIME`), evaluates all three layers, and mutates only if all allow, so
concurrency correctness is a property of the one Redis, not of application locking. The cost:
the script is the most safety-critical code here; it is specified fully below, implement exactly.

## Proposed folder / file tree

```
quotaguard/
├── app/
│   ├── __init__.py
│   ├── main.py              # app factory, routers, handlers, lifespan (load scripts, start loops)
│   ├── config.py            # Settings (pydantic-settings), read once via get_settings()
│   ├── logging.py           # structured JSON logging + request-id middleware
│   ├── errors.py            # AppError types, exception handlers, error envelope
│   ├── db.py                # engine + sessionmaker from DATABASE_URL
│   ├── models.py            # Plan, ApiKey, Rollup, WebhookOutbox
│   ├── deps.py              # get_db, get_settings, require_admin, require_service_token
│   ├── redis_client.py      # redis.asyncio pool, timeouts, script registry (SHA + NOSCRIPT retry)
│   ├── lua/
│   │   ├── check.lua        # the atomic admission script (spec below)
│   │   ├── restore.lua      # raise a quota counter to a floor value atomically
│   │   └── unlock.lua       # compare-and-delete advisory lock release
│   ├── routers/
│   │   ├── health.py        # GET /health
│   │   ├── check.py         # POST /v1/check (async def; Redis only)
│   │   ├── usage.py         # GET /v1/usage/{key_id}
│   │   └── admin.py         # /admin/plans, /admin/keys (sync def; SQLAlchemy)
│   ├── schemas/             # check.py, plans.py, keys.py, usage.py, errors.py (Request/Out models)
│   ├── services/
│   │   ├── checker.py       # marshal ARGV, decode tuple, headers, fail-open/closed policy
│   │   ├── keycache.py      # TTL cache: sha256(api_key) -> resolved key + effective limits
│   │   ├── keys.py          # issue (secret gen + hash), override, revoke
│   │   ├── plans.py         # plan CRUD + validation bounds
│   │   ├── usage.py         # live month (Redis GET) + history (rollups)
│   │   ├── rollup.py        # advisory-locked rollup, restore, reconciliation
│   │   └── webhooks.py      # outbox enqueue + delivery with retries and signing
│   ├── scheduler.py         # asyncio loops (rollup, webhook delivery), lifespan-managed
│   └── cli.py               # python -m app.cli; argparse subcommands (in-process, no HTTP)
├── migrations/              # Alembic: env.py, versions/0001_init.py, 0002_rollups_outbox.py
├── tests/                   # see docs/testing.md
├── .github/workflows/ci.yml # lint + tests with a real Redis service container
└── .env.example, alembic.ini, pyproject.toml, uv.lock, README.md
```

## Tech stack with rationale

Python 3.12 (the workspace baseline for Python services). Major versions below; exact versions
pinned (`==`) in `pyproject.toml` at install time, `uv.lock` committed.

- **FastAPI + Uvicorn** - typed models, dependency injection for auth and sessions. The check
  path is `async def` on `redis.asyncio` (one awaited Redis call; a threadpool hop would double
  its latency); admin/usage routes are plain `def` (threadpool) on sync SQLAlchemy. Mixing is a
  supported pattern and keeps the ORM layer synchronous and simple.
- **redis-py 5 (`redis.asyncio`)** - the only Redis client; explicit socket and connect timeouts
  from `REDIS_TIMEOUT_MS` on every command. Requires **Redis 7.x** (scripts call `TIME`, allowed
  under the default effects replication).
- **SQLAlchemy 2.0 + Alembic** - typed models, parameterized queries, real migrations. SQLite is
  the only supported database (single-instance is a design decision); `DATABASE_URL` stays a URL
  so Postgres is not precluded later.
- **httpx** - webhook delivery only (sync client, explicit timeouts, called from a threadpool
  inside the delivery loop); also the test client (`httpx.ASGITransport`).
- **pytest / Ruff / Black** - tests, lint, format; managed with `uv`. Tests require a real Redis;
  CI provides one as a service container (see `docs/testing.md`).
- **stdlib argparse** for the CLI - no CLI framework dependency.

No other runtime dependencies. Anything beyond this list requires approval per `docs/rules.md`.

## Data model (SQLite)

Timestamps are timezone-aware UTC, ISO-8601 serialized. Every table has an autoincrement INTEGER
`id` PK plus `created_at`/`updated_at`; those three columns are omitted from the tables below.

### `plans`

| column                   | type    | notes                                                    |
|--------------------------|---------|----------------------------------------------------------|
| slug                     | TEXT    | UNIQUE NOT NULL, 1-64 chars `[a-z0-9-]`                  |
| name                     | TEXT    | NOT NULL, 1-128 chars                                    |
| burst_capacity / burst_refill_per_sec | INTEGER | tokens 1..1,000,000; tokens/second 1..100,000 |
| sustained_limit / sustained_window_seconds | INTEGER | requests 1..1,000,000; seconds 1..86,400 |
| monthly_quota            | INTEGER | hard cap, 1..1,000,000,000,000                           |
| quota_soft_pct           | INTEGER | 0..100; 0 disables the soft threshold                    |
| webhook_url              | TEXT NULL | required when quota_soft_pct > 0; http/https, <=2048    |
| redis_down_policy        | TEXT    | CHECK in (`fail_open`, `fail_closed`), default `fail_open` |

The numeric bounds are enforced at validation time and are load-bearing: they keep every
intermediate in the Lua arithmetic far below 2^53 (see refill math below).

### `api_keys`

| column                   | type    | notes                                                    |
|--------------------------|---------|----------------------------------------------------------|
| key_id                   | TEXT    | UNIQUE NOT NULL; public id `k_` + 12 hex; used in Redis key names and admin URLs |
| key_hash / key_prefix    | TEXT    | UNIQUE sha256 hex of the full secret (check lookup index); first 12 chars for display |
| name                     | TEXT    | NOT NULL, 1-128 chars                                    |
| plan_id                  | INTEGER FK -> plans.id | indexed                                   |
| override_burst_capacity, override_burst_refill_per_sec, override_sustained_limit, override_sustained_window_seconds, override_monthly_quota | INTEGER NULL | per-key overrides; same bounds as the plan column when set |
| revoked_at               | TIMESTAMP NULL | set once; revoked keys are never deleted          |

The full secret is `qk_` + 43 url-safe base64 chars (32 random bytes) and exists only in the
issue response. Effective limits = override when non-null, else the plan value; resolved once at
key-cache fill time.

### `rollups`

| column      | type    | notes                                                       |
|-------------|---------|-------------------------------------------------------------|
| api_key_id  | INTEGER FK -> api_keys.id |                                           |
| month       | TEXT    | `YYYY-MM` (UTC)                                             |
| used        | INTEGER | persisted month counter; only ever raised (monotonic upsert)|
| quota_limit | INTEGER | effective monthly quota snapshotted at rollup time          |

UNIQUE `(api_key_id, month)` - the rollup idempotency guarantee.

### `webhook_outbox`

| column          | type    | notes                                                   |
|-----------------|---------|---------------------------------------------------------|
| api_key_id      | INTEGER FK -> api_keys.id |                                       |
| month           | TEXT    | `YYYY-MM`                                               |
| kind            | TEXT    | CHECK in (`quota_soft`)                                 |
| payload         | TEXT    | JSON body to POST, built at enqueue time                |
| attempts / next_attempt_at | INTEGER / TIMESTAMP | default 0; due time, backoff-advanced on failure |
| delivered_at    | TIMESTAMP NULL | set on 2xx; terminal                             |
| last_error      | TEXT NULL | last failure detail (status or exception class)       |

UNIQUE `(api_key_id, month, kind)` - at most one soft warning per key per month, race-safe
against concurrent enqueue and reconciliation.

## Redis keyspace

`{key_id}` is `api_keys.key_id`; `{resource}` is the validated resource from the check request.

| key                          | type   | content                            | TTL |
|------------------------------|--------|------------------------------------|-----|
| `qg:b:{key_id}:{resource}`   | HASH   | `t` tokens in microtokens, `ts` last refill in epoch microseconds | full-refill time + 60s |
| `qg:s:{key_id}:{resource}`   | ZSET   | one member per admitted request, score = admit time in epoch microseconds | window + 60s |
| `qg:q:{key_id}:{YYYY-MM}`    | STRING | integer used count for the month   | month end + 45 days |
| `qg:f:{key_id}:{YYYY-MM}`    | STRING | `1` once the soft threshold fired  | month end + 45 days |
| `qg:lock:rollup`             | STRING | lock token                         | 120s |

Memory bound per (key, resource): the bucket hash is two small fields; the ZSET can never exceed
`sustained_limit` members (admission requires cardinality < limit after trim), roughly 64 bytes
each. Resource strings are validated (1-128 chars, `[a-zA-Z0-9_.:/-]`) and come from trusted
gateways, so cardinality is operator-controlled. Redis must run with `maxmemory-policy
noeviction`: evicting limiter state silently resets limits (launch checklist).

## The check script (`app/lua/check.lua`)

**KEYS**: [1] bucket `qg:b:{key_id}:{resource}`, [2] window `qg:s:{key_id}:{resource}`,
[3] quota prefix `qg:q:{key_id}:`, [4] flag prefix `qg:f:{key_id}:`. The quota and flag keys are
prefixes to which the script appends `YYYY-MM`, because only the script knows the month (see
Clock handling); building key names in-script is incompatible with Redis Cluster - a stated
non-goal boundary, recorded in the decisions log.

**ARGV** (integers except 8, all validated app-side against the plan bounds): [1] `cap_utok`
burst capacity in microtokens (capacity * 1,000,000); [2] `rate_utok` refill rate in microtokens
per second; [3] `sustained_limit`; [4] `window_us` sustained window in microseconds;
[5] `quota_hard`; [6] `quota_soft` absolute count, precomputed app-side as
floor(hard * pct / 100), 0 disables; [7] `cost` 1..1000, applies to the bucket and the quota (the
window always counts 1); [8] `request_id` opaque unique string (uuid4 hex from the app) used only
to make the ZSET member unique, never as a time source.

**Algorithm** (exact; steps 1-6 read, step 7 writes only when admitted):

1. `TIME` -> `now_s`, `frac_us`; `now_us = now_s * 1000000 + frac_us`. This is the only clock.
2. Derive `(year, month)` from `now_s` via the civil-calendar conversion (below); build zero-padded
   `month_str`, `qkey = KEYS[3] .. month_str`, `fkey = KEYS[4] .. month_str`, and `month_end_s` =
   epoch seconds of the next month's first instant (UTC).
3. **Bucket read + refill.** `HMGET` bucket `t`, `ts`; if absent, `tokens = cap_utok`,
   `ts = now_us`. Else `elapsed = max(0, now_us - ts)` (clamp: TIME can regress across a
   failover). If `elapsed >= (cap_utok / rate_utok) * 1e6` the bucket is full: `tokens =
   cap_utok`, `ts = now_us`. Otherwise `add = floor((elapsed / 1e6) * rate_utok)`; if `add > 0`:
   `tokens = min(cap_utok, tokens + add)`; if `tokens == cap_utok` then `ts = now_us` else
   `ts = ts + ceil(add * 1e6 / rate_utok)` (advance by at least the time the credited tokens are
   worth, so rounding only under-credits; the advance never passes `now_us` because `add` was
   floored). Burst allows iff `tokens >= cost * 1e6`.
4. **Window trim + read.** `ZREMRANGEBYSCORE` window `-inf` `(now_us - window_us)` on every check,
   allow or deny (the memory bound); `count = ZCARD`; sustained allows iff `count + 1 <= sustained_limit`.
5. **Quota read.** `used = tonumber(GET qkey) or 0`. Quota allows iff `used + cost <= quota_hard`.
6. **Decision.** `allowed = burst and sustained and quota`. `deny_layer` = first denier in the
   fixed order burst, sustained, quota; empty string when allowed.
7. **Consume (only when allowed).** `tokens = tokens - cost * 1e6`; `HSET` bucket `t`, `ts`;
   `PEXPIRE` bucket `ceil(cap_utok / rate_utok) * 1000 + 60000`. `ZADD` window `now_us`
   `now_us .. "-" .. request_id`; `PEXPIRE` window `window_us / 1000 + 60000`.
   `new_used = INCRBY qkey cost`; if `new_used == cost` (first write this month)
   `EXPIREAT qkey month_end_s + 3888000` (45 days grace).
   **Soft threshold**: if `quota_soft > 0 and used < quota_soft and new_used >= quota_soft` then
   `soft_crossed = SETNX fkey 1` (1 for exactly one caller ever, even under concurrency) and on
   success `EXPIREAT fkey month_end_s + 3888000`. On deny nothing is written (the step 4 trim
   removes only already-expired members and is the single allowed mutation on the deny path).
8. **Reporting values.**
   - burst: `limit` = cap tokens, `remaining = floor(tokens / 1e6)` (post-decision),
     `reset = ceil((cap_utok - tokens) / rate_utok)` seconds until full; on a burst deny
     `retry = ceil((cost * 1e6 - tokens) / rate_utok)` seconds until this cost could pass.
   - sustained: `limit`, `remaining = sustained_limit - count'` (post-decision),
     `reset = ceil((oldest_score + window_us - now_us) / 1e6)` for the first member after trim
     (0 when empty); on deny `retry = reset`.
   - quota: `limit = quota_hard`, `used` (post-decision), `remaining = quota_hard - used`,
     `reset = month_end_s - now_s`; on deny `retry = reset`.
   - `retry_after` = max `retry` over all denying layers; 0 when allowed.

**Return tuple** (flat array; redis-py delivers integers and byte strings):

```
{ allowed, deny_layer, b_limit, b_remaining, b_reset, s_limit, s_remaining, s_reset,
  q_limit, q_used, q_remaining, q_reset, soft_crossed, month_str, retry_after }
```

### Clock handling

Every timestamp in the decision comes from `redis.call('TIME')` inside the script: refill math,
window scores and cutoffs, the month derivation, and all reset values. Application clocks never
participate; skewed gateways get identical decisions because the single Redis is the single time
authority. The month must therefore be derived in-script, which is why the quota key is built
from a prefix: the client cannot name a month-bearing key without consulting a clock.

Civil-calendar conversion (Lua, integer arithmetic; the standard civil-from-days construction):
`days = floor(now_s / 86400)`; `z = days + 719468`; `era = floor(z / 146097)`;
`doe = z - era * 146097`;
`yoe = floor((doe - floor(doe/1460) + floor(doe/36524) - floor(doe/146096)) / 365)`;
`y = yoe + era * 400`; `doy = doe - (365*yoe + floor(yoe/4) - floor(yoe/100))`;
`mp = floor((5*doy + 2) / 153)`; `m = mp < 10 and mp + 3 or mp - 9`; `if m <= 2 then y = y + 1
end`. `month_end_s` is the inverse (days-from-civil) applied to the first day of the following
month, times 86400. Tests cross-check both against Python's `calendar` over a wide timestamp
range (see `docs/testing.md`).

### Integer microtoken refill math

Bucket state is stored as integers only: microtokens (1 token = 1,000,000 utok) and epoch
microseconds. Lua numbers are doubles, exact for integers below 2^53; the plan bounds keep every
stored value and intermediate well below that (`now_us` about 1.8e15; the refill product, after
the full-bucket clamp, is bounded by `cap_utok` = 1e12). The one non-integer step is the
`(elapsed / 1e6) * rate_utok` division, whose floating error is far below one microtoken; the
result is floored and the `ts` advance is ceiled, so every rounding under-credits. Net guarantee:
per-refill error < 1 utok, always conservative, never accumulated (stored state is integral).

## Header semantics

quotaguard implements **draft-ietf-httpapi-ratelimit-headers-07** (the structured-field
dictionary revision), returning ready-to-emit strings the gateway copies verbatim:

- `RateLimit: limit=<l>, remaining=<r>, reset=<s>` - one policy: on deny, the denying layer; on
  allow, the most constrained layer (smallest remaining/limit ratio; tie broken by shorter
  window). `reset` is delta seconds.
- `RateLimit-Policy: <q1>;w=<w1>, <q2>;w=<w2>, <q3>;w=<w3>` - all policies in order burst,
  sustained, monthly. The bucket renders as quota `burst_capacity` in window
  `ceil(capacity / refill_per_sec)`; the monthly window is the current UTC month's real seconds.
- `Retry-After: <s>` - deny only; the script's `retry_after`.

Later draft revisions renamed these fields to partitioned named policies; the -07 pin and its
upgrade path are in the decisions log. The raw integers always accompany the strings, so a caller
on a different revision can render its own headers.

## Failure modes and handling

| Failure | Handling |
|---|---|
| Redis unreachable, timeout, or command error on a check | Per-plan `redis_down_policy`: `fail_open` -> `allowed: true, degraded: true`, header fields null; `fail_closed` -> `allowed: false`, reason `redis_unavailable`, `degraded: true`. Both answer 200 within `REDIS_TIMEOUT_MS` + connect timeout, log `check.redis_down` at WARNING with the policy taken, and consume nothing. `NOSCRIPT` after a Redis restart is not an outage: the registry reloads from `app/lua/` and retries the EVALSHA once; a second failure follows the policy. |
| Redis data loss (restart without persistence, FLUSHALL) | Buckets and windows re-initialize full/empty: a bounded over-admission window equal to one burst + one sustained window, accepted and documented. Month counters restart at 0 but are restored by the next rollup run: `restore.lua` reads the current value and `INCRBY`s the shortfall to the persisted rollup value in one atomic script, so concurrent live increments are never lost and the counter is only ever raised. |
| SQLite error on admin/usage/rollup | 500 `internal_error` (logged with detail); the check path is unaffected because resolved keys are cached. A cache-miss lookup that hits a database error also returns 500 - SQLite is a local file and no availability policy is built for it. |
| Webhook endpoint down or non-2xx | Outbox row retried on the backoff schedule `min(30 * 2^(n-1), 3600)` seconds with jitter factor 0.8-1.2 up to `WEBHOOK_MAX_ATTEMPTS`; then `webhook.exhausted` is logged at ERROR and the row is kept undelivered for manual inspection. Delivery never blocks or slows the check path. |
| Rollup overlap (scheduler + CLI, or a duplicate process) | Advisory lock `SET qg:lock:rollup <token> NX PX 120000`; the loser logs `rollup.skipped_locked` and exits. Release is compare-and-delete (`unlock.lua`) so an expired lock is never deleted by a stale holder. A run must finish within the lock TTL; the SCAN is batched (COUNT 500) and bounded by live key count. |
| Process crash between soft-flag SETNX and outbox insert | The flag is set but no webhook row exists; rollup reconciliation inserts the missing row (used >= threshold and no outbox row for key+month+kind). The outbox unique constraint makes hot-path enqueue and reconciliation race-safe. |
| Month boundary during traffic | The month is derived per script run from Redis TIME; checks straddling the boundary atomically hit different month keys, no torn state. |
| Key revoked or limits changed | The in-process cache is invalidated immediately by the admin mutation; worst-case staleness elsewhere is `KEY_CACHE_TTL_SECONDS`. |

## Correctness invariants

1. **No over-admission.** All admission state is read and written inside one Lua script, which
   Redis executes serially: N concurrent checks against remaining budget M admit exactly
   min(N, M). Asserted by a concurrency test against real Redis, re-run on any script change.
2. **All-or-nothing consumption.** A deny consumes nothing from any layer; its only write is
   trimming already-expired window members.
3. **One clock.** Redis TIME inside the script is the only time source for decisions and resets.
4. **Conservative integer math.** Bucket state is integral; every rounding in the refill path
   under-credits by less than one microtoken and never accumulates.
5. **Idempotent rollups.** Upsert on unique `(api_key_id, month)` with `used = max(existing,
   read)`; the counter is monotonic within a month, so replays and overlaps converge and never
   lower a persisted value. The lock only prevents wasted work (but `restore.lua` runs solely
   under it).
6. **At-most-once soft warning per key per month.** SETNX flag (exactly one crossing caller) +
   outbox unique constraint (dedupes enqueue vs reconciliation) + `delivered_at` terminality.
7. **Bounded memory.** Window ZSETs are capped at `sustained_limit` members and trimmed every
   check; every Redis key carries a TTL; month keys outlive the month by 45 days so rollups run
   against live data.
8. **Secrets stay hashed.** The api key secret exists in plaintext only in the issue response.

## Where state lives

- **Redis** - all admission state and the rollup lock. AOF everysec is the recommended
  deployment (launch checklist); loss is bounded and recoverable per the failure table.
- **SQLite (`DATABASE_URL`)** - plans, keys, rollups, webhook outbox, `alembic_version`; the only
  place configuration and history survive; one file to back up.
- **Process memory** - the key cache and loaded script SHAs; a restart costs only cold caches.
- **Client state** - none; the gateway holds nothing beyond the headers it copies.

Deployment assumption, stated plainly: one API process (uvicorn `--workers 1`). In-process cache
invalidation and the scheduler assume it; more workers stay correct (Redis and the advisory lock
do not care) but widen revocation latency to the cache TTL. Recorded in the launch checklist.

## External dependencies and required environment variables

External runtime services: Redis 7.x (required), the operator's webhook endpoint (optional).

| variable                  | required | default                          | purpose                                   |
|---------------------------|----------|----------------------------------|-------------------------------------------|
| `REDIS_URL`               | yes      | -                                | e.g. `redis://localhost:6379/0`           |
| `DATABASE_URL`            | no       | `sqlite:///./data/quotaguard.db` | SQLAlchemy URL                            |
| `QG_ADMIN_TOKEN`          | yes      | -                                | `X-Admin-Token` for admin routes + usage  |
| `QG_SERVICE_TOKEN`        | no       | (unset)                          | when set, `POST /v1/check` requires it    |
| `REDIS_TIMEOUT_MS`        | no       | `100`                            | socket + connect timeout per command      |
| `KEY_CACHE_TTL_SECONDS`   | no       | `5`                              | key cache staleness bound                 |
| `ROLLUP_INTERVAL_SECONDS` | no       | `300`                            | rollup loop period; `0` disables the loop |
| `WEBHOOK_TIMEOUT_SECONDS` | no       | `5`                              | per-attempt webhook POST timeout          |
| `WEBHOOK_MAX_ATTEMPTS`    | no       | `5`                              | attempts before `webhook.exhausted`       |
| `QG_WEBHOOK_SECRET`       | no       | (unset)                          | when set, sign webhook bodies (HMAC-SHA256) |
| `LOG_LEVEL`               | no       | `INFO`                           | structured logger level                   |

`.env.example` mirrors this table with dummy values.
