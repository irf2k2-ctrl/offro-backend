"""Single source of truth for a store's effective subscription state.

Before this module the three places that show a store's status disagreed:
  * admin dashboard + customer Home read the newest `subscriptions.end_date`
    and compared it to the current UTC instant;
  * the merchant app read `stores.subscription_end` (a denormalised copy that
    is only refreshed by some flows) with an end-of-day rule, and the detail
    endpoint ignored string dates entirely.
So a store renewed/extended through the subscriptions collection could be
Active for admin/customers but Expired for its merchant.

Rule used everywhere now: the effective end is the LATEST end date found in
either the paid/active `subscriptions` rows or `stores.subscription_end`; a
subscription is valid THROUGH its end date (expired only after 23:59:59 on
that calendar day, India business clock - the same rule deals/offers use).
"""
from datetime import datetime, timedelta


# A subscription row in one of these states has NOT been paid for, so its end
# date must never make a store look active (a merchant who opened the renew
# screen and abandoned/cancelled payment leaves a "pending"/"failed" row with
# a future end_date behind).
UNPAID_STATES = ("pending", "failed", "cancelled", "canceled", "created")


def counts(status):
    return str(status or "").lower() not in UNPAID_STATES


def parse_dt(raw):
    """Naive datetime from a datetime / ISO string / 'DD Mon YYYY'; else None."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo else raw
    s = str(raw).strip()
    if not s or s in ("null", "None"):
        return None
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:19].replace("Z", ""), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s[:19].replace("Z", "").replace(" ", "T"))
    except ValueError:
        return None


def ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def is_expired(end, now=None):
    """True when [end] parses and its calendar day has fully passed (IST).
    None / unparseable / empty -> False (a bad date never expires a store)."""
    dt = parse_dt(end)
    if dt is None:
        return False
    return dt.replace(hour=23, minute=59, second=59, microsecond=0) < (now or ist_now())


def effective_end(store_end, sub_ends):
    """Latest parseable end date among the store copy and subscription rows.
    A subscription row with no end date is perpetual -> returns None
    (callers treat None as 'never expires')."""
    cands = []
    for e in [store_end, *list(sub_ends or [])]:
        d = parse_dt(e)
        if d is not None:
            cands.append(d)
    return max(cands) if cands else None


def store_effective_end(db, store):
    """effective_end() for a store document, reading its paid/active
    subscriptions (store_id stored as str or ObjectId)."""
    sid = store["_id"]
    ends = [r.get("end_date") for r in db.subscriptions.find(
        {"store_id": {"$in": [str(sid), sid]}, "status": {"$nin": list(UNPAID_STATES)}},
        {"end_date": 1})]
    return effective_end(store.get("subscription_end"), ends)


def effective_status(raw_status, end, now=None):
    """Displayed status: an 'active' store whose subscription ran out is 'expired'."""
    if raw_status == "active" and is_expired(end, now):
        return "expired"
    return raw_status
