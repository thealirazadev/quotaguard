# quotaguard - Launch Checklist

Work top to bottom before pointing production traffic at an instance. Nothing is checked until
verified in the target environment.

## Environment and configuration

- [ ] Production `.env` created from `.env.example` with real values; `QG_ADMIN_TOKEN` and (if
      the check endpoint is reachable beyond the gateway network) `QG_SERVICE_TOKEN` set to
      long random values.
- [ ] `REDIS_URL` points at the production Redis 7.x; `DATABASE_URL` at a persistent path that
      is included in backups.
- [ ] `REDIS_TIMEOUT_MS`, `KEY_CACHE_TTL_SECONDS`, `ROLLUP_INTERVAL_SECONDS`, webhook settings
      reviewed for this deployment.
- [ ] `QG_WEBHOOK_SECRET` set and shared with the webhook receiver; signature verification
      confirmed end to end with one real soft-threshold event.

## Redis

- [ ] Redis 7.x confirmed; `maxmemory-policy noeviction` set (eviction silently resets limits).
- [ ] AOF persistence enabled (`appendonly yes`, everysec) so limiter state survives restarts.
- [ ] Redis reachable only from quotaguard (network policy or auth); latency from quotaguard to
      Redis measured at sub-millisecond.

## Processes

- [ ] One API process (`uvicorn --workers 1`) under a supervisor, restarting on failure; the
      single-process assumption from `docs/architecture.md` acknowledged, or multi-worker
      staleness consciously accepted.
- [ ] Scheduler loops confirmed running (startup log lines); `rollup.completed` observed at the
      configured interval.
- [ ] Deploy procedure restarts the process and re-runs `alembic upgrade head`.

## Security

- [ ] No secrets committed; `.env` git-ignored; only `.env.example` (dummies) tracked.
- [ ] HTTPS termination in front of the API; admin routes unreachable from the public internet
      or protected by network policy in addition to the token.
- [ ] A database dump contains no key secrets (hashes and prefixes only) - inspected once.
- [ ] Wrong admin token returns 401 without timing leaks; tokens absent from all logs.

## Reliability drills (run each once, in the target environment)

- [ ] Redis-down drill: stop Redis; fail-open and fail-closed plans behave per contract, no
      500s, `check.redis_down` logged; start Redis; recovery without process restart.
- [ ] Restore drill: `FLUSHDB`, run `rollup run`, confirm the current-month counter returns to
      the persisted value and live checks lose no increments.
- [ ] Concurrency spot check: parallel checks against a small remaining budget admit exactly
      the budget (repeat the phase 2 manual test against production Redis).
- [ ] Webhook retry drill: dead receiver -> backoff retries -> `webhook.exhausted`; live
      receiver -> exactly one delivery per key+month.
- [ ] Kill the process mid-traffic; restart; plans, keys, rollups, outbox intact; checks resume.

## Quality gates

- [ ] CI green on the deployed commit (lint, format, full pytest with the Redis container).
- [ ] `uv.lock` committed and matching the deployed build; dependency pins exact.
- [ ] Latency measured in production: check p50 within a few milliseconds, exactly one Redis
      round trip per check (`redis-cli monitor` sample).
- [ ] Log aggregation receiving JSON lines; alerting on `webhook.exhausted`, `check.redis_down`,
      and prolonged `rollup.skipped_locked`.
