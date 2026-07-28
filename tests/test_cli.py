"""CLI commands, table output, and exit codes."""

import json

from app.cli import main

PLAN_ARGS = [
    "plan",
    "create",
    "--slug",
    "pro",
    "--name",
    "Pro",
    "--burst-capacity",
    "100",
    "--burst-refill",
    "50",
    "--sustained-limit",
    "5000",
    "--sustained-window",
    "3600",
    "--monthly-quota",
    "500000",
    "--soft-pct",
    "80",
    "--webhook-url",
    "https://ops.example.com/hooks/quota",
]


def _issue_key(capsys) -> str:
    capsys.readouterr()
    main(["key", "issue", "--plan", "pro", "--name", "acme production", "--json"])
    return json.loads(capsys.readouterr().out)["data"]["key_id"]


def test_empty_states_explain_themselves(capsys):
    assert main(["plan", "list"]) == 0
    assert "no plans yet" in capsys.readouterr().out

    assert main(["key", "list"]) == 0
    assert "no keys yet" in capsys.readouterr().out


def test_plan_create_and_list(capsys):
    assert main(PLAN_ARGS) == 0
    assert "created plan pro (Pro)" in capsys.readouterr().out

    assert main(["plan", "list"]) == 0
    output = capsys.readouterr().out
    assert output.splitlines()[0].split() == [
        "slug",
        "burst",
        "sustained",
        "monthly",
        "policy",
        "keys",
    ]
    assert "100 @ 50/s" in output
    assert "5,000 / 1h" in output
    assert "500,000" in output


def test_duplicate_slug_fails_with_exit_code_one(capsys):
    main(PLAN_ARGS)
    capsys.readouterr()

    assert main(PLAN_ARGS) == 1
    assert "error: A plan with slug 'pro' already exists." in capsys.readouterr().err


def test_invalid_slug_fails_before_touching_the_database(capsys):
    args = [*PLAN_ARGS]
    args[args.index("pro")] = "NOT A SLUG"

    assert main(args) == 1
    assert capsys.readouterr().err.startswith("error: slug:")


def test_key_issue_prints_the_secret_once(capsys):
    main(PLAN_ARGS)
    capsys.readouterr()

    assert main(["key", "issue", "--plan", "pro", "--name", "acme production"]) == 0
    output = capsys.readouterr().out
    secret_lines = [line for line in output.splitlines() if "qk_" in line]
    assert len(secret_lines) == 1
    assert secret_lines[0].startswith("api key (shown once, store it now): qk_")


def test_key_list_shows_the_prefix_and_never_the_secret(capsys):
    main(PLAN_ARGS)
    main(["key", "issue", "--plan", "pro", "--name", "acme production"])
    secret = [line for line in capsys.readouterr().out.splitlines() if "qk_" in line][0].split()[-1]

    assert main(["key", "list"]) == 0
    output = capsys.readouterr().out
    assert secret not in output
    assert secret[:12] in output


def test_override_set_and_clear_report_effective_values(capsys):
    main(PLAN_ARGS)
    key_id = _issue_key(capsys)

    assert main(["key", "override", key_id, "--sustained-limit", "10000"]) == 0
    assert f"key {key_id}: sustained_limit 5000 -> 10000 (override)" in capsys.readouterr().out

    assert main(["key", "override", key_id, "--clear", "sustained_limit"]) == 0
    assert f"key {key_id}: sustained_limit override cleared (plan value 5000)" in (
        capsys.readouterr().out
    )


def test_override_rejects_an_unknown_field(capsys):
    main(PLAN_ARGS)
    key_id = _issue_key(capsys)

    assert main(["key", "override", key_id, "--clear", "bogus"]) == 1
    assert "is not overridable" in capsys.readouterr().err


def test_revoke_is_not_repeatable(capsys):
    main(PLAN_ARGS)
    key_id = _issue_key(capsys)

    assert main(["key", "revoke", key_id]) == 0
    assert f"revoked key {key_id} at" in capsys.readouterr().out

    assert main(["key", "revoke", key_id]) == 1
    assert "already revoked" in capsys.readouterr().err


def test_unknown_key_exits_one(capsys):
    assert main(["key", "revoke", "k_missing"]) == 1
    assert "error: No key with id 'k_missing' exists." in capsys.readouterr().err


def test_json_output_matches_the_api_shape(capsys):
    main(PLAN_ARGS)
    capsys.readouterr()

    assert main(["plan", "list", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["data"]["total"] == 1
    assert body["data"]["plans"][0]["slug"] == "pro"


def test_quiet_suppresses_informational_lines(capsys):
    assert main(["-q", *PLAN_ARGS]) == 0
    assert capsys.readouterr().out == ""
