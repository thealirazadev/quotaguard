# quotaguard - Product Requirements

## What we are building

quotaguard is an API rate limiting and quota decision service. An API gateway or application calls
`POST /v1/check` once per incoming request with an api key, a resource name, and an optional cost;
quotaguard answers allow or deny in a single Redis round trip and returns the standard draft
RateLimit header fields (limit, remaining, reset) ready to copy onto the caller's response. Every
key is enforced by two short-window layers configured on its plan - a token bucket for burst and a
sliding window log for sustained rate - plus a monthly quota with a hard limit and a soft warning
threshold that fires a webhook once per key per month. All admission decisions run as one atomic
Redis Lua script, so concurrent checks can never over-admit. Plans and keys are managed through a
small admin API and a CLI (create plan, issue key, override limits per key, revoke). A scheduled
job persists monthly usage from Redis into SQLite rollups idempotently, a usage report endpoint
serves per-key history, and each plan chooses its Redis-down policy: fail open with logging, or
fail closed.

## Target user

A developer or platform team running an API gateway (nginx/OpenResty, Envoy, Kong, or their own
middleware) who needs correct, low-latency rate limiting and monthly quotas without buying a
hosted metering product. They run one quotaguard instance next to one Redis, call it in-band from
the gateway, and keep control: limits live in plans, exceptions live in per-key overrides, usage
history lands in a file-backed SQLite database they can query directly. Secondary audience: a
reviewer of this repository evaluating how atomic rate limiting should be engineered - the Lua
scripts, the clock discipline, and the idempotent rollups are the point, not decoration.

quotaguard is a decision service, not a proxy: it never sees or forwards the protected traffic.

## Core features (prioritized)

1. **Atomic check endpoint** (highest priority). `POST /v1/check` with `api_key`, `resource`, and
   optional `cost` (default 1) returns allow/deny plus per-layer limit, remaining, and reset
   values. One `EVALSHA` covers the token bucket, the sliding window, and the monthly quota; a
   denied request consumes nothing from any layer.
2. **Two short-window enforcement layers per plan** (highest priority). A token bucket (capacity +
   refill per second) absorbs bursts; a sliding window log (limit per window) bounds sustained
   rate. Both are configured per plan, overridable per key, and scoped per (key, resource) so one
   hot resource cannot starve the others. Bucket state is integer microtokens; the window ZSET is
   trimmed on every check and its size is bounded by the sustained limit.
3. **Monthly quotas with hard and soft thresholds** (highest priority). A per-key counter keyed by
   the UTC month enforces a hard monthly cap; crossing the plan's soft threshold percentage fires
   exactly one webhook warning per key per month via a persistent outbox with retries.
4. **Standard draft RateLimit headers.** The response carries ready-to-emit `RateLimit`,
   `RateLimit-Policy`, and (on deny) `Retry-After` values per draft-ietf-httpapi-ratelimit-headers-07,
   plus the raw numbers for callers that render their own headers.
5. **Plan and key management.** Admin API and CLI: create/list/update plans, issue keys (secret
   shown once, stored hashed), list keys, override any limit per key, revoke. Admin routes require
   the admin token.
6. **Monthly usage rollups.** A scheduled job copies Redis month counters into SQLite rollup rows,
   idempotently (upsert by key + month, monotonic), guarded by an advisory lock so overlapping
   runs are safe, and restores Redis counters from rollups after a Redis data loss.
7. **Usage report endpoint.** `GET /v1/usage/{key_id}` returns the live current month from Redis
   plus prior months from rollups, with quota and threshold context.
8. **Redis-down policy per plan.** When Redis is unreachable, a plan either fails open (allow,
   logged, response marked degraded) or fails closed (deny with a distinct reason). The check
   endpoint itself keeps answering 200 either way.

## Non-goals

- Distributed or multi-region Redis, Redis Cluster, or any cross-instance state sync. One Redis,
  one quotaguard deployment.
- Billing integration: no invoices, no payment providers, no metering export formats. The soft
  threshold webhook is the only outbound integration.
- A dashboard UI. Reports are the CLI and JSON endpoints.
- Proxying or inspecting the protected traffic; quotaguard only answers check calls.
- Per-resource monthly quotas; the monthly quota is per key. Short-window limits are per
  (key, resource).
- Per-check persistence or analytics; only monthly aggregates are persisted.
- A rule DSL, dynamic limit scripting, or time-of-day schedules. Limits are plan columns.
- Auth for end consumers; the api key is an identifier for limiting, not an identity product.

## Success criteria per core feature

- **Atomic check endpoint** - A valid check returns 200 with `allowed`, per-layer numbers, and
  header values in one Redis round trip; an unknown or revoked key returns 200 with
  `allowed: false` and reason `unknown_key`/`revoked_key`; invalid input returns 422 in the
  single error format. Under N concurrent checks with M remaining budget, exactly min(N, M) are
  admitted (verified by a concurrency test against real Redis).
- **Short-window layers** - The bucket admits a burst of exactly `burst_capacity` requests from
  full, then refills at the configured rate with no float drift (integer microtokens, error
  bounded below one microtoken per refill, always in the conservative direction). The window
  admits at most `sustained_limit` requests in any window; its ZSET never holds more than
  `sustained_limit` members and expired members are trimmed on every check.
- **Monthly quotas** - The hard limit denies with reason `quota`; the soft threshold produces one
  webhook per key per month even under concurrent crossing checks and even if the process crashes
  between flagging and enqueueing (rollup reconciliation covers the gap).
- **Headers** - Emitted values match draft-07 syntax exactly; `RateLimit` reflects the denying
  layer on deny and the most constrained layer on allow; `Retry-After` equals the largest reset
  among denying layers.
- **Plan and key management** - Issuing a key shows the secret once and stores only its hash;
  revocation takes effect on checks within the key-cache TTL; overrides change effective limits
  for that key only; every admin mutation is available in both the API and the CLI.
- **Rollups** - Running the rollup twice in a row, or concurrently via CLI and scheduler, yields
  the same rows (advisory lock plus monotonic upsert); a flushed Redis regains its current-month
  counters at the next rollup run.
- **Usage report** - The current month matches Redis, prior months match rollups, and both carry
  quota context; unknown key returns 404.
- **Redis-down policy** - With Redis stopped: fail-open plans get `allowed: true, degraded: true`
  and a WARNING log; fail-closed plans get `allowed: false` with reason `redis_unavailable`; the
  endpoint answers within the Redis timeout budget and recovers without restart when Redis
  returns.
