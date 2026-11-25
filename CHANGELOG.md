# Changelog

## Unreleased

### Added
- Per-plan Redis-down policy (fail open or fail closed), and Redis status in `/health`.
- Usage report endpoint, plus `usage` and `rollup` CLI commands.
- Latency smoke test for the check path.

### Fixed
- A `cost` above the key's burst capacity or monthly quota is refused with
  `reason: "cost_exceeds_limit"` and no `Retry-After`, instead of a retry hint that never succeeds.
- The check path's Redis client is pinned to zero retries, so a timeout can never charge a check
  twice; the timeout behaviour per Redis-down policy is documented.
- Webhook delivery skips outbox rows whose plan no longer has a `webhook_url` and sends due rows
  oldest first, so undeliverable rows can no longer fill every batch; `webhook.exhausted` is
  logged at ERROR as documented.
- Usage no longer duplicates the current month or soft-threshold flags from the outbox.
- Webhook delivery and rollups run off the event loop.
- Rollup restore and unlock scripts run on the sync Redis client.
- `webhook.exhausted` is logged once instead of on every delivery pass.
- Upgraded FastAPI, Starlette, Black, and certifi to resolve security advisories.

## 0.1.0

Plans and API keys, an atomic Redis check endpoint with burst, sustained, and monthly quota
layers, signed webhooks, idempotent rollups to SQLite, and an admin API and CLI.
