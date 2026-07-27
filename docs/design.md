# quotaguard - Design

quotaguard has no graphical UI (a dashboard is an explicit non-goal), so this document covers the
surfaces humans actually look at: the CLI, the JSON API conventions, and the log lines. Visual
design sections (color themes, typography, spacing) do not apply and are replaced by the
terminal-UX equivalents below.

## CLI UX (`python -m app.cli`, installed as `quotaguard`)

### Command tree

```
quotaguard plan create --slug pro --name "Pro" --burst-capacity 100 --burst-refill 50
                       --sustained-limit 5000 --sustained-window 3600
                       --monthly-quota 500000 --soft-pct 80
                       --webhook-url https://ops.example.com/hooks/quota
                       [--redis-down-policy fail_open|fail_closed]
quotaguard plan list
quotaguard key issue --plan pro --name "acme production"
quotaguard key list [--plan pro]
quotaguard key override <key_id> [--burst-capacity N] [--sustained-limit N] ...
                        [--clear burst_capacity,sustained_limit,...]
quotaguard key revoke <key_id>
quotaguard usage <key_id> [--months 3]
quotaguard rollup run
```

Global flags on every command: `--json` (machine output, exactly the API response shape) and
`-q` (suppress informational lines, keep results and errors). The CLI imports the service layer
directly (same `.env`, same database and Redis) - the server does not need to be running.

### Output layout

Plain text tables to stdout: aligned fixed-width columns, lowercase headers, no table-drawing
characters, thousands separators on counts, timestamps ISO-8601 UTC. Empty results print one
explanatory line (`no keys yet - issue one with: quotaguard key issue`), never a bare header.

```
$ quotaguard plan list
slug   burst        sustained     monthly    policy       keys
free   10 @ 5/s     100 / 1h      10,000     fail_open    12
pro    100 @ 50/s   5,000 / 1h    500,000    fail_open    3
```

`key issue` prints the secret exactly once, on its own labeled line, and warns it will not be
shown again:

```
$ quotaguard key issue --plan pro --name "acme production"
issued key k_1a2b3c4d5e6f (acme production) on plan pro
api key (shown once, store it now): qk_vX2mR8pQ4tY7...
```

`usage` renders one row per month, newest first, marking the live month:

```
$ quotaguard usage k_1a2b3c4d5e6f --months 3
month     used       quota      remaining   source
2026-07   12,401     500,000    487,599     live
2026-06   431,009    500,000    68,991      rollup
2026-05   287,340    500,000    212,660     rollup
```

### Color, errors, exit codes

- ANSI color only when stdout is a TTY and `NO_COLOR` is unset: red for `error:`, yellow for
  warnings (e.g. soft threshold crossed in `usage`). Color is never the only signal; piped
  output is byte-identical minus escape codes.
- Errors go to stderr as `error: <one clear sentence>` plus one remedy line where useful
  (`set QG_ADMIN_TOKEN in .env`). Never a traceback for expected failures.
- Exit codes: `0` success, `1` operation failed (unknown key, duplicate slug, rollup lock held
  and nothing done, validation rejection), `2` configuration or environment error (missing env,
  unreachable database). `rollup run` reports `skipped: lock held` and exits 0 - a concurrent
  rollup is normal operation, not a failure.

## API response design

- JSON only; snake_case; ISO-8601 UTC timestamps; success bodies wrapped in `{"data": ...}`,
  lists in `{"<items>": [...], "total"}` under `data`; nulls explicit, never omitted keys.
- One error shape everywhere: `{"error": {"code", "message"}}` - `message` a complete friendly
  sentence, `code` the machine contract.
- The check response is decision-complete: `allowed`, `reason`, `degraded`, the chosen header
  values, and per-layer numbers, so a gateway never needs a second call or log access to explain
  a deny. Reasons are explicit strings, not HTTP status distinctions - the endpoint returns 200
  for every well-formed check, including denials, unknown keys, and degraded answers.
- Header values are returned as strings under `headers` exactly as they should be emitted,
  plus raw integers for callers that render their own.

## Log line design

One JSON object per line: `timestamp`, `level`, `logger`, `message`, then context fields
(`request_id`, `route`, `status_code`, `duration_ms`, `key_id`, `resource`, `deny_layer`,
`policy`, `month`, `error_code`). Messages are short and grep-stable (`check denied`,
`rollup completed`); variability lives in the fields. Allowed checks log at DEBUG only, so an
INFO-level production log stays proportional to problems, not traffic. No key secrets, no
tokens, no multi-line entries.

## Accessibility baseline

With no GUI, accessibility obligations land on the terminal and API surfaces: no color-only
meaning and `NO_COLOR` respect; tables linear and labeled (no ASCII art) so screen readers read
them sanely; API and OpenAPI descriptions in plain language. If a web dashboard is ever
proposed, it starts from semantic HTML, labeled inputs, keyboard operability, visible focus, and
WCAG AA contrast - and from a new phase, not this build.
