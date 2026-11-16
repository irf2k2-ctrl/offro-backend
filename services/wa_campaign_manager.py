"""
services/wa_campaign_manager.py
-------------------------------
Campaign lifecycle for the WhatsApp Campaign Manager (Phase 2D): drafts, validation, recipient snapshot, schedule
METADATA and the single-recipient TEST send. There is NO bulk sender and NO worker here: nothing in this module reads a
schedule and acts on it, and the only code path that can contact WhatsApp is `send_test_message`, which is locked behind every gate
below. Bulk execution is Phase 2E.

Status lifecycle (changes are conditional updates on status AND version, so two admins / replicas cannot both win):

    draft ──validate──▶ ready ──schedule──▶ scheduled        draft/ready/scheduled ──cancel──▶ cancelled (terminal)
      ▲                  │ ▲                    │             scheduled ──unschedule──▶ ready
      └──revert-to-draft─┘ └────────────────────┘             completed: reserved for the Phase 2E worker; unreachable here

    * Only `draft` can be edited. Any edit of a ready campaign goes back through revert-to-draft + validate.
    * `validate` freezes a recipient SNAPSHOT (eligible recipients + their resolved template values) in
      `wa_campaign_recipients`; counts of the excluded, by reason, are kept on the campaign (no numbers for excluded people).
    * `schedule` is the explicit admin confirmation. It only stores `schedule.send_at`; no process reads it in 2D.
      There is deliberately no "send now" here: immediate launch will be its own approved action in Phase 2E.

TEST send gates (all must pass, in this order; nothing reaches Meta until the local gates have passed):
    explicit confirm flag -> campaign has a usable template -> Phase 2A kill switch (env key AND DB switch) ->
    number is on the WA_CAMPAIGN_TEST_NUMBERS allow-list -> Phase 2A eligibility (consent / STOP, fail closed) ->
    ATOMIC per-recipient claim -> rate limits -> LIVE Meta re-verification of the template (approved, marketing, unchanged since
    mapping) -> payload built strictly from the mapping (never guesses) -> audit-first attempt record -> rate-limit re-check ->
    FINAL consent / STOP re-read (the last step before the call) -> send (classified outcome).
    An 'uncertain' outcome is recorded, holds the claim for UNCERTAIN_HOLD, and is NEVER retried automatically.

Privacy: phone numbers are stored only canonically inside snapshot / test-send records (needed for later sending), are
masked everywhere they are returned, and never appear in audit entries (last-4 + hash only).
"""
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.errors import DuplicateKeyError

from services import wa_audience as AU
from services import wa_campaign_audit as audit
from services import wa_campaign_settings as ST
from services import wa_consent as C
from services import wa_eligibility as E
from services import wa_templates as T
from services import whatsapp as WA
from services.wa_webhook_security import exc_summary, mask_phone

logger = logging.getLogger("offro.wa_campaigns.manager")

db = None  # set lazily to database.db (patchable in tests)
CAMPAIGNS = "wa_campaigns"
RECIPIENTS = "wa_campaign_recipients"
TEST_SENDS = "wa_campaign_test_sends"
TEST_LOCKS = "wa_campaign_test_locks"           # one atomic claim document per (campaign, number)

DRAFT, READY, SCHEDULED, CANCELLED, COMPLETED = "draft", "ready", "scheduled", "cancelled", "completed"
STATUSES = (DRAFT, READY, SCHEDULED, CANCELLED, COMPLETED)
TRANSITIONS = {DRAFT: {READY, CANCELLED}, READY: {DRAFT, SCHEDULED, CANCELLED}, SCHEDULED: {READY, CANCELLED},
               CANCELLED: set(), COMPLETED: set()}

NAME_MIN, NAME_MAX, DESC_MAX = 3, 100, 500
MAX_SELECTED_IDS = 20000
SNAPSHOT_MAX_AGE = timedelta(hours=24)         # a confirmation must rest on a recent consent check
MIN_SCHEDULE_LEAD = timedelta(minutes=15)
MAX_SCHEDULE_AHEAD = timedelta(days=90)
TEST_COOLDOWN = timedelta(seconds=60)          # same campaign + same number (held after sent / failed too)
UNCERTAIN_HOLD = timedelta(minutes=15)         # after an UNKNOWN outcome: no repeat until a human has checked the phone
TEST_MAX_PER_ADMIN_HOUR = 10
TEST_MAX_GLOBAL_HOUR = 30
INSERT_BATCH = 500
_MEDIA_ID_RE = re.compile(r"^[0-9]{5,32}$")

SAMPLE_RECIPIENT = {"merchant_name": "Sample Merchant", "store_name": "Sample Store", "store_city": "Pune",
                    "store_area": "Kothrud", "recipient_name": "Sample Contact"}


def _db():
    global db
    if db is None:
        from database import db as _d
        db = _d
    return db


class CampaignError(Exception):
    """A refusal the caller can act on. `http` is the API status; `detail` goes to the response body."""
    def __init__(self, code, http=400, message="", **extra):
        super().__init__(code)
        self.code, self.http, self.message, self.extra = code, http, message or code, extra

    def body(self):
        return {"code": self.code, "message": self.message, **self.extra}


# ── indexes (new collections only; created at startup, safe on Production) ─────────────────────

def ensure_indexes():
    d = _db()
    d[CAMPAIGNS].create_index([("status", 1), ("schedule.send_at", 1)], name="wa_campaigns_status_sendat")
    d[CAMPAIGNS].create_index([("created_at", -1)], name="wa_campaigns_created")
    d[RECIPIENTS].create_index([("campaign_id", 1), ("phone", 1)], unique=True, name="wa_campaign_recipients_unique")
    d[TEST_SENDS].create_index([("campaign_id", 1), ("created_at", -1)], name="wa_campaign_test_sends_campaign")
    d[TEST_SENDS].create_index([("created_at", -1)], name="wa_campaign_test_sends_created")


def ensure_indexes_safe() -> bool:
    try:
        ensure_indexes()
        return True
    except Exception as e:
        logger.error("[WA-Campaign] campaign index creation skipped (%s)", exc_summary(e))
        return False


# ── validation of editable fields ───────────────────────────────────────────────────────────────

def _clean_text(value, limit, field):
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        raise CampaignError("invalid_" + field, message="%s is longer than %d characters." % (field, limit))
    return s


def clean_name(value):
    if not isinstance(value, str):
        raise CampaignError("invalid_name", message="name must be text.")
    s = _clean_text(value, NAME_MAX, "name")
    if len(s) < NAME_MIN:
        raise CampaignError("invalid_name", message="name must be at least %d characters." % NAME_MIN)
    return s


def clean_description(value):
    if not isinstance(value, str):
        raise CampaignError("invalid_description", message="description must be text.")
    return _clean_text(value, DESC_MAX, "description")


def clean_template(value):
    if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not isinstance(value.get("language"), str):
        raise CampaignError("invalid_template", message="template must be {\"name\", \"language\"}.")
    name, lang = value["name"].strip(), value["language"].strip()
    if not re.match(r"^[a-z0-9_]{1,512}$", name) or not re.match(r"^[A-Za-z]{2,3}(_[A-Za-z0-9]{2,4})?$", lang):
        raise CampaignError("invalid_template", message="Unknown template name or language format.")
    return {"name": name, "language": lang}


def clean_audience(value):
    """Shape check only - eligibility is computed at preview / validate time. -> {"merchants": {...}|None, "imports": [...]}"""
    if not isinstance(value, dict):
        raise CampaignError("invalid_audience", message="audience must be an object.")
    m = value.get("merchants")
    merchants = None
    if m is not None:
        if not isinstance(m, dict):
            raise CampaignError("invalid_audience", message="audience.merchants must be an object.")
        ids = m.get("ids", [])
        flt = m.get("filter")
        if not isinstance(ids, list) or not all(isinstance(i, str) and len(i) <= 64 for i in ids) or len(ids) > MAX_SELECTED_IDS:
            raise CampaignError("invalid_audience", message="merchants.ids must be a list of ids.")
        if flt is not None:
            if not isinstance(flt, dict):
                raise CampaignError("invalid_audience", message="merchants.filter must be an object.")
            flt = {k: str(flt.get(k) or "").strip()[:100] for k in ("q", "city", "area", "state")}
        merchants = {"ids": list(dict.fromkeys(ids)), "filter": flt}
    imports = value.get("imports", [])
    if not isinstance(imports, list) or not all(isinstance(i, str) and len(i) <= 64 for i in imports) or len(imports) > 50:
        raise CampaignError("invalid_audience", message="audience.imports must be a list of import ids.")
    return {"merchants": merchants, "imports": list(dict.fromkeys(imports))}


def clean_header_media(value):
    """Header media for templates with an IMAGE / VIDEO / DOCUMENT header: {"type": "image", "id"|"link"}. None clears."""
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("type") not in ("image", "video", "document"):
        raise CampaignError("invalid_header_media", message="header_media.type must be image, video or document.")
    media_id, link = str(value.get("id") or "").strip(), str(value.get("link") or "").strip()
    if bool(media_id) == bool(link):
        raise CampaignError("invalid_header_media", message="Provide exactly one of id or link.")
    if media_id:
        if not _MEDIA_ID_RE.match(media_id):
            raise CampaignError("invalid_header_media", message="id is not a valid WhatsApp media id.")
        return {"type": value["type"], "id": media_id}
    p = urlparse(link)
    if p.scheme != "https" or not p.hostname or p.username or p.password or len(link) > 2048:
        raise CampaignError("invalid_header_media", message="link must be a public https URL.")
    return {"type": value["type"], "link": link}


def parse_send_at(value) -> datetime:
    """ISO-8601 WITH an explicit UTC offset (or 'Z') -> naive UTC datetime. Enforces lead time and horizon."""
    if not isinstance(value, str) or not value.strip():
        raise CampaignError("invalid_send_at", message="send_at is required (ISO-8601 with a timezone offset).")
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise CampaignError("invalid_send_at", message="send_at is not a valid ISO-8601 date-time.")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise CampaignError("invalid_send_at", message="send_at must include a timezone offset, e.g. +05:30 or Z.")
    utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
    now = datetime.utcnow()
    if utc < now + MIN_SCHEDULE_LEAD:
        raise CampaignError("send_at_too_soon", message="send_at must be at least %d minutes in the future." % (MIN_SCHEDULE_LEAD.seconds // 60))
    if utc > now + MAX_SCHEDULE_AHEAD:
        raise CampaignError("send_at_too_far", message="send_at must be within %d days." % MAX_SCHEDULE_AHEAD.days)
    return utc


# ── CRUD ────────────────────────────────────────────────────────────────────────────────────────

def _oid(campaign_id):
    try:
        return ObjectId(str(campaign_id))
    except (InvalidId, TypeError):
        raise CampaignError("not_found", 404, "Campaign not found.")


def get(campaign_id) -> dict:
    doc = _db()[CAMPAIGNS].find_one({"_id": _oid(campaign_id)})
    if not doc:
        raise CampaignError("not_found", 404, "Campaign not found.")
    return doc


def create(name, description, admin) -> dict:
    now = datetime.utcnow()
    doc = {"name": clean_name(name), "description": clean_description(description or ""), "status": DRAFT, "version": 1,
           "template": None, "header_media": None, "audience": {"merchants": None, "imports": []}, "snapshot": None,
           "schedule": None, "created_at": now, "created_by": admin, "updated_at": now, "updated_by": admin}
    res = _db()[CAMPAIGNS].insert_one(doc)
    if not res.acknowledged:
        raise RuntimeError("campaign insert not acknowledged")
    doc["_id"] = res.inserted_id
    return doc


def _transition(doc, to, admin, extra_set=None, extra_unset=None) -> dict:
    """Atomic status change guarded by (status, version). Raises CampaignError(conflict) when someone else got there first."""
    if to not in TRANSITIONS.get(doc["status"], set()):
        raise CampaignError("invalid_transition", 409, "A %s campaign cannot become %s." % (doc["status"], to))
    now = datetime.utcnow()
    upd = {"$set": {"status": to, "updated_at": now, "updated_by": admin, **(extra_set or {})}, "$inc": {"version": 1}}
    if extra_unset:
        upd["$unset"] = {k: "" for k in extra_unset}
    res = _db()[CAMPAIGNS].update_one({"_id": doc["_id"], "status": doc["status"], "version": doc["version"]}, upd)
    if res.matched_count != 1:
        raise CampaignError("conflict", 409, "The campaign was changed by someone else. Reload and try again.")
    return _db()[CAMPAIGNS].find_one({"_id": doc["_id"]})


def update_draft(doc, changes: dict, expected_version, admin) -> dict:
    if doc["status"] != DRAFT:
        raise CampaignError("not_editable", 409, "Only a draft can be edited. Use revert-to-draft first.")
    if expected_version is not None and expected_version != doc["version"]:
        raise CampaignError("conflict", 409, "The campaign was changed by someone else. Reload and try again.")
    sets = {}
    if "name" in changes:
        sets["name"] = clean_name(changes["name"])
    if "description" in changes:
        sets["description"] = clean_description(changes["description"])
    if "template" in changes:
        tpl = clean_template(changes["template"]) if changes["template"] is not None else None
        sets["template"] = tpl
        if tpl != doc.get("template"):
            sets["header_media"] = None                       # media belongs to a template; never carry it across
    if "header_media" in changes:
        sets["header_media"] = clean_header_media(changes["header_media"])
    if "audience" in changes:
        sets["audience"] = clean_audience(changes["audience"])
    if not sets:
        raise CampaignError("nothing_to_update", 400, "No editable field was provided.")
    sets.update(updated_at=datetime.utcnow(), updated_by=admin)
    res = _db()[CAMPAIGNS].update_one({"_id": doc["_id"], "status": DRAFT, "version": doc["version"]},
                                      {"$set": sets, "$inc": {"version": 1}})
    if res.matched_count != 1:
        raise CampaignError("conflict", 409, "The campaign was changed by someone else. Reload and try again.")
    return _db()[CAMPAIGNS].find_one({"_id": doc["_id"]})


def drop_snapshot(campaign_oid) -> int:
    return _db()[RECIPIENTS].delete_many({"campaign_id": str(campaign_oid)}).deleted_count


def delete(doc) -> None:
    if doc["status"] not in (DRAFT, CANCELLED):
        raise CampaignError("not_deletable", 409, "Only a draft or cancelled campaign can be deleted.")
    res = _db()[CAMPAIGNS].delete_one({"_id": doc["_id"], "status": doc["status"], "version": doc["version"]})
    if res.deleted_count != 1:
        raise CampaignError("conflict", 409, "The campaign was changed by someone else. Reload and try again.")
    drop_snapshot(doc["_id"])


# ── template readiness ──────────────────────────────────────────────────────────────────────────

def load_template(tpl_ref):
    doc = _db()[T.TEMPLATES].find_one({"_id": T.template_id(tpl_ref["name"], tpl_ref["language"])})
    if not doc:
        raise CampaignError("template_not_found", 404, "Template not found. Sync templates first.")
    return doc


def template_problems(campaign) -> list:
    """Cache-based problems with the campaign's template / header media ([] = fine). Never calls Meta."""
    ref = campaign.get("template")
    if not ref:
        return ["template_not_selected"]
    try:
        tdoc = load_template(ref)
    except CampaignError:
        return ["template_not_found"]
    r = T.readiness(tdoc)
    problems = list(r["reasons"])
    parsed = tdoc.get("parsed") or {}
    if parsed.get("requires_header_media"):
        hm = campaign.get("header_media")
        want = ((parsed.get("header") or {}).get("format") or "").lower()
        if not hm:
            problems.append("header_media_required")
        elif hm.get("type") != want:
            problems.append("header_media_type_mismatch")
    elif campaign.get("header_media"):
        problems.append("header_media_not_needed")
    return problems


def _components_for(tdoc, values, header_media):
    return T.build_send_components(tdoc.get("parsed") or {}, values, header_media)


# ── preview ─────────────────────────────────────────────────────────────────────────────────────

def preview_message(campaign, sample=None) -> dict:
    """Render the campaign template for a SAMPLE recipient (defaults are obviously fake). Sends nothing."""
    ref = campaign.get("template")
    if not ref:
        raise CampaignError("template_not_selected", 409, "Select a template first.")
    tdoc = load_template(ref)
    ctx = {**SAMPLE_RECIPIENT, **{k: v for k, v in (sample or {}).items() if k in SAMPLE_RECIPIENT and isinstance(v, str)}}
    cu = tdoc.get("campaign_use") or {}
    values = T.resolve_values(cu, ctx)
    out = T.render_preview(tdoc.get("parsed") or {}, values, use_samples=False)
    out.update({"template": ref, "sample_recipient": ctx, "resolved_values": values,
                "template_problems": template_problems(campaign), "sent": False})
    return out


def audience_preview(campaign, admin, page=1, page_size=50, status="all") -> dict:
    aud = campaign.get("audience") or {}
    if not aud.get("merchants") and not aud.get("imports"):
        raise CampaignError("empty_audience", 409, "Select merchants and/or imports first.")
    try:
        return AU.build_preview(aud.get("merchants"), aud.get("imports"), admin, page, page_size, status)
    except AU.AudienceError as e:
        raise CampaignError(e.code, 404 if e.code == "import_not_found" else 400, e.message)


# ── validate + snapshot ─────────────────────────────────────────────────────────────────────────

def validate_and_snapshot(campaign, admin) -> dict:
    """draft -> ready. Returns the validation report. On any blocking problem the campaign stays draft and no snapshot
    is kept. Fails closed: if the consent lookup fails nobody is eligible and nothing is snapshotted."""
    if campaign["status"] != DRAFT:
        raise CampaignError("not_draft", 409, "Only a draft can be validated.")
    errors = []
    tproblems = template_problems(campaign)
    errors += ["template:" + p for p in tproblems]
    aud = campaign.get("audience") or {}
    if not aud.get("merchants") and not aud.get("imports"):
        errors.append("audience:empty")
    if errors:
        return {"ok": False, "errors": errors}

    tdoc = load_template(campaign["template"])
    cu = tdoc.get("campaign_use") or {}
    collected = []
    try:
        pv = AU.build_preview(aud.get("merchants"), aud.get("imports"), admin, 1, 1, "all", collect=collected)
    except AU.AudienceError as e:
        return {"ok": False, "errors": ["audience:" + e.code], "message": e.message}
    summary = pv["summary"]
    if not summary["consent_check_ok"]:
        return {"ok": False, "errors": ["consent_check_failed"], "message": "Consent could not be verified; nothing was snapshotted."}

    by_reason = dict(summary["by_reason"])
    keep, invalid_values = [], 0
    for r in collected:
        if not r["eligible"]:
            continue
        values = T.resolve_values(cu, r["ctx"])
        try:
            _components_for(tdoc, values, campaign.get("header_media"))
        except T.ComponentError:
            invalid_values += 1
            continue
        keep.append((r, values))
    if invalid_values:
        by_reason["invalid_variable_values"] = invalid_values
    if not keep:
        return {"ok": False, "errors": ["no_eligible_recipients"], "summary": {**summary, "by_reason": by_reason}}

    snap_id = str(ObjectId())
    now = datetime.utcnow()
    try:
        for i in range(0, len(keep), INSERT_BATCH):
            _db()[RECIPIENTS].insert_many([
                {"campaign_id": str(campaign["_id"]), "snapshot_id": snap_id, "phone": r["canonical"], "source": r["source"],
                 "ref": r["ref"], "consent_basis": r["basis"], "values": values, "state": "snapshot", "created_at": now}
                for r, values in keep[i:i + INSERT_BATCH]], ordered=True)
    except Exception as e:
        logger.error("[WA-Campaign] snapshot write failed (%s)", exc_summary(e))
        _db()[RECIPIENTS].delete_many({"snapshot_id": snap_id})
        raise
    total = summary["total"]
    snap = {"id": snap_id, "at": now, "total": total, "eligible": len(keep), "excluded": total - len(keep),
            "by_reason": by_reason, "by_source": summary["by_source"], "content_hash": tdoc.get("content_hash"),
            "mapped_hash": cu.get("mapped_hash")}
    try:
        _db()[RECIPIENTS].delete_many({"campaign_id": str(campaign["_id"]), "snapshot_id": {"$ne": snap_id}})
        _transition(campaign, READY, admin, {"snapshot": snap})
    except Exception:
        _db()[RECIPIENTS].delete_many({"snapshot_id": snap_id})
        raise
    shown = {k: v for k, v in snap.items() if k != "at"}
    shown["at"] = now.isoformat() + "Z"
    return {"ok": True, "errors": [], "snapshot": shown}


def list_snapshot(campaign, page=1, page_size=50) -> dict:
    page_size = max(1, min(int(page_size or 50), 200))
    snap = campaign.get("snapshot") or {}
    q = {"campaign_id": str(campaign["_id"]), "snapshot_id": snap.get("id")}
    total = _db()[RECIPIENTS].count_documents(q)
    pages = max(1, -(-total // page_size))
    page = min(max(1, int(page or 1)), pages)
    rows = _db()[RECIPIENTS].find(q).sort("_id", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [{"phone_masked": mask_phone(r["phone"]), "source": r["source"], "ref": r["ref"],
                       "consent_basis": r["consent_basis"], "state": r["state"]} for r in rows],
            "total": total, "page": page, "page_size": page_size, "pages": pages}


# ── confirmation / schedule ─────────────────────────────────────────────────────────────────────

def confirmation_report(campaign) -> dict:
    """What the admin must read before confirming. Pure read; no Meta call."""
    snap = campaign.get("snapshot") or {}
    warnings = []
    ok_sending, why = ST.sending_allowed()
    if not ok_sending:
        warnings.append({"code": "sending_disabled", "message": "Sending is currently DISABLED (%s). A scheduled campaign will "
                         "not run until the global switch is enabled." % why})
    warnings.append({"code": "no_worker_yet", "message": "In this release scheduling only records the time; no process sends "
                     "anything yet."})
    warnings.append({"code": "consent_rechecked", "message": "Consent and STOP status are re-checked for every recipient at "
                     "send time; people who opted out after the snapshot will be skipped."})
    if snap:
        if snap.get("excluded"):
            warnings.append({"code": "recipients_excluded", "message": "%d of %d selected recipients are excluded (%s)." % (
                snap["excluded"], snap["total"], ", ".join("%s: %s" % kv for kv in sorted(snap.get("by_reason", {}).items())))})
        age = datetime.utcnow() - snap["at"]
        if age > SNAPSHOT_MAX_AGE:
            warnings.append({"code": "snapshot_stale", "message": "The recipient snapshot is older than %d hours. Re-validate "
                             "before scheduling." % (SNAPSHOT_MAX_AGE.total_seconds() // 3600)})
        tested = _db()[TEST_SENDS].count_documents({"campaign_id": str(campaign["_id"]), "status": "sent",
                                                    "created_at": {"$gte": snap["at"]}})
        if not tested:
            warnings.append({"code": "no_test_send", "message": "No successful test message has been sent for this version."})
    return {"status": campaign["status"], "eligible_count": snap.get("eligible"), "excluded_count": snap.get("excluded"),
            "template": campaign.get("template"), "warnings": warnings,
            "required_confirmation": {"eligible_count": snap.get("eligible"), "acknowledge": True,
                                      "template": "%s/%s" % (campaign["template"]["name"], campaign["template"]["language"])
                                      if campaign.get("template") else None}}


def check_schedulable(campaign, confirm) -> None:
    """Everything that must hold to schedule, EXCEPT the live Meta check (done by the caller so it can be audited)."""
    if campaign["status"] != READY:
        raise CampaignError("not_ready", 409, "Only a ready (validated) campaign can be scheduled.")
    snap = campaign.get("snapshot") or {}
    if not snap or not snap.get("eligible"):
        raise CampaignError("no_snapshot", 409, "Validate the campaign first.")
    if datetime.utcnow() - snap["at"] > SNAPSHOT_MAX_AGE:
        raise CampaignError("snapshot_stale", 409, "The recipient snapshot is too old. Revert to draft and validate again.")
    if not isinstance(confirm, dict) or confirm.get("acknowledge") is not True:
        raise CampaignError("confirmation_required", 400, "confirm.acknowledge must be true.")
    if confirm.get("eligible_count") != snap["eligible"]:
        raise CampaignError("confirmation_mismatch", 409, "confirm.eligible_count does not match the snapshot (%s)." % snap["eligible"],
                            expected=snap["eligible"])
    want = "%s/%s" % (campaign["template"]["name"], campaign["template"]["language"])
    if confirm.get("template") != want:
        raise CampaignError("confirmation_mismatch", 409, "confirm.template must be '%s'." % want)
    tdoc = load_template(campaign["template"])
    # the snapshot's values were built from THIS mapping of THIS template content; if either changed, re-validate
    if tdoc.get("content_hash") != snap.get("content_hash") or (tdoc.get("campaign_use") or {}).get("mapped_hash") != snap.get("mapped_hash"):
        raise CampaignError("template_changed", 409, "The template or its mapping changed after validation. Revert to draft and validate again.")
    problems = [p for p in template_problems(campaign)]
    if problems:
        raise CampaignError("template_not_ready", 409, "The template is not ready.", reasons=problems)


def schedule(campaign, send_at_utc, admin) -> dict:
    return _transition(campaign, SCHEDULED, admin, {"schedule": {"send_at": send_at_utc, "mode": "scheduled",
                                                                 "confirmed_by": admin, "confirmed_at": datetime.utcnow()}})


def unschedule(campaign, admin) -> dict:
    return _transition(campaign, READY, admin, extra_unset=["schedule"])


def revert_to_draft(campaign, admin) -> dict:
    if campaign["status"] != READY:
        raise CampaignError("invalid_transition", 409, "Only a ready campaign can be reverted (unschedule a scheduled one first).")
    out = _transition(campaign, DRAFT, admin, extra_unset=["snapshot", "schedule"])
    drop_snapshot(campaign["_id"])
    return out


def cancel(campaign, admin) -> dict:
    out = _transition(campaign, CANCELLED, admin, {"cancelled_at": datetime.utcnow(), "cancelled_by": admin})
    drop_snapshot(campaign["_id"])
    return out


# ── TEST send (the only code path in this module that can contact WhatsApp) ──────────────────────

def _phone_hash(p):
    return hashlib.sha256(p.encode()).hexdigest()


def _claim_test_send(campaign_id, phone_hash, last4, admin_id):
    """ATOMIC claim of the right to send a test to this number for this campaign (the cooldown IS the claim).
    One upsert on a document whose _id is (campaign, number): it only matches when `next_allowed_at` has passed; while a
    claim is live the upsert cannot match, tries to insert the same _id and gets DuplicateKeyError - so of any number of
    simultaneous requests (any replica) exactly one wins. There is no separate check-then-write step to race.
    -> (lock_id, token). Raises CampaignError cooldown (429) or claim_unavailable (503, fail closed)."""
    now = datetime.utcnow()
    token = str(ObjectId())
    lock_id = "%s:%s" % (campaign_id, phone_hash)
    try:
        _db()[TEST_LOCKS].update_one(
            {"_id": lock_id, "next_allowed_at": {"$lte": now}},
            {"$set": {"next_allowed_at": now + TEST_COOLDOWN, "claim_token": token, "claimed_at": now,
                      "campaign_id": campaign_id, "phone_last4": last4, "admin_id": admin_id}},
            upsert=True)
    except DuplicateKeyError:
        raise CampaignError("cooldown", 429, "A test for this number and campaign is in progress or was just sent. Wait a minute.")
    except Exception as e:
        logger.error("[WA-Campaign] test-send claim failed (%s) - nothing sent", exc_summary(e))
        raise CampaignError("claim_unavailable", 503, "Could not reserve the test send - nothing was sent.")
    return lock_id, token


def _release_test_claim(lock_id, token) -> None:
    """Free a claim when NOTHING was sent (a gate refused, or a step before the send failed). Only the holder's own token
    can release it, so a late release can never free someone else's newer claim."""
    try:
        _db()[TEST_LOCKS].update_one({"_id": lock_id, "claim_token": token},
                                     {"$set": {"next_allowed_at": datetime.utcnow() - timedelta(seconds=1)}})
    except Exception as e:                    # worst case the cooldown simply runs out by itself
        logger.error("[WA-Campaign] test-send claim release failed (%s)", exc_summary(e))


def _hold_test_claim(lock_id, token, hold) -> None:
    try:
        _db()[TEST_LOCKS].update_one({"_id": lock_id, "claim_token": token},
                                     {"$set": {"next_allowed_at": datetime.utcnow() + hold}})
    except Exception as e:
        logger.error("[WA-Campaign] test-send claim hold failed (%s)", exc_summary(e))


def _over_test_limits(admin_id, now, own=0) -> bool:
    """True if this admin / the whole system has used up the hourly test-send budget. Blocked attempts never count.
    `own` = number of this request's own records already counted (1 for the re-check made after the record is written)."""
    coll = _db()[TEST_SENDS]
    since = {"$gte": now - timedelta(hours=1)}
    return coll.count_documents({"admin_id": admin_id, "created_at": since, "status": {"$ne": "blocked"}}) >= TEST_MAX_PER_ADMIN_HOUR + own \
        or coll.count_documents({"created_at": since, "status": {"$ne": "blocked"}}) >= TEST_MAX_GLOBAL_HOUR + own


def send_test_message(campaign, phone, admin, confirm, dry_run=False, sample=None) -> dict:
    """One template message to ONE allow-listed, consenting number. See the module docstring for the gate order."""
    if confirm is not True:
        raise CampaignError("confirmation_required", 400, "confirm_test_send must be true.")
    if campaign["status"] in (CANCELLED, COMPLETED):
        raise CampaignError("campaign_closed", 409, "This campaign is %s." % campaign["status"])
    problems = template_problems(campaign)
    if problems:
        raise CampaignError("template_not_ready", 409, "The template is not ready.", reasons=problems)
    canonical = C.normalize_phone(phone)
    if not canonical:
        raise CampaignError("invalid_phone", 400, "Not a valid phone number (international numbers need +CC).")
    admin_id = (admin or {}).get("id", "")
    ref = audit.phone_ref(canonical)

    def blocked(code, http, message, audit_details=None, **extra):
        audit.record("campaign.test_send", admin, outcome="blocked:" + code, campaign_id=campaign["_id"], target=ref,
                     details=audit_details)
        return CampaignError(code, http, message, **extra)

    allowed, why = ST.sending_allowed()                                        # Phase 2A kill switch (never bypassed)
    if not allowed:
        raise blocked("sending_disabled", 409, "Sending is disabled (%s)." % why, reason=why)
    env_ok, env_why = ST.environment_gate()                                    # Production / unknown environment: no test send either
    if not env_ok:
        raise blocked(env_why, 409, "Test sending is not allowed in this environment (%s)." % env_why, reason=env_why)
    if not ST.is_test_number(canonical):                                      # Phase 2A allow-list; empty = nobody
        raise blocked("not_a_test_number", 403, "This number is not on the test-number allow-list.")
    el = E.check(canonical)                                                    # Phase 2A eligibility: consent / STOP, fail closed
    if not el.eligible:
        raise blocked("recipient_not_eligible", 409, "This number cannot be messaged (%s)." % el.reason, reason=el.reason)

    now = datetime.utcnow()
    lock = None
    if not dry_run:
        try:
            if _over_test_limits(admin_id, now):
                raise blocked("rate_limited", 429, "Test-send limit reached. Try again later.")
        except CampaignError:
            raise
        except Exception as e:
            logger.error("[WA-Campaign] test-send limit check failed (%s) - nothing sent", exc_summary(e))
            raise CampaignError("claim_unavailable", 503, "Could not check the test-send limits - nothing was sent.")
        try:
            lock = _claim_test_send(str(campaign["_id"]), _phone_hash(canonical), canonical[-4:], admin_id)   # atomic: check + claim in one step
        except CampaignError as e:
            audit.record("campaign.test_send", admin, outcome="blocked:" + e.code, campaign_id=campaign["_id"], target=ref)
            raise

    # From here until the send, ANY exit means nothing was sent: the claim is released so a retry is possible.
    # Once the sender has been called the claim is NEVER released (the message may be on its way).
    state = {"sending": False}
    try:
        return _send_claimed_test(campaign, canonical, admin, ref, dry_run, sample, el, lock, now, blocked, state)
    except BaseException:
        if lock and not state["sending"]:
            _release_test_claim(*lock)
        raise


def _send_claimed_test(campaign, canonical, admin, ref, dry_run, sample, el, lock, now, blocked, state) -> dict:
    admin_id = (admin or {}).get("id", "")
    tdoc = load_template(campaign["template"])
    try:                                                                       # LIVE re-verification, fail closed
        live = T.verify_live(tdoc)
    except T.TemplateSyncError as e:
        raise blocked("template_unverified", 502, e.message, meta_code=e.meta_code, template_error=e.code)
    if live:
        raise blocked("template_not_ready", 409, "The template is no longer usable at Meta.", reasons=live)

    ctx = {**SAMPLE_RECIPIENT, **{k: v for k, v in (sample or {}).items() if k in SAMPLE_RECIPIENT and isinstance(v, str)}}
    values = T.resolve_values(tdoc.get("campaign_use") or {}, ctx)
    try:
        components = _components_for(tdoc, values, campaign.get("header_media"))
    except T.ComponentError as e:
        raise blocked("payload_invalid", 409, "The template values / header media are incomplete (%s)." % e)

    if dry_run:
        return {"ok": True, "dry_run": True, "sent": False, "to_last4": canonical[-4:], "template": campaign["template"],
                "components": components, "consent_basis": el.basis}

    record = {"campaign_id": str(campaign["_id"]), "admin_id": admin_id, "phone_hash": _phone_hash(canonical),
              "phone_last4": canonical[-4:], "status": "attempting", "created_at": now, "claim_token": lock[1]}
    try:
        audit.record("campaign.test_send", admin, outcome="attempt", campaign_id=campaign["_id"], target=ref, strict=True)
        claim_id = _db()[TEST_SENDS].insert_one(record).inserted_id
    except Exception as e:
        logger.error("[WA-Campaign] test-send claim failed (%s) - nothing sent", exc_summary(e))
        raise CampaignError("audit_unavailable", 503, "Could not record the attempt - nothing was sent.")

    def refuse(code, http, message, reason, **extra):
        """A check failed AFTER the attempt was recorded: mark it blocked (it no longer counts), audit it, send nothing."""
        try:
            _db()[TEST_SENDS].update_one({"_id": claim_id}, {"$set": {"status": "blocked", "outcome": "blocked:" + code}})
        except Exception as e:
            logger.error("[WA-Campaign] blocked test-send not saved (%s)", exc_summary(e))
        return blocked(code, http, message, audit_details={"reason": reason, "stage": "final_check"}, **extra)

    try:                                                                       # budget re-check AFTER our own record exists
        over = _over_test_limits(admin_id, now, own=1)
    except Exception as e:
        logger.error("[WA-Campaign] test-send limit re-check failed (%s) - nothing sent", exc_summary(e))
        raise refuse("claim_unavailable", 503, "Could not check the test-send limits - nothing was sent.", "limit_check_failed")
    if over:
        raise refuse("rate_limited", 429, "Test-send limit reached. Try again later.", "limits")

    # FINAL consent / STOP re-read: the last thing before the WhatsApp call. Nothing from the earlier eligibility check
    # (or from any preview) is trusted: STOP and every lookup failure block the send (wa_eligibility fails closed).
    try:
        final = E.check(canonical)
    except Exception as e:                                   # defence in depth: even an unexpected error blocks the send
        logger.error("[WA-Campaign] final consent check failed (%s) - nothing sent", exc_summary(e))
        raise refuse("recipient_not_eligible", 409, "Consent could not be re-checked. Nothing was sent.",
                     "consent_lookup_failed")
    if not final.eligible:
        raise refuse("recipient_not_eligible", 409, "Consent changed before the send (%s). Nothing was sent." % final.reason,
                     final.reason)
    # FINAL kill-switch re-read (environment AND database switch), the very last gate before the WhatsApp call. Either one off,
    # an unreadable switch, or any error blocks the send; the claim is released by the caller because nothing was sent.
    try:
        allowed, why = ST.sending_allowed()                  # fails closed itself; the except is defence in depth
    except Exception as e:
        logger.error("[WA-Campaign] final kill-switch check failed (%s) - nothing sent", exc_summary(e))
        allowed, why = False, "settings_unavailable"
    if not allowed:
        raise refuse("sending_disabled", 409, "Sending was disabled before the send (%s). Nothing was sent." % why, why)
    state["sending"] = True
    res = WA.send_whatsapp_template_checked(canonical, campaign["template"]["name"], campaign["template"]["language"], components)
    outcome = res.get("outcome", "uncertain")
    status = {"sent": "sent", "uncertain": "uncertain"}.get(outcome, "failed")
    if outcome == "uncertain":
        _hold_test_claim(lock[0], lock[1], UNCERTAIN_HOLD)         # unknown delivery: no repeat until someone has checked
    try:
        _db()[TEST_SENDS].update_one({"_id": claim_id}, {"$set": {"status": status, "outcome": outcome,
                                                                  "message_id": res.get("message_id", ""), "error_code": res.get("error_code")}})
    except Exception as e:
        logger.error("[WA-Campaign] test-send result not saved (%s)", exc_summary(e))
    audit.record("campaign.test_send", admin, outcome=outcome, campaign_id=campaign["_id"], target=ref,
                 details={"error_code": res.get("error_code")})
    return {"ok": bool(res.get("ok")), "sent": bool(res.get("ok")), "outcome": outcome, "message_id": res.get("message_id", ""),
            "error_code": res.get("error_code"), "to_last4": canonical[-4:],
            "warning": "Delivery outcome is UNKNOWN; do not resend automatically. Check the phone / Meta." if outcome == "uncertain" else ""}
