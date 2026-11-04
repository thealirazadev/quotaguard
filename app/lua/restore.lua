-- Raise a quota counter to a floor value atomically.
-- Used after Redis data loss (FLUSHDB) to restore the current month counter
-- from the persisted rollup value. The shortfall is added with INCRBY rather
-- than SET, so the counter is only ever raised.
-- KEYS[1] = quota key (e.g., "qg:q:k_abc123:2026-07")
-- ARGV[1] = floor value (the persisted value from rollups)
-- ARGV[2] = month end (epoch seconds for EXPIREAT)
-- Returns the shortfall that was added, 0 when the counter was already at or above the floor.

local key = KEYS[1]
local floor = tonumber(ARGV[1])
local month_end = tonumber(ARGV[2])

local current = tonumber(redis.call("GET", key) or 0)
if current >= floor then
    return 0
end

local shortfall = floor - current
redis.call("INCRBY", key, string.format("%.0f", shortfall))
redis.call("EXPIREAT", key, string.format("%.0f", month_end + 3888000))
return shortfall
