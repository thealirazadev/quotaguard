"""Cross-check the Lua civil-calendar conversion against Python's calendar.

The helpers are lifted verbatim out of app/lua/check.lua between its sentinel
comments and evaluated on a real Redis, so this exercises the shipped code rather
than a Python re-implementation of it.
"""

import random
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

SOURCE = (Path(__file__).resolve().parent.parent / "app" / "lua" / "check.lua").read_text()

HARNESS = """
local now_s = tonumber(ARGV[1])
local y, m = civil_from_days(math.floor(now_s / 86400))
local ny, nm = y, m + 1
if nm > 12 then ny, nm = y + 1, 1 end
return {string.format('%04d-%02d', y, m),
        string.format('%.0f', days_from_civil(ny, nm, 1) * 86400)}
"""

# Month ends, leap days, the 1900/2100 non-leap century rule, and the epoch itself.
FIXED_EPOCHS = [
    0,  # 1970-01-01
    951782400,  # 2000-02-29, leap day of a leap century
    951868799,  # one second before 2000-03-01
    4107542400,  # 2100-03-01, the century that is not a leap year
    4102444800,  # 2100-01-01
    1709164800,  # 2024-02-29
    1767225599,  # 2025-12-31 23:59:59
    1767225600,  # 2026-01-01 00:00:00
    1798761599,  # 2026-12-31 23:59:59
    1785196800,  # mid 2026
]


def _extract_civil_block() -> str:
    match = re.search(r"-- <civil>(.*?)-- </civil>", SOURCE, re.S)
    assert match is not None, "check.lua lost its civil-calendar sentinel comments"
    return match.group(1)


def _expected(epoch: int) -> tuple[str, int]:
    moment = datetime.fromtimestamp(epoch, UTC)
    next_year, next_month = (
        (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    )
    month_end = datetime(next_year, next_month, 1, tzinfo=UTC)
    return moment.strftime("%Y-%m"), int(month_end.timestamp())


@pytest.fixture
def civil(redis_client):
    source = _extract_civil_block() + HARNESS

    async def _civil(epoch: int) -> tuple[str, int]:
        month, month_end = await redis_client.eval(source, 0, epoch)
        return month, int(month_end)

    return _civil


@pytest.mark.parametrize("epoch", FIXED_EPOCHS)
async def test_month_and_month_end_match_python(civil, epoch):
    assert await civil(epoch) == _expected(epoch)


async def test_month_boundaries_across_two_decades(civil):
    """Both sides of every month boundary from 2020 to 2039."""
    for year in range(2020, 2040):
        for month in range(1, 13):
            start = int(datetime(year, month, 1, tzinfo=UTC).timestamp())
            for epoch in (start, start - 1, start + 86399):
                assert await civil(epoch) == _expected(epoch), epoch


async def test_random_epochs_match_python(civil):
    rng = random.Random(20260729)
    for _ in range(250):
        epoch = rng.randrange(0, 4_102_444_800)
        assert await civil(epoch) == _expected(epoch), epoch


async def test_script_reports_the_current_month(script, make_plan, make_key, do_check):
    """The month the script derives from Redis TIME is the real UTC month."""
    await make_plan()
    key = await make_key()
    decision = await do_check(key["api_key"])
    assert decision["layers"]["quota"]["month"] == datetime.now(UTC).strftime("%Y-%m")
