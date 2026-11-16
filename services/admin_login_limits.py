"""services/admin_login_limits.py - failure-based brute-force protection for the admin sign-in, with progressive delays.

Design (Patch C). Only FAILURES are counted; a successful PIN, a successful OTP, a logout, a page refresh or a trusted-device check never adds
to a failure counter. Counters are NOT shared more widely than they must be:

  pin_fail_pair  mobile + client IP     5 wrong PINs from one place for one account start a delay: 1, 2, 5, 15, then 30 minutes
  pin_fail_acct  mobile (all devices)   a looser ceiling (15 / 20 / 25 wrong PINs in an hour -> 5 / 15 / 30 minutes) that still stops a spread-out guesser
  pin_fail_ip    client IP              30 wrong PINs from one IP in 15 minutes -> 5 minute delay (stops one machine trying many accounts)
  otp_fail_acct  account                10 / 15 / 20 wrong codes in an hour (across challenges) -> 5 / 15 / 30 minute delay before a new code is sent
  verify_fail_ip client IP              20 wrong codes from one IP in 15 minutes -> 5 minute delay

The delay is counted from the most recent failure, so it ends by itself; nothing needs an administrator. Counters are keyed by the mobile NUMBER
(not by whether an account exists), so an unknown number behaves exactly like a real one. All events live in admin_auth_throttle and expire.
"""
import hashlib
import math
from datetime import datetime

from database import db
from services import admin_login_otp as O

# (kind, window minutes, [(failures needed, wait seconds), ...])
PIN_PAIR = ("pin_fail_pair", 60, [(5, 60), (6, 120), (7, 300), (8, 900), (9, 1800)])
PIN_ACCT = ("pin_fail_acct", 60, [(15, 300), (20, 900), (25, 1800)])
PIN_IP = ("pin_fail_ip", 15, [(30, 300)])
OTP_ACCT = ("otp_fail_acct", 60, [(10, 300), (15, 900), (20, 1800)])
VERIFY_IP = ("verify_fail_ip", 15, [(20, 300)])


def _pair_key(mobile: str, ip: str) -> str:
    return hashlib.sha256(("pair:%s|%s" % (mobile, ip)).encode("utf-8")).hexdigest()


def remaining(rule, key: str) -> int:
    """Seconds still to wait for this rule/key (0 = allowed). Read-only."""
    kind, window, table = rule
    n = O.throttle_count(kind, key, window)
    if n < table[0][0]:
        return 0
    wait = max(w for th, w in table if n >= th)
    last = db[O.THROTTLE].find_one({"kind": kind, "key": key}, sort=[("at", -1)])
    if not last:
        return 0
    left = wait - (datetime.utcnow() - last["at"]).total_seconds()
    return max(0, int(math.ceil(left)))


def pin_cooldown(mobile: str, ip: str) -> int:
    return max(remaining(PIN_PAIR, _pair_key(mobile, ip)), remaining(PIN_ACCT, O.mobile_key(mobile)), remaining(PIN_IP, ip))


def record_pin_failure(mobile: str, ip: str):
    mkey = O.mobile_key(mobile)
    O.throttle_hit(PIN_PAIR[0], _pair_key(mobile, ip), PIN_PAIR[1], group=mkey)
    O.throttle_hit(PIN_ACCT[0], mkey, PIN_ACCT[1], group=mkey)
    O.throttle_hit(PIN_IP[0], ip, PIN_IP[1])


def clear_pin_failures_here(mobile: str, ip: str):
    """Correct PIN from this place: its own wrong-PIN history here is forgiven (typos are not an attack)."""
    O.throttle_clear(PIN_PAIR[0], _pair_key(mobile, ip))


def clear_account(mobile: str):
    """Fully signed in, or an administrator reset the PIN: forgive every wrong-PIN / wrong-code counter of this mobile number."""
    db[O.THROTTLE].delete_many({"group": O.mobile_key(mobile), "kind": {"$in": [PIN_PAIR[0], PIN_ACCT[0], OTP_ACCT[0]]}})


def otp_cooldown(user_id) -> int:
    return remaining(OTP_ACCT, str(user_id))


def verify_cooldown(ip: str) -> int:
    return remaining(VERIFY_IP, ip)


def record_verify_failure(ip: str):
    O.throttle_hit(VERIFY_IP[0], ip, VERIFY_IP[1])


def wait_message(seconds: int) -> str:
    """The only time a user is told to wait: a real limit was reached. Generic (no account details)."""
    seconds = max(1, int(seconds))
    if seconds < 90:
        span = "%d seconds" % seconds
    else:
        span = "%d minutes" % int(math.ceil(seconds / 60.0))
    return "Too many incorrect attempts. Please try again in about " + span + "."
