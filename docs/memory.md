# quotaguard - Project Memory

Running log of what is done, in progress, and decided. Update after every meaningful chunk of
work; log every non-obvious decision with its reason. Keep entries short and dated.

## Completed

- 2026-07-27 - Planning documentation created (README, PRD, architecture, rules, phases, design,
  testing, api-contracts, launch-checklist, memory). No code yet; implementation follows
  `docs/phases.md` starting with Phase 1 after owner review.

## Project status

- Planning stage. Docs are the source of truth; `docs/architecture.md` fully specifies the
  check script (KEYS, ARGV, algorithm, return tuple) so a coding agent can implement Phase 2
  without further design work.

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
- 2026-07-27 - Rollups upsert with `used = max(existing, read)` and Redis counters are restored
  after data loss via a delta-based `INCRBY` inside `restore.lua` (never SET), so overlapping
  rollups, replayed runs, and concurrent live traffic can never lower a persisted value or lose
  increments. `redis_down_policy` defaults to `fail_open` because a limiter outage should not
  take down the customer's API; plans guarding paid quotas opt into `fail_closed`.
