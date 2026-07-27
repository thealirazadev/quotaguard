# quotaguard - API Contracts

Three surfaces: the **check API** (called by gateways, hot path), the **admin API** (plans,
keys, usage; operator-only), and the **CLI** (same operations in-process). This file also fixes
the **outbound webhook** contract. All agreed here before any code is written.

Timestamps are ISO-8601 UTC. Success bodies are wrapped in `{"data": ...}`.

## Error envelope (all JSON errors)

```json
{
  "error": {
    "code": "validation_error",
    "message": "cost must be between 1 and 1000."
  }
}
```

### Stable error codes

| HTTP | `error.code`       | When |
|------|--------------------|------|
| 422  | `validation_error` | Malformed body, out-of-bounds numbers, bad resource/slug charset. |
| 401  | `unauthorized`     | Missing or wrong `X-Admin-Token` / `X-Service-Token`. |
| 404  | `not_found`        | Unknown plan slug or key id on admin/usage routes. |
| 409  | `conflict`         | Duplicate plan slug, revoking an already revoked key. |
| 500  | `internal_error`   | Unexpected error (details logged, never returned). |

Deliberately absent from this table: unknown/revoked api keys and Redis outages on the check
endpoint. Those are answered in-band with `allowed: false` and a `reason`, because the gateway
needs a decision on every call, not an error branch.

### Auth summary

| Route | Auth |
|-------|------|
| `GET /health` | none |
| `POST /v1/check` | `X-Service-Token` only when `QG_SERVICE_TOKEN` is set |
| `GET /v1/usage/{key_id}` | `X-Admin-Token` |
| `/admin/*` | `X-Admin-Token` |

Both tokens are compared constant-time and never logged.

---

## Check API

### POST /v1/check

Called once per protected request. `api_key` is the consumer's full secret; `resource` scopes
the short-window layers (the monthly quota is per key); `cost` (default 1, max 1000) is charged
against the bucket and the quota, while the sliding window always counts one request.

Request:

```json
{
  "api_key": "qk_vX2mR8pQ4tY7cW1zK9nB5jH3fD6sG0aLxE8uM2oP7iQ",
  "resource": "search",
  "cost": 1
}
```

`resource`: 1-128 chars, `[a-zA-Z0-9_.:/-]`. Unknown fields rejected.

Response `200` (allowed):

```json
{
  "data": {
    "allowed": true,
    "reason": null,
    "degraded": false,
    "limit": 100,
    "remaining": 97,
    "reset": 2,
    "retry_after": null,
    "headers": {
      "RateLimit": "limit=100, remaining=97, reset=2",
      "RateLimit-Policy": "100;w=2, 5000;w=3600, 500000;w=2678400"
    },
    "layers": {
      "burst": { "limit": 100, "remaining": 97, "reset": 2 },
      "sustained": { "limit": 5000, "remaining": 4993, "reset": 3599 },
      "quota": {
        "month": "2026-07",
        "limit": 500000,
        "used": 12401,
        "remaining": 487599,
        "reset": 397440,
        "soft_threshold_crossed": false
      }
    }
  }
}
```

Response `200` (denied - here by the sustained layer):

```json
{
  "data": {
    "allowed": false,
    "reason": "sustained",
    "degraded": false,
    "limit": 5000,
    "remaining": 0,
    "reset": 41,
    "retry_after": 41,
    "headers": {
      "RateLimit": "limit=5000, remaining=0, reset=41",
      "RateLimit-Policy": "100;w=2, 5000;w=3600, 500000;w=2678400",
      "Retry-After": "41"
    },
    "layers": { "burst": {}, "sustained": {}, "quota": {} }
  }
}
```

(`layers` is always fully populated as in the allow example; elided above for brevity.)

Response `200` (unknown or revoked key - no Redis touched, no `layers`):

```json
{
  "data": {
    "allowed": false,
    "reason": "unknown_key",
    "degraded": false,
    "limit": null, "remaining": null, "reset": null, "retry_after": null,
    "headers": {},
    "layers": null
  }
}
```

Response `200` (Redis down, plan policy `fail_open`; `fail_closed` is identical except
`allowed: false` and `reason: "redis_unavailable"`):

```json
{
  "data": {
    "allowed": true,
    "reason": null,
    "degraded": true,
    "limit": null, "remaining": null, "reset": null, "retry_after": null,
    "headers": {},
    "layers": null
  }
}
```

`reason` is one of: `null`, `"burst"`, `"sustained"`, `"quota"`, `"unknown_key"`,
`"revoked_key"`, `"redis_unavailable"`.

**Header contract** (draft-ietf-httpapi-ratelimit-headers-07): `RateLimit` carries the denying
layer on deny, else the most constrained layer (smallest remaining/limit ratio, tie to the
shorter window); `RateLimit-Policy` lists burst, sustained, monthly as `<quota>;w=<seconds>`;
`Retry-After` (deny only) is the largest reset among denying layers. The gateway copies these
strings onto its own response unchanged.

### GET /v1/usage/{key_id}?months=N

Admin token. `months` 1-24, default 3. Current month is read live from Redis; prior months from
rollups; months with no traffic are omitted.

```json
{
  "data": {
    "key_id": "k_1a2b3c4d5e6f",
    "name": "acme production",
    "plan": "pro",
    "months": [
      { "month": "2026-07", "used": 12401, "quota": 500000, "remaining": 487599,
        "soft_threshold_crossed": false, "source": "live" },
      { "month": "2026-06", "used": 431009, "quota": 500000, "remaining": 68991,
        "soft_threshold_crossed": true, "source": "rollup" }
    ]
  }
}
```

`404 not_found` for an unknown key id. Revoked keys still report their history.

### GET /health

```json
{ "status": "ok", "redis": "ok" }
```

Always HTTP 200; `redis` becomes `"down"` during an outage (the health route must not flap the
process out of a load balancer because Redis is down - the check path has its own policy).

---

## Admin API

All routes require `X-Admin-Token`. Plan numeric bounds are in `docs/architecture.md` and are
validated on every write.

### POST /admin/plans

```json
{
  "slug": "pro",
  "name": "Pro",
  "burst_capacity": 100,
  "burst_refill_per_sec": 50,
  "sustained_limit": 5000,
  "sustained_window_seconds": 3600,
  "monthly_quota": 500000,
  "quota_soft_pct": 80,
  "webhook_url": "https://ops.example.com/hooks/quota",
  "redis_down_policy": "fail_open"
}
```

`201` with the created plan under `data` (all fields plus timestamps). `409 conflict` on a
duplicate slug. `webhook_url` is required when `quota_soft_pct > 0`.

### GET /admin/plans

`200` with `{"data": {"plans": [...], "total": 2}}`, ordered by slug.

### GET /admin/plans/{slug}

`200` with the plan plus `keys_count`. `404` when unknown.

### PATCH /admin/plans/{slug}

Any subset of the mutable fields (everything except `slug`). Changed limits take effect for
checks within the key-cache TTL. `200` with the updated plan.

### POST /admin/keys

```json
{ "plan": "pro", "name": "acme production" }
```

`201` - the only response that ever contains the secret:

```json
{
  "data": {
    "key_id": "k_1a2b3c4d5e6f",
    "name": "acme production",
    "plan": "pro",
    "api_key": "qk_vX2mR8pQ4tY7cW1zK9nB5jH3fD6sG0aLxE8uM2oP7iQ",
    "key_prefix": "qk_vX2mR8pQ4",
    "created_at": "2026-07-27T09:14:03Z"
  }
}
```

### GET /admin/keys?plan={slug}

`200` with `{"data": {"keys": [...], "total": 12}}` - each entry: `key_id`, `key_prefix`,
`name`, `plan`, `overrides` (object of non-null overrides), `revoked_at`, `created_at`. Never
the secret or hash.

### GET /admin/keys/{key_id}

`200` with the same shape plus effective limits (`plan value or override, resolved`).

### PATCH /admin/keys/{key_id}

Set or clear overrides and rename. Explicit `null` clears an override:

```json
{ "override_sustained_limit": 10000, "override_monthly_quota": null }
```

`200` with the updated key including resolved effective limits. `409` when the key is revoked.

### POST /admin/keys/{key_id}/revoke

`200` with `{"data": {"key_id": "k_1a2b3c4d5e6f", "revoked_at": "2026-07-27T10:02:11Z"}}`.
Checks with this key flip to `reason: "revoked_key"` immediately in-process, within
`KEY_CACHE_TTL_SECONDS` otherwise. `409` when already revoked. Revocation is permanent; issue a
new key instead of un-revoking.

---

## Outbound webhook (quotaguard -> operator endpoint)

Fired at most once per key per month when the soft threshold is crossed:

```
POST <plan.webhook_url>
Content-Type: application/json
User-Agent: quotaguard/1.0
X-QuotaGuard-Event: quota.soft_threshold
X-QuotaGuard-Signature: sha256=<hex hmac_sha256(QG_WEBHOOK_SECRET, body)>   (only when secret set)

{
  "event": "quota.soft_threshold",
  "key_id": "k_1a2b3c4d5e6f",
  "key_name": "acme production",
  "plan": "pro",
  "month": "2026-07",
  "used": 400012,
  "quota": 500000,
  "threshold_pct": 80,
  "sent_at": "2026-07-27T09:14:03Z"
}
```

Any 2xx within `WEBHOOK_TIMEOUT_SECONDS` marks it delivered. Anything else is retried on the
backoff schedule (`min(30 * 2^(n-1), 3600)` seconds, jitter 0.8-1.2) up to
`WEBHOOK_MAX_ATTEMPTS`, then parked with `webhook.exhausted` logged. Receivers should verify the
signature (constant-time) when a secret is configured and treat duplicate `(key_id, month)`
events as no-ops - delivery is at-least-once in crash corners, uniqueness is per enqueue.

---

## CLI

Every command supports `--json` (exact API response shape) and `-q`. Errors go to stderr as
`error: <sentence>`; exit codes per `docs/design.md` (0 success, 1 operation failed, 2
config/environment error).

```
$ quotaguard plan create --slug pro --name "Pro" --burst-capacity 100 --burst-refill 50 \
    --sustained-limit 5000 --sustained-window 3600 --monthly-quota 500000 --soft-pct 80 \
    --webhook-url https://ops.example.com/hooks/quota
created plan pro (Pro)

$ quotaguard plan list
slug   burst        sustained     monthly    policy       keys
free   10 @ 5/s     100 / 1h      10,000     fail_open    12
pro    100 @ 50/s   5,000 / 1h    500,000    fail_open    3

$ quotaguard key issue --plan pro --name "acme production"
issued key k_1a2b3c4d5e6f (acme production) on plan pro
api key (shown once, store it now): qk_vX2mR8pQ4tY7cW1zK9nB5jH3fD6sG0aLxE8uM2oP7iQ

$ quotaguard key override k_1a2b3c4d5e6f --sustained-limit 10000
key k_1a2b3c4d5e6f: sustained_limit 5000 -> 10000 (override)

$ quotaguard key override k_1a2b3c4d5e6f --clear sustained_limit
key k_1a2b3c4d5e6f: sustained_limit override cleared (plan value 5000)

$ quotaguard key revoke k_1a2b3c4d5e6f
revoked key k_1a2b3c4d5e6f at 2026-07-27T10:02:11Z

$ quotaguard usage k_1a2b3c4d5e6f --months 2
month     used       quota      remaining   source
2026-07   12,401     500,000    487,599     live
2026-06   431,009    500,000    68,991      rollup

$ quotaguard rollup run
rollup complete: 41 counters scanned, 41 upserted, 1 restored, 0 webhooks reconciled
```

`rollup run` while the scheduler holds the lock prints `skipped: another rollup holds the lock`
and exits 0.

---

## Gateway integration sketch

Per incoming request: extract the consumer's api key, `POST /v1/check` with the resource name,
then either forward the request (copying `data.headers` onto the eventual response) or reject it
with HTTP 429 (`reason` burst/sustained/quota) or 401/403 (`unknown_key`/`revoked_key`), again
copying the headers on 429. Treat `degraded: true` per your own risk posture; quotaguard has
already applied the plan's policy.
