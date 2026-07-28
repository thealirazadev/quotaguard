"""Operator CLI. Imports the service layer directly, so no server needs to run.

This module owns its stdout: it is the only place in the codebase that prints.
"""

import argparse
import json
import os
import sys

import pydantic

from app.errors import AppError
from app.logging import configure_logging
from app.models import OVERRIDE_TO_PLAN_FIELD

OVERRIDABLE = sorted(OVERRIDE_TO_PLAN_FIELD.values())


def _color(text: str, code: str, stream) -> str:
    if os.environ.get("NO_COLOR") is not None or not stream.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _fail(message: str, exit_code: int = 1) -> int:
    print(_color("error:", "31", sys.stderr) + f" {message}", file=sys.stderr)
    return exit_code


def _say(message: str, args: argparse.Namespace) -> None:
    if not args.quiet:
        print(message)


def _dump(payload) -> None:
    print(json.dumps({"data": payload}, indent=2, default=str))


def _key_payload(key) -> dict:
    """The same shape the admin API returns for a key, built without the HTTP layer."""
    from app.models import to_iso_utc

    return {
        "key_id": key.key_id,
        "key_prefix": key.key_prefix,
        "name": key.name,
        "plan": key.plan.slug,
        "overrides": key.overrides(),
        "revoked_at": to_iso_utc(key.revoked_at) if key.revoked_at else None,
        "created_at": to_iso_utc(key.created_at),
    }


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [
        max(len(header), *(len(row[index]) for row in rows)) if rows else len(header)
        for index, header in enumerate(headers)
    ]
    lines = ["  ".join(value.ljust(widths[index]) for index, value in enumerate(headers)).rstrip()]
    for row in rows:
        lines.append(
            "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


def _window(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _plan_create(session, args: argparse.Namespace) -> int:
    from app.schemas.plans import PlanCreateRequest
    from app.services import plans as plans_service

    payload = PlanCreateRequest.model_validate(
        {
            "slug": args.slug,
            "name": args.name,
            "burst_capacity": args.burst_capacity,
            "burst_refill_per_sec": args.burst_refill,
            "sustained_limit": args.sustained_limit,
            "sustained_window_seconds": args.sustained_window,
            "monthly_quota": args.monthly_quota,
            "quota_soft_pct": args.soft_pct,
            "webhook_url": args.webhook_url,
            "redis_down_policy": args.redis_down_policy,
        }
    )
    plan = plans_service.create_plan(session, payload)
    if args.json:
        from app.schemas.plans import PlanOut

        _dump(PlanOut.model_validate(plan).model_dump())
    else:
        _say(f"created plan {plan.slug} ({plan.name})", args)
    return 0


def _plan_list(session, args: argparse.Namespace) -> int:
    from app.schemas.plans import PlanListOut, PlanOut
    from app.services import plans as plans_service

    plans = plans_service.list_plans(session)
    if args.json:
        body = PlanListOut(plans=[PlanOut.model_validate(p) for p in plans], total=len(plans))
        _dump(body.model_dump())
        return 0
    if not plans:
        _say("no plans yet - create one with: quotaguard plan create", args)
        return 0
    rows = [
        [
            plan.slug,
            f"{plan.burst_capacity} @ {plan.burst_refill_per_sec}/s",
            f"{plan.sustained_limit:,} / {_window(plan.sustained_window_seconds)}",
            f"{plan.monthly_quota:,}",
            plan.redis_down_policy,
            str(plans_service.count_keys(session, plan)),
        ]
        for plan in plans
    ]
    print(_table(["slug", "burst", "sustained", "monthly", "policy", "keys"], rows))
    return 0


def _key_issue(session, args: argparse.Namespace) -> int:
    from app.schemas.keys import KeyIssuedOut
    from app.services import keys as keys_service

    key, secret = keys_service.issue_key(session, args.plan, args.name)
    issued = KeyIssuedOut(
        key_id=key.key_id,
        name=key.name,
        plan=key.plan.slug,
        api_key=secret,
        key_prefix=key.key_prefix,
        created_at=key.created_at,
    )
    if args.json:
        _dump(issued.model_dump())
        return 0
    _say(f"issued key {key.key_id} ({key.name}) on plan {key.plan.slug}", args)
    print(f"api key (shown once, store it now): {secret}")
    return 0


def _key_list(session, args: argparse.Namespace) -> int:
    from app.services import keys as keys_service

    keys = keys_service.list_keys(session, args.plan)
    if args.json:
        _dump({"keys": [_key_payload(key) for key in keys], "total": len(keys)})
        return 0
    if not keys:
        _say("no keys yet - issue one with: quotaguard key issue", args)
        return 0
    rows = [
        [
            key.key_id,
            key.key_prefix,
            key.name,
            key.plan.slug,
            ",".join(sorted(OVERRIDE_TO_PLAN_FIELD[field] for field in key.overrides())) or "-",
            "revoked" if key.revoked_at else "active",
        ]
        for key in keys
    ]
    print(_table(["key_id", "prefix", "name", "plan", "overrides", "status"], rows))
    return 0


def _key_override(session, args: argparse.Namespace) -> int:
    from app.schemas.keys import KeyUpdateRequest
    from app.services import keys as keys_service

    changes: dict[str, int | None] = {}
    for field in OVERRIDABLE:
        value = getattr(args, field)
        if value is not None:
            changes[f"override_{field}"] = value
    for field in [name.strip() for name in (args.clear or "").split(",") if name.strip()]:
        if field not in OVERRIDABLE:
            return _fail(f"'{field}' is not overridable. Choose from: {', '.join(OVERRIDABLE)}.")
        changes[f"override_{field}"] = None

    key = keys_service.get_key(session, args.key_id)
    before = key.effective_limits()
    key = keys_service.update_key(session, args.key_id, KeyUpdateRequest.model_validate(changes))
    after = key.effective_limits()
    if args.json:
        _dump({**_key_payload(key), "effective_limits": after})
        return 0
    for override_field, value in changes.items():
        field = OVERRIDE_TO_PLAN_FIELD[override_field]
        if value is None:
            _say(f"key {key.key_id}: {field} override cleared (plan value {after[field]})", args)
        else:
            _say(f"key {key.key_id}: {field} {before[field]} -> {after[field]} (override)", args)
    return 0


def _key_revoke(session, args: argparse.Namespace) -> int:
    from app.models import to_iso_utc
    from app.services import keys as keys_service

    key = keys_service.revoke_key(session, args.key_id)
    revoked_at = to_iso_utc(key.revoked_at)
    if args.json:
        _dump({"key_id": key.key_id, "revoked_at": revoked_at})
    else:
        _say(f"revoked key {key.key_id} at {revoked_at}", args)
    return 0


def build_parser() -> argparse.ArgumentParser:
    # Accepted before or after the subcommand; SUPPRESS keeps the leaf copies
    # from overwriting a value already given at the top level.
    flags = argparse.ArgumentParser(add_help=False)
    flags.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print the API response shape",
    )
    flags.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="suppress informational lines",
    )

    parser = argparse.ArgumentParser(prog="quotaguard", description="quotaguard operator CLI.")
    parser.add_argument("--json", action="store_true", help="print the API response shape")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress informational lines")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="manage plans").add_subparsers(
        dest="subcommand", required=True
    )
    create = plan.add_parser("create", help="create a plan", parents=[flags])
    create.add_argument("--slug", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--burst-capacity", type=int, required=True)
    create.add_argument("--burst-refill", type=int, required=True)
    create.add_argument("--sustained-limit", type=int, required=True)
    create.add_argument("--sustained-window", type=int, required=True)
    create.add_argument("--monthly-quota", type=int, required=True)
    create.add_argument("--soft-pct", type=int, default=0)
    create.add_argument("--webhook-url")
    create.add_argument("--redis-down-policy", default="fail_open")
    create.set_defaults(handler=_plan_create)
    plan.add_parser("list", help="list plans", parents=[flags]).set_defaults(handler=_plan_list)

    key = commands.add_parser("key", help="manage api keys").add_subparsers(
        dest="subcommand", required=True
    )
    issue = key.add_parser("issue", help="issue a key", parents=[flags])
    issue.add_argument("--plan", required=True)
    issue.add_argument("--name", required=True)
    issue.set_defaults(handler=_key_issue)
    listing = key.add_parser("list", help="list keys", parents=[flags])
    listing.add_argument("--plan")
    listing.set_defaults(handler=_key_list)
    override = key.add_parser(
        "override", help="set or clear per-key limit overrides", parents=[flags]
    )
    override.add_argument("key_id")
    for field in OVERRIDABLE:
        override.add_argument(f"--{field.replace('_', '-')}", dest=field, type=int)
    override.add_argument("--clear", help=f"comma separated: {', '.join(OVERRIDABLE)}")
    override.set_defaults(handler=_key_override)
    revoke = key.add_parser("revoke", help="revoke a key permanently", parents=[flags])
    revoke.add_argument("key_id")
    revoke.set_defaults(handler=_key_revoke)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        from app.config import get_settings
        from app.db import SessionLocal

        configure_logging(get_settings().log_level, sys.stderr)
    except Exception as exc:  # missing or invalid configuration
        return _fail(f"configuration problem: {exc.__class__.__name__}. Check your .env file.", 2)

    session = SessionLocal()
    try:
        return args.handler(session, args)
    except AppError as exc:
        return _fail(exc.message)
    except pydantic.ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first.get("loc", ()))
        return _fail(f"{field}: {first.get('msg', 'is invalid')}.")
    except Exception as exc:
        session.rollback()
        return _fail(f"unexpected failure: {exc.__class__.__name__}.", 2)
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
