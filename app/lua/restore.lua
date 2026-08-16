-- Restore a quota counter to a floor value atomically.
-- Used after Redis data loss (FLUSHDB) to restore the current month counter
-- from the persisted rollup value.
-- KEYS[1] = quota key (e.g., "qg:q:k_abc123:2026-07")
-- ARGV[1] = floor value (the persisted value from rollups)
-- ARGV[2] = month end (epoch seconds for EXPIREAT)
-- Returns the new value after the restore.

local key = KEYS[1]
local floor = tonumber(ARGV[1])
local month_end = tonumber(ARGV[2])

local current = tonumber(redis.call("GET", key) or 0)
if current < floor then
    redis.call("SET", key, floor)
    redis.call("EXPIREAT", key, month_end + 3888000)
    return floor
else
    return current
end
