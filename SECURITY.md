# Security policy

## Supported versions

Only the latest release (0.1.0, the version in `pyproject.toml`) on `main` receives security
fixes.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting
(Security tab, "Report a vulnerability") or email thealirazadev@gmail.com with the
steps to reproduce. You should get a reply within 72 hours.

## How quotaguard protects keys and decisions

- API key secrets are shown once at issue time and stored only as a sha256 hash with a short
  display prefix, so a lost secret cannot be read back.
- Admin and service tokens are compared in constant time, and every admin route requires
  `X-Admin-Token`. The check route can also require `X-Service-Token`.
- Unknown and revoked keys are answered without touching Redis.
- Resource names are restricted to a fixed character set and length, and `cost` is bounded to
  1-1000. Malformed input returns `422` in the standard error envelope.
- Webhook bodies can be signed with HMAC-SHA256 (`sha256=` signature) when a signing secret is
  configured.
- Each plan chooses its Redis-down policy: `fail_open` (logged and marked degraded) or
  `fail_closed`.
