"""services/notification_audience.py - who an admin push notification may reach (Phase 2, Stage 1, Task E).

Replaces the old broadcast loop of POST /admin/notifications/send, which (a) ignored the selected city (the computed FCM topic was never
used, so a "city" notification reached every token holder), (b) read at most 3000 accounts and 3000 users with no warning and (c) could
not say how many recipients were skipped. Everything here is read-only; nothing is written to accounts or users.

Targets
  all_users  every account (customers AND merchants, as before) - narrowed to the admin's own country and/or assigned cities when the
             admin is restricted.
  city       CUSTOMER accounts (role "user", or no roles field = legacy customer) whose `city` is the selected city AND whose country is the
             city's country. The country comes from the City Master (a city that exists in one country) or from `target_country`; if it
             cannot be established the request is refused - it never falls back to India or to "any country".
  country    (Phase 2 / Task 4) every account (customers AND merchants - exactly the population "all_users" has always reached) whose country
             is the selected one. It is "all_users" narrowed to one country, so an unrestricted admin can now do deliberately what a
             country-restricted admin's "all_users" already did implicitly. A country-restricted admin may select only their own country;
             an admin with assigned cities still reaches only those cities. The country must be given and supported (never defaulted).
             Legacy `users` records carry no reliable country and are never included.
  specific   unchanged (one phone number).
  offers     REFUSED. The apps subscribe every device to the `offers` topic and the server stores no offers opt-in, so "Offers Subscribers"
             cannot be told apart from "everyone" - it is not guessed.

Counting rules (documented so the numbers can be checked)
  eligible_records     accounts (and legacy `users` records) that match the audience, with or without a token
  skipped_no_token     matching records that have no device token
  skipped_duplicate    matching records whose token was already taken by another record (one device = one message)
  unique_tokens        the devices that will be tried
Device tokens are never returned, logged or stored in the history.
"""
import re
from typing import Dict, List, Optional

from fastapi import HTTPException

from services import dashboard_country as _dc
from utils import countries as _countries

BATCH = 1000                      # records read per database page
SEND_BATCH = 500                  # devices sent per batch
INLINE_MAX = 500                  # more devices than this are sent in the background so the request cannot time out
BROADCAST_TARGETS = ("all", "all_users", "city", "country")
OFFERS_MESSAGE = ("The 'Offers Subscribers' target is disabled: the apps subscribe every device to it and the server keeps no offers opt-in, "
                  "so it would reach everyone. Use 'All Users' or a city target.")


class _LiveDb:
    def __getattr__(self, name):
        import database
        return getattr(database.db, name)


db = _LiveDb()


def _keys(d: dict) -> set:
    keys = {str(d.get("name") or "").strip().lower(), str(d.get("slug") or "").strip().lower()}
    names = d.get("names")
    if isinstance(names, dict):
        keys.update(str(v or "").strip().lower() for v in names.values())
    return {k for k in keys if k}


def city_restriction(admin: dict) -> Optional[List[str]]:
    """None = no city restriction (Super Admin or '*'); else the assigned cities (possibly empty = nothing allowed)."""
    admin = admin or {}
    cities = admin.get("assigned_cities") or []
    if admin.get("role_name") == _dc.SUPER_ADMIN or "*" in cities:
        return None
    return [str(c) for c in cities]


def _city_regex(names) -> dict:
    alts = sorted({re.escape(str(n).strip()) for n in names if str(n or "").strip()})
    if not alts:
        return {"city": "\x00none"}                                  # matches nothing (fail closed)
    return {"city": {"$regex": r"^\s*(" + "|".join(alts) + r")\s*$", "$options": "i"}}


def resolve_city(city: str, country_hint: str) -> dict:
    """{'iso', 'names'}: the city's country and every spelling of it to match on. Raises 400 when the country is not certain."""
    typed = str(city or "").strip()
    if not typed:
        raise HTTPException(400, "City name is required for a city notification")
    hint = str(country_hint or "").strip().upper()
    if hint and not _countries.country_info(hint):
        raise HTTPException(400, "The selected country is not supported")
    low = typed.lower()
    docs = [d for d in db.cities.find({}, {"name": 1, "slug": 1, "names": 1, "country": 1}) if low in _keys(d)]
    countries = sorted({str(d.get("country") or "").upper() for d in docs if _countries.country_info(str(d.get("country") or "").upper())})
    if hint:
        iso = hint
        use = [d for d in docs if str(d.get("country") or "").upper() == iso]
    elif len(countries) == 1:
        iso = countries[0]
        use = [d for d in docs if str(d.get("country") or "").upper() == iso]
    elif len(countries) > 1:
        raise HTTPException(400, "More than one country has a city called '%s' (%s). Choose the country." % (typed, ", ".join(countries)))
    else:
        raise HTTPException(400, "The country of '%s' cannot be determined from the City Master. Choose the country." % typed)
    names = {typed}
    for d in use:
        names.add(str(d.get("name") or ""))
        names.update(_keys(d))
    return {"iso": iso, "names": sorted(n for n in names if n)}


def _customer_clause() -> dict:
    return {"$or": [{"roles": {"$exists": False}}, {"roles": "user"}]}


def _in_assigned(city: str, assigned: List[str]) -> bool:
    low = str(city or "").strip().lower()
    return bool(low) and low in {str(c).strip().lower() for c in assigned}


def build(admin: dict, target: str, city: str = "", country: str = "") -> dict:
    """Read the audience (DB only). Returns the plan: {'tokens': [...], eligible_records, skipped_no_token, skipped_duplicate,
    unique_tokens, country, city}. Raises HTTPException for a refused target / unsafe scope."""
    own = _dc.scope(admin)
    if target == "offers":
        raise HTTPException(403 if own else 400, OFFERS_MESSAGE)
    if target not in BROADCAST_TARGETS:
        raise HTTPException(400, "Unknown notification target")
    if own == _dc.INVALID_SCOPE and target == "city":                 # a corrupted country on the user: fail closed
        raise HTTPException(403, "This target is not available for your account")
    assigned = city_restriction(admin)
    iso, clauses, out_city = "", [], ""
    if target == "country":
        want = str(country or "").strip().upper()
        if not want:
            raise HTTPException(400, "Select a country")
        if not _countries.country_info(want):
            raise HTTPException(400, "The selected country is not supported")
        if own and own != want:                                       # also refuses INVALID_SCOPE (equals no country)
            raise HTTPException(403, "You can only send to your own country")
        iso = want
        clauses.append(_dc.account_query(iso))
        if assigned is not None:
            clauses.append(_city_regex(assigned))
    elif target == "city":
        info = resolve_city(city, country)
        iso = info["iso"]
        if own and own != iso:
            raise HTTPException(403, "This city is not in your country")
        if assigned is not None and not (_in_assigned(city, assigned) or any(_in_assigned(n, assigned) for n in info["names"])):
            raise HTTPException(403, "This city is not one of your assigned cities")
        clauses = [_customer_clause(), _city_regex(info["names"]), _dc.account_query(iso)]
        out_city = str(city).strip()
    else:
        if own:
            iso = own
            clauses.append(_dc.account_query(own))
        if assigned is not None:
            clauses.append(_city_regex(assigned))
    q = {"$and": clauses} if clauses else {}
    # legacy `users` hold no reliable country: read them only when no country is being applied (as the old code did)
    colls = [db.accounts] if (own or target in ("city", "country")) else [db.accounts, db.users]
    seen, tokens = set(), []
    eligible = no_token = dup = 0
    for coll in colls:
        last = None
        while True:
            conds = ([q] if q else []) + ([{"_id": {"$gt": last}}] if last is not None else [])
            page_q = {"$and": conds} if conds else {}
            page = list(coll.find(page_q, {"fcm_token": 1}).sort("_id", 1).limit(BATCH))
            if not page:
                break
            for rec in page:
                eligible += 1
                tok = str(rec.get("fcm_token") or "").strip()
                if not tok:
                    no_token += 1
                elif tok in seen:
                    dup += 1
                else:
                    seen.add(tok)
                    tokens.append(tok)
            last = page[-1]["_id"]
            if len(page) < BATCH:
                break
    return {"tokens": tokens, "eligible_records": eligible, "skipped_no_token": no_token, "skipped_duplicate": dup,
            "unique_tokens": len(tokens), "country": iso, "city": out_city}


def summary(plan: dict) -> dict:
    """The plan without the device tokens."""
    return {k: v for k, v in plan.items() if k != "tokens"}


def deliver(tokens: List[str], send_one, on_batch=None, workers: int = 10) -> Dict[str, int]:
    """Send to every token in batches of SEND_BATCH with a small thread pool. send_one(token) raises on failure. Returns
    {'sent','failed'} and an 'error' class name (never a token). `on_batch(sent, failed)` is called after each batch."""
    from concurrent.futures import ThreadPoolExecutor
    sent = failed = 0
    err = ""

    def one(tok):
        try:
            send_one(tok)
            return None
        except Exception as e:                                     # noqa: BLE001 - counted, reported by class only
            return type(e).__name__
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for i in range(0, len(tokens), SEND_BATCH):
            for r in ex.map(one, tokens[i:i + SEND_BATCH]):
                if r is None:
                    sent += 1
                else:
                    failed += 1
                    err = err or r
            if on_batch:
                on_batch(sent, failed)
    return {"sent": sent, "failed": failed, "error": err}


def final_status(sent: int, failed: int) -> str:
    if sent and not failed:
        return "sent"
    if sent and failed:
        return "partial"
    if failed:
        return "failed"
    return "skipped_no_tokens"
