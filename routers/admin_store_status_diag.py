"""TEMPORARY, READ-ONLY diagnostic: why can one store show different subscription statuses in the admin dashboard, the customer app
and the merchant app?   GET /admin/diagnostics/store-status?name=test store stage   (or ?store_id=<id>)

* Dashboard session required (the same `get_current_admin` every /admin route uses) AND the system Super Admin role.
* Never writes: only find / find_one. Nothing is synchronised or corrected.
* Returns no payment-gateway identifiers, signatures, tokens, phone numbers, names or other customer data: subscription rows are
  passed through a field WHITELIST (the names of any other fields are listed, never their values).
* Remove this module and its include_router line in server.py once the root cause is fixed.
"""
import re
from datetime import datetime

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from database import db
from routers.admin import get_current_admin
from services import store_status as ss

router = APIRouter(tags=["Admin diagnostics (temporary)"])

DIAG_VERSION = "store-status-diag-v1"
_SUB_FIELDS = ("plan", "plan_name", "status", "payment_status", "start_date", "end_date", "created_at", "updated_at", "paid_at",
               "amount", "currency", "country", "source", "type", "mode", "manual", "duration_days", "days", "renewal", "is_renewal")


def _is_super_admin(a: dict) -> bool:
    role = a.get("role") if isinstance(a.get("role"), dict) else {}
    return role.get("role_name") == "Super Admin" and role.get("is_system_role") is True


def _iso(v):
    if isinstance(v, datetime):
        return v.isoformat()
    return v if isinstance(v, (str, int, float, bool)) or v is None else str(v)


def _type_of(v):
    return type(v).__name__


def _sub_row(r: dict, store_id: str, now) -> dict:
    sid = r.get("store_id")
    end = r.get("end_date")
    parsed = ss.parse_dt(end)
    counts = ss.counts(r.get("status"))
    row = {"_id": str(r.get("_id")),
           "store_id_type": _type_of(sid), "store_id_matches": str(sid) == store_id,
           "end_date_type": _type_of(end), "end_date_parsed": _iso(parsed),
           "status_counts_as_paid": counts,
           "valid_now": bool(counts and (end in (None, "") or (parsed is not None and not ss.is_expired(parsed, now)))),
           "other_field_names": sorted(k for k in r if k not in _SUB_FIELDS and k not in ("_id", "store_id"))}
    for k in _SUB_FIELDS:
        if k in r:
            row[k] = _iso(r[k])
    return row


def _admin_rule(subs: list, now) -> dict:
    """routers/admin.py::_fmt_store_fast - newest PAID row (else newest row) decides; status from its end_date."""
    chosen = next((r for r in subs if ss.counts(r.get("status"))), subs[0] if subs else None)
    if chosen is None:
        return {"status": "unpaid", "decided_by": None}
    raw = chosen.get("status", "")
    if not ss.counts(raw):
        st = "unpaid"
    else:
        ed = ss.parse_dt(chosen.get("end_date"))
        st = ("expired" if ss.is_expired(ed, now) else "active") if ed is not None else ("active" if raw in ("active", "paid") else "unpaid")
    return {"status": st, "decided_by": str(chosen.get("_id"))}


def _customer_rule(store: dict, subs: list, now) -> dict:
    """routers/public.py::get_stores - listed only when stores.status == 'active' AND any counted sub is valid (no end_date = perpetual)."""
    valid = []
    for r in subs:
        if not ss.counts(r.get("status")):
            continue
        ed = r.get("end_date")
        if ed in (None, ""):
            valid.append(str(r.get("_id")))
        elif ss.parse_dt(ed) is not None and not ss.is_expired(ed, now):
            valid.append(str(r.get("_id")))
    listed = store.get("status") == "active" and bool(valid)
    return {"status": "active" if listed else ("hidden" if store.get("status") == "active" else str(store.get("status"))),
            "visible_in_customer_app": listed, "valid_subscription_ids": valid}


def _merchant_rule(store: dict, now) -> dict:
    """routers/merchant_app.py (Phase 5): shared rule - latest end over paid rows and stores.subscription_end, valid through that day (IST)."""
    end = ss.store_effective_end(db, store)
    return {"status": ss.effective_status(store.get("status", "draft"), end, now), "effective_end": _iso(end)}


def _merchant_legacy_rule(store: dict) -> dict:
    """The pre-Phase-5 merchant rule (stores.subscription_end only, UTC clock). If this is what the merchant app shows, the deployed
    backend does not have the Phase 5 files, or stores.subscription_end is stale."""
    raw = store.get("status", "draft")
    end = store.get("subscription_end")
    expired = False
    if raw == "active" and end:
        d = end if isinstance(end, datetime) else ss.parse_dt(end)
        if d is not None:
            expired = d.replace(hour=23, minute=59, second=59) < datetime.utcnow()
    return {"status": "expired" if expired else raw, "uses_only": "stores.subscription_end (UTC)"}


def _diagnose(store: dict, now) -> dict:
    sid = str(store["_id"])
    rows = list(db.subscriptions.find({"store_id": {"$in": [sid, store["_id"]]}}).sort("created_at", -1))
    rule_admin = _admin_rule(rows, now)
    rule_customer = _customer_rule(store, rows, now)
    rule_merchant = _merchant_rule(store, now)
    legacy = _merchant_legacy_rule(store)

    def norm(s):  # "hidden" (customer) = store not listed; compare on active vs not-active
        return "active" if s == "active" else "not_active"
    verdicts = {"admin_dashboard": rule_admin["status"], "customer_app": rule_customer["status"], "merchant_app": rule_merchant["status"]}
    consistent = len({norm(v) for v in verdicts.values()}) == 1
    hints = []
    if consistent and norm(legacy["status"]) != norm(rule_merchant["status"]):
        hints.append("The CURRENT shared rule gives one answer for all three interfaces, but the pre-Phase-5 merchant rule gives a "
                     "different one. If the merchant app still shows the old answer, the staging backend is not running the Phase 5 "
                     "backend files (or was not restarted).")
    if not consistent:
        hints.append("The three rules disagree on the data itself - read subscriptions[*].status_counts_as_paid / valid_now / "
                     "end_date_parsed / store_id_matches to see which row each interface used.")
    if any(not r["store_id_matches"] for r in map(lambda x: _sub_row(x, sid, now), rows)):
        hints.append("A subscription row matched this store through a different store_id type.")
    if store.get("subscription_end") and rows:
        newest = max((ss.parse_dt(r.get("end_date")) for r in rows if ss.counts(r.get("status")) and ss.parse_dt(r.get("end_date"))),
                     default=None)
        own = ss.parse_dt(store.get("subscription_end"))
        if newest and own and own < newest:
            hints.append("stores.subscription_end (%s) is OLDER than the newest paid subscription end (%s): the store copy is stale. "
                         "The pre-Phase-5 merchant rule reads only that copy." % (own.date(), newest.date()))
    return {
        "store": {"store_id": sid, "store_name": store.get("store_name"), "merchant_id": str(store.get("merchant_id")),
                  "city": store.get("city"), "status": store.get("status"), "subscription_end": _iso(store.get("subscription_end")),
                  "subscription_end_type": _type_of(store.get("subscription_end")), "subscription_plan": store.get("subscription_plan")},
        "subscriptions": [_sub_row(r, sid, now) for r in rows],
        "final_status": {**verdicts, "all_three_agree": consistent},
        "rule_detail": {"admin_dashboard": rule_admin, "customer_app": rule_customer, "merchant_app": rule_merchant,
                        "merchant_app_pre_phase5_rule": legacy},
        "hints": hints,
    }


@router.get("/diagnostics/store-status")
def store_status_diagnostic(name: str = "", store_id: str = "", a=Depends(get_current_admin)):
    if not _is_super_admin(a):
        raise HTTPException(403, "Super Admin only")
    if not name.strip() and not store_id.strip():
        raise HTTPException(400, "Pass ?name=<store name> or ?store_id=<id>")
    if store_id.strip():
        try:
            q = {"_id": ObjectId(store_id.strip())}
        except Exception:
            raise HTTPException(400, "Invalid store_id")
    else:
        q = {"store_name": {"$regex": "^\\s*" + re.escape(name.strip()) + "\\s*$", "$options": "i"}}
    stores = list(db.stores.find(q).limit(10))
    now = ss.ist_now()
    return {"diag_version": DIAG_VERSION, "read_only": True,
            "server_time_utc": datetime.utcnow().isoformat(), "server_time_ist": now.isoformat(),
            "merchant_endpoint_uses_shared_rule": _merchant_uses_shared_rule(),
            "matches": len(stores), "stores": [_diagnose(s, now) for s in stores]}


def _merchant_uses_shared_rule() -> bool:
    try:
        import routers.merchant_app as ma
        return getattr(ma, "_ss", None) is ss
    except Exception:
        return False
