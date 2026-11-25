# Design decisions

The engineering decisions behind quotaguard that the code reflects, the reason for each, and the
bugs or edge cases found along the way. The full Lua script spec, data model and failure table are
in `docs/architecture.md`; endpoint and CLI contracts are in `docs/api-contracts.md`.

## 1. One atomic Lua script per decision

Every check is a single `EVALSHA` of `app/lua/check.lua`, which evaluates the token bucket, the
sliding window and the monthly quota, and only then writes. If any layer denies, nothing is
consumed anywhere. This is what makes check-then-consume races impossible: fifty concurrent checks
against a remaining budget of ten admit exactly ten, which `tests/test_concurrency.py` asserts for
each layer as the binding constraint and over HTTP. The same harness run against a naive
check-then-consume implementation admits all fifty, so the test can actually fail.
`tests/test_latency.py` asserts one `EVALSHA` per check from Redis command stats.

## 2. Redis `TIME` is the only clock

The script reads `TIME` once and uses it for the refill, the window scores, the UTC month and every
reset value. Application servers never contribute a timestamp, so clock skew between them cannot
grant or steal capacity. Because the month is derived inside the script, the quota and soft-flag
key names are built from prefixes passed in `KEYS` rather than fully declared to `EVALSHA`;
declaring them client-side would need an application clock to name the month. The cost is Redis
Cluster incompatibility, which is acceptable because the design targets a single Redis node. If
`TIME` goes backwards across a failover, the refill never credits negative elapsed time.

## 3. Integer microtokens with rounding that always under-credits

Token state is kept in microtokens (1 token = 1,000,000). The refill floors the credited amount and
advances the stored timestamp by `ceil(add * 1e6 / rate)` instead of jumping to now, carrying the
fractional remainder as time not yet elapsed. Every rounding therefore lands on the stingy side:
the limiter can be short by under one microtoken per refill but can never over-admit. Plan
validation bounds keep all script arithmetic exact below 2^53, and a Python mirror of the formula
is property-tested for conservatism over a fixed seed in `tests/test_refill_math.py`.

## 4. Every number sent to Redis is formatted as an integer string

Bug class found while writing the script: Lua numbers are doubles, and the default number to
string conversion switches to scientific notation well below microsecond epoch magnitudes, which
would have written ZSET scores, members and bucket state as `1.785e+15`. Every value passed to
`redis.call` goes through `string.format('%.0f', value)`. This was the most likely silent
corruption in the script, and it is visible in `redis-cli monitor` output.

## 5. The shipped calendar code is tested, not a copy of it

The civil-calendar helpers that turn `TIME` into a UTC month and month end are wrapped in
`-- <civil>` sentinel comments. `tests/test_civil_dates.py` extracts that block verbatim, appends a
small harness, and runs it on the real Redis over every month boundary from 2020 to 2039, both
leap-day cases and the 2100 non-leap century. Redis `TIME` cannot be faked from a test, so testing
a Python reimplementation would prove nothing about the Lua that ships.

## 6. What `cost` charges, and how state is scoped

`cost` is charged to the token bucket and the monthly quota, but the sliding window always counts
one request. The window bounds request rate and its sorted set's memory equals its limit; letting
cost multiply window entries would break that bound and conflate work units with requests.
Short-window state is scoped per (key, resource) so one hot endpoint cannot starve the rest of a
key's traffic, while the monthly quota stays per key because that is the unit operators reason
about. The sustained `reset` is read after the consume, so an allowed check into an empty window
reports the full window length rather than 0.

Known edge case: a `cost` larger than the key's effective burst capacity can never be admitted, yet
the bucket layer still reports a finite `retry_after`, so a client that honours `Retry-After` will
retry indefinitely. A rule for impossible costs (reject at validation, or deny with no retry hint)
is the open fix.

## 7. Draft-07 rate limit headers

Headers follow draft-ietf-httpapi-ratelimit-headers-07 (`RateLimit: limit=..., remaining=...,
reset=...` plus `RateLimit-Policy` in burst, sustained, monthly order). Later drafts moved to named
policies; -07 maps one to one onto the response fields and matches what most deployed gateways
emit. Moving to a later draft is a rendering change only, because the raw integers always
accompany the header strings in the JSON body.

## 8. Unknown and revoked keys are answered in band

`POST /v1/check` always answers `200` with a decision. An unknown or revoked key gets
`allowed: false` with `reason` `unknown_key` or `revoked_key`, `layers: null`, and Redis is never
touched, so a flood of bad keys costs no Redis work. Secrets are `qk_` plus 32 random url-safe
bytes, and only their sha256 hash and a 12-character display prefix are stored, so a lost secret
can only be replaced.

## 9. An in-process key cache that cannot serve a stale revoke

Key lookups are cached by secret hash, including misses, so bogus keys do not cost a SQLite query
per request; a real secret cannot collide with a cached miss. Admin mutations clear the whole cache
because they know only `key_id`, not the hash. The cache-miss lookup runs in a threadpool so the
event loop never blocks on SQLite. Two bugs found in review:

- A lookup in flight while an admin revoked the key wrote the pre-revoke row back into the cache,
  so a revoked key kept resolving as live for a full TTL. Each store now carries the invalidation
  generation it was read under and is dropped if the generation moved.
- The cache had no size bound, so an unauthenticated caller sending distinct random secrets could
  grow process memory without limit. Past 10,000 entries the cache is dropped whole.

## 10. Redis outage policy is per plan

Any `RedisError` on the check path applies the plan's `redis_down_policy`: `fail_open` answers
`allowed: true, degraded: true`, `fail_closed` answers `allowed: false, reason:
"redis_unavailable"`, and either logs `check.redis_down` once. The default is `fail_open`, because a
limiter outage should not take down the customer's API; plans guarding paid quotas opt into
`fail_closed`. `/health` reports `redis: ok|down` and stays `200`, and admin routes keep working.
A `NOSCRIPT` reply (script cache flushed) is answered with one reload and retry rather than treated
as an outage. Known edge case: a socket timeout can expire after the script has already consumed,
so the policy then applies to a request that was also charged.

## 11. Monthly rollups that can only move forward

`rollup run` copies monthly counters from Redis to SQLite under a Redis lock (`SET NX PX` with a
random token, released by a compare-and-delete script), so concurrent runs skip instead of
interleaving. Rollup rows only ever increase `used`, so overlapping or replayed runs cannot lower a
persisted value. After Redis data loss, `restore.lua` raises a counter to the persisted floor with
`INCRBY` of the shortfall, never `SET`, so live traffic arriving during the restore is not lost.
Bugs found when the rollup path was first run against a real Redis:

- Restore called the async script registry from the synchronous job, created a coroutine that was
  never awaited, and silently restored nothing.
- Lock release fell back to a non-atomic `GET` then `DEL`, which could delete another run's lock.
- The usage report listed the current month twice after a rollup, returned one month too few
  without live data, and always reported `soft_threshold_crossed: false` for history; history now
  excludes the current month, falls back to the current-month rollup when Redis lost it, and takes
  the flag from the webhook outbox.

## 12. Soft-threshold webhooks through a persistent outbox

Crossing the soft threshold is detected in the script with `SETNX` on a per-key, per-month flag,
which returns 1 for exactly one caller even under concurrency, and the check path enqueues one
outbox row. A delivery loop sends it with optional HMAC signing and retries on a
`min(30 * 2^(n-1), 3600)` second backoff with 0.8 to 1.2 jitter, and the rollup reconciles flags
that have no outbox row. Two fixes found in testing: `webhook.exhausted` was logged on every pass
for every exhausted row instead of once, and the scheduler ran blocking HTTP and SQLite work on the
event loop, which could stall `/v1/check` behind a slow webhook receiver; both loops now run through
`asyncio.to_thread`.

## 13. API and CLI details that keep contracts exact

- SQLite returns naive datetimes for `DateTime(timezone=True)` columns, so one helper
  (`models.to_iso_utc`) treats a missing tzinfo as UTC and renders the `Z` suffix the contract
  shows, instead of `+00:00` or local time.
- PATCH bodies use `model_dump(exclude_unset=True)`, so an absent field and an explicit `null` are
  different requests: `null` clears a nullable field (a plan's `webhook_url`, any key override) and
  is a `422` anywhere else.
- `--json` and `-q` are accepted before or after the subcommand; leaf parsers use
  `argparse.SUPPRESS` as their default so they do not overwrite a value given earlier. The CLI
  sends logs to stderr so `--json` output stays machine-readable.
- Access logs are at DEBUG, so the check hot path does not pay for a log line per request, and no
  secret, token or submitted value reaches a log line or an error body.

## 14. Tests run against a real Redis

The Lua scripts are the product, so the suite runs against a real Redis on a dedicated database
index (`TEST_REDIS_URL`, default `redis://localhost:6379/15`) and flushes it between tests; mocking
Redis would test the mock. Tests use one session-scoped SQLite file migrated with
`alembic upgrade head`, so migrations are exercised for real. They raise `REDIS_TIMEOUT_MS` to 5000
because the 100 ms production default is a latency budget, not a correctness bound, and rebuild
the Redis pool per test because `redis.asyncio` connections bind to the event loop that created
them.
