"""draft-ietf-httpapi-ratelimit-headers-07 rendering: strings, layer choice, ties."""

import pytest

from app.services import checker
from app.services.checker import ScriptResult
from app.services.keycache import ResolvedKey

KEY = ResolvedKey(
    key_id="k_headers0001",
    name="headers",
    plan_slug="pro",
    revoked=False,
    burst_capacity=100,
    burst_refill_per_sec=50,
    sustained_limit=5000,
    sustained_window_seconds=3600,
    monthly_quota=500000,
    quota_soft_pct=80,
    redis_down_policy="fail_open",
)


def result(**overrides) -> ScriptResult:
    fields = {
        "allowed": True,
        "deny_layer": "",
        "burst_limit": 100,
        "burst_remaining": 97,
        "burst_reset": 2,
        "sustained_limit": 5000,
        "sustained_remaining": 4993,
        "sustained_reset": 3599,
        "quota_limit": 500000,
        "quota_used": 12401,
        "quota_remaining": 487599,
        "quota_reset": 397440,
        "soft_crossed": False,
        "month": "2026-07",
        "retry_after": 0,
    }
    return ScriptResult(**{**fields, **overrides})


def test_policy_header_lists_burst_sustained_and_monthly_in_order():
    header = checker.policy_header(KEY, "2026-07")
    assert header == "100;w=2, 5000;w=3600, 500000;w=2678400"


def test_policy_header_uses_the_real_length_of_the_month():
    assert checker.policy_header(KEY, "2026-02").endswith("500000;w=2419200")
    assert checker.policy_header(KEY, "2024-02").endswith("500000;w=2505600")


def test_allow_reflects_the_most_constrained_layer():
    out = checker.to_out(KEY, result())
    assert out.headers["RateLimit"] == "limit=100, remaining=97, reset=2"
    assert (out.limit, out.remaining, out.reset) == (100, 97, 2)
    assert "Retry-After" not in out.headers
    assert out.retry_after is None


def test_allow_switches_to_the_quota_when_it_is_tighter():
    out = checker.to_out(KEY, result(quota_remaining=10, quota_used=499990))
    assert out.headers["RateLimit"] == "limit=500000, remaining=10, reset=397440"


def test_a_ratio_tie_goes_to_the_shorter_window():
    """Burst and sustained both half spent: the two second window wins over an hour."""
    out = checker.to_out(
        KEY, result(burst_remaining=50, sustained_remaining=2500, quota_remaining=500000)
    )
    assert out.headers["RateLimit"] == "limit=100, remaining=50, reset=2"


@pytest.mark.parametrize(
    ("layer", "expected"),
    [
        ("burst", "limit=100, remaining=0, reset=2"),
        ("sustained", "limit=5000, remaining=0, reset=41"),
        ("quota", "limit=500000, remaining=0, reset=397440"),
    ],
)
def test_deny_reflects_the_denying_layer(layer, expected):
    out = checker.to_out(
        KEY,
        result(
            allowed=False,
            deny_layer=layer,
            burst_remaining=0,
            sustained_remaining=0,
            sustained_reset=41,
            quota_remaining=0,
            retry_after=41,
        ),
    )
    assert out.headers["RateLimit"] == expected
    assert out.headers["Retry-After"] == "41"
    assert out.reason == layer
    assert out.retry_after == 41


def test_layers_are_always_fully_populated():
    out = checker.to_out(KEY, result(allowed=False, deny_layer="burst", retry_after=3))
    assert out.layers is not None
    assert out.layers.burst.limit == 100
    assert out.layers.sustained.limit == 5000
    assert out.layers.quota.month == "2026-07"
    assert out.layers.quota.soft_threshold_crossed is False


def test_burst_policy_window_rounds_up():
    key = ResolvedKey(**{**KEY.__dict__, "burst_capacity": 10, "burst_refill_per_sec": 3})
    assert checker.burst_window_seconds(key) == 4
