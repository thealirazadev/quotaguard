# Changelog

## Unreleased

### Added
- Per-plan Redis-down policy (fail open or fail closed), and Redis status in `/health`.
- Usage report endpoint, plus `usage` and `rollup` CLI commands.
- Latency smoke test for the check path.

### Fixed
- Usage no longer duplicates the current month or soft-threshold flags from the outbox.
- Webhook delivery and rollups run off the event loop.
- Rollup restore and unlock scripts run on the sync Redis client.
- `webhook.exhausted` is logged once instead of on every delivery pass.
- Upgraded FastAPI, Starlette, Black, and certifi to resolve security advisories.

## 0.1.0

Plans and API keys, an atomic Redis check endpoint with burst, sustained, and monthly quota
layers, signed webhooks, idempotent rollups to SQLite, and an admin API and CLI.
