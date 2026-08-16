-- Compare-and-delete advisory lock release.
-- KEYS[1] = lock key (e.g., "qg:lock:rollup")
-- ARGV[1] = lock token (the value set by the lock acquisition)
-- Returns 1 if the lock was held by this token and deleted; 0 otherwise.

local key = KEYS[1]
local token = ARGV[1]

if redis.call("GET", key) == token then
    redis.call("DEL", key)
    return 1
else
    return 0
end
