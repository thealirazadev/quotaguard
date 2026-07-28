-- Atomic admission decision: burst bucket, sliding window, monthly quota.
-- Redis runs this serially, so the whole decision is one indivisible step: all three
-- layers must allow before anything is consumed, and a deny writes nothing except the
-- trim of already-expired window members.
--
-- KEYS: [1] bucket hash qg:b:{key_id}:{resource}
--       [2] window zset qg:s:{key_id}:{resource}
--       [3] quota key prefix qg:q:{key_id}:   (the month is appended in-script)
--       [4] flag key prefix  qg:f:{key_id}:
-- ARGV: [1] cap_utok  [2] rate_utok  [3] sustained_limit  [4] window_us
--       [5] quota_hard  [6] quota_soft  [7] cost  [8] request_id

local bkey = KEYS[1]
local skey = KEYS[2]

local cap_utok = tonumber(ARGV[1])
local rate_utok = tonumber(ARGV[2])
local sustained_limit = tonumber(ARGV[3])
local window_us = tonumber(ARGV[4])
local quota_hard = tonumber(ARGV[5])
local quota_soft = tonumber(ARGV[6])
local cost = tonumber(ARGV[7])
local request_id = ARGV[8]

local UTOK = 1000000

-- Lua numbers are doubles; the default tostring switches to scientific notation well
-- below the microsecond magnitudes used here, which Redis would then store or score
-- with fewer digits. Every number handed to redis.call goes through this.
local function n2s(value)
  return string.format('%.0f', value)
end

-- civil-calendar helpers (extracted verbatim by tests/test_civil_dates.py; keep the
-- sentinel comments intact). Standard days-from-civil / civil-from-days construction,
-- integer arithmetic only, valid for the whole 64-bit epoch range.
-- <civil>
local function civil_from_days(days)
  local z = days + 719468
  local era = math.floor(z / 146097)
  local doe = z - era * 146097
  local yoe = math.floor(
    (doe - math.floor(doe / 1460) + math.floor(doe / 36524) - math.floor(doe / 146096)) / 365
  )
  local y = yoe + era * 400
  local doy = doe - (365 * yoe + math.floor(yoe / 4) - math.floor(yoe / 100))
  local mp = math.floor((5 * doy + 2) / 153)
  local d = doy - math.floor((153 * mp + 2) / 5) + 1
  local m = mp + 3
  if mp >= 10 then m = mp - 9 end
  if m <= 2 then y = y + 1 end
  return y, m, d
end

local function days_from_civil(y, m, d)
  if m <= 2 then y = y - 1 end
  local era = math.floor(y / 400)
  local yoe = y - era * 400
  local mp = m - 3
  if m <= 2 then mp = m + 9 end
  local doy = math.floor((153 * mp + 2) / 5) + d - 1
  local doe = yoe * 365 + math.floor(yoe / 4) - math.floor(yoe / 100) + doy
  return era * 146097 + doe - 719468
end
-- </civil>

-- 1. The only clock in the whole decision.
local time = redis.call('TIME')
local now_s = tonumber(time[1])
local now_us = now_s * UTOK + tonumber(time[2])

-- 2. Month derivation, so the client never needs a clock to name the quota key.
local year, month = civil_from_days(math.floor(now_s / 86400))
local month_str = string.format('%04d-%02d', year, month)
local next_year, next_month = year, month + 1
if next_month > 12 then
  next_year, next_month = year + 1, 1
end
local month_end_s = days_from_civil(next_year, next_month, 1) * 86400
local qkey = KEYS[3] .. month_str
local fkey = KEYS[4] .. month_str

-- 3. Bucket read and refill. Every rounding under-credits, never the other way.
local state = redis.call('HMGET', bkey, 't', 'ts')
local tokens, ts
if state[1] == false or state[2] == false then
  tokens = cap_utok
  ts = now_us
else
  tokens = tonumber(state[1])
  ts = tonumber(state[2])
  local elapsed = now_us - ts
  if elapsed < 0 then
    -- TIME can regress across a failover; never credit backwards time.
    elapsed = 0
  end
  if elapsed >= (cap_utok / rate_utok) * UTOK then
    tokens = cap_utok
    ts = now_us
  else
    local add = math.floor((elapsed / UTOK) * rate_utok)
    if add > 0 then
      tokens = tokens + add
      if tokens >= cap_utok then
        tokens = cap_utok
        ts = now_us
      else
        -- Advance only by the time the credited tokens are worth, so the floored
        -- remainder stays as un-elapsed time instead of being discarded.
        ts = ts + math.ceil(add * UTOK / rate_utok)
      end
    end
  end
end
local burst_ok = tokens >= cost * UTOK

-- 4. Window trim (the memory bound, applied on allow and deny alike) and read.
redis.call('ZREMRANGEBYSCORE', skey, '-inf', '(' .. n2s(now_us - window_us))
local count = redis.call('ZCARD', skey)
local sustained_ok = (count + 1) <= sustained_limit

-- 5. Monthly quota read.
local used = tonumber(redis.call('GET', qkey)) or 0
local quota_ok = (used + cost) <= quota_hard

-- 6. Decision: every layer agrees, or nothing is consumed.
local allowed = burst_ok and sustained_ok and quota_ok
local deny_layer = ''
if not allowed then
  if not burst_ok then
    deny_layer = 'burst'
  elseif not sustained_ok then
    deny_layer = 'sustained'
  else
    deny_layer = 'quota'
  end
end

-- 7. Consume, only when admitted.
local new_used = used
local count_after = count
local soft_crossed = 0
if allowed then
  tokens = tokens - cost * UTOK
  redis.call('HSET', bkey, 't', n2s(tokens), 'ts', n2s(ts))
  redis.call('PEXPIRE', bkey, n2s(math.ceil(cap_utok / rate_utok) * 1000 + 60000))

  redis.call('ZADD', skey, n2s(now_us), n2s(now_us) .. '-' .. request_id)
  redis.call('PEXPIRE', skey, n2s(math.ceil(window_us / 1000) + 60000))
  count_after = count + 1

  new_used = redis.call('INCRBY', qkey, cost)
  if new_used == cost then
    redis.call('EXPIREAT', qkey, n2s(month_end_s + 3888000))
  end

  if quota_soft > 0 and used < quota_soft and new_used >= quota_soft then
    -- SETNX returns 1 for exactly one caller ever, even under concurrency.
    soft_crossed = redis.call('SETNX', fkey, 1)
    if soft_crossed == 1 then
      redis.call('EXPIREAT', fkey, n2s(month_end_s + 3888000))
    end
  end
end

-- 8. Post-decision reporting values.
local b_limit = math.floor(cap_utok / UTOK)
local b_remaining = math.floor(tokens / UTOK)
local b_reset = math.ceil((cap_utok - tokens) / rate_utok)

local s_remaining = sustained_limit - count_after
if s_remaining < 0 then
  s_remaining = 0
end
local s_reset = 0
local oldest = redis.call('ZRANGE', skey, 0, 0, 'WITHSCORES')
if oldest[2] then
  s_reset = math.ceil((tonumber(oldest[2]) + window_us - now_us) / UTOK)
  if s_reset < 0 then
    s_reset = 0
  end
end

local q_remaining = quota_hard - new_used
if q_remaining < 0 then
  q_remaining = 0
end
local q_reset = month_end_s - now_s

local retry_after = 0
if not allowed then
  if not burst_ok then
    retry_after = math.ceil((cost * UTOK - tokens) / rate_utok)
  end
  if not sustained_ok and s_reset > retry_after then
    retry_after = s_reset
  end
  if not quota_ok and q_reset > retry_after then
    retry_after = q_reset
  end
end

return {
  allowed and 1 or 0, deny_layer,
  b_limit, b_remaining, b_reset,
  sustained_limit, s_remaining, s_reset,
  quota_hard, new_used, q_remaining, q_reset,
  soft_crossed, month_str, retry_after,
}
