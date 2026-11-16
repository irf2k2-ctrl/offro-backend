"""
services/wa_eligibility.py
--------------------------
The ONE place that decides whether a phone number may receive a WhatsApp MARKETING / campaign message
(Phase 2A). It reuses the Phase 1 consent store (wa_contacts) - there is no second consent mechanism - and every
future campaign path (audience preview, validation, test send, worker) must call it.

Eligible ONLY when ALL hold:
    * the number normalizes to a valid canonical number;
    * no wa_contacts record under ANY storage variant of the number is opted_out (an opt-out always wins);
    * the contact is opted_in AND the stored evidence is recognisable and complete:
        - customer START  : last_consent.source == "whatsapp_keyword" with a WhatsApp message id, or
        - admin evidence  : last_consent.source == "admin_recorded" with source, evidence reference, consent time,
                            attestation and the administrator's identity.
Unknown consent (no record), invalid numbers, opted-out numbers, opted-in records without evidence, and ANY database
error are NOT eligible. Nothing here raises for a data problem; a lookup failure is reported as
reason == "consent_lookup_failed" (never eligible) so callers fail closed.

Callers must call this again IMMEDIATELY before each send - eligibility at audience-build time is not enough.
"""
import logging
from dataclasses import dataclass

from services import wa_consent as C
from services.wa_webhook_security import exc_summary

logger = logging.getLogger("offro.wa_campaigns.eligibility")

# reasons
ELIGIBLE = "eligible"
INVALID_NUMBER = "invalid_number"
OPTED_OUT = "opted_out"
UNKNOWN_CONSENT = "unknown_consent"
EVIDENCE_MISSING = "consent_evidence_missing"
LOOKUP_FAILED = "consent_lookup_failed"

BASIS_CUSTOMER_START = "customer_start"
BASIS_ADMIN_RECORDED = "admin_recorded"

_BATCH = 500


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reason: str
    canonical: str = ""          # canonical number ('' if invalid) - for server-side use; never log it
    basis: str = ""              # customer_start | admin_recorded when eligible

    @property
    def phone_last4(self) -> str:
        return self.canonical[-4:] if self.canonical else ""


def _evidence_basis(contact: dict) -> str:
    """'' if the opted_in record has no recognisable, complete evidence."""
    lc = contact.get("last_consent")
    if not isinstance(lc, dict):
        return ""
    if lc.get("source") == "whatsapp_keyword":
        return BASIS_CUSTOMER_START if str(lc.get("message_id", "")).strip() else ""
    if lc.get("source") == "admin_recorded":
        needed = ("consent_source", "evidence_ref", "consented_at", "admin_id")
        if lc.get("attested") is True and all(str(lc.get(k, "")).strip() for k in needed):
            return BASIS_ADMIN_RECORDED
    return ""


def _decide(raw, contacts_by_id: dict) -> Eligibility:
    keys = C.phone_lookup_keys(raw)
    if not keys:
        return Eligibility(False, INVALID_NUMBER)
    canonical = keys[0]
    found = [contacts_by_id[k] for k in keys if k in contacts_by_id]
    if any(d.get("status") == "opted_out" for d in found):        # an opt-out under ANY variant wins
        return Eligibility(False, OPTED_OUT, canonical)
    opted_in = [d for d in found if d.get("status") == "opted_in"]
    if not opted_in:
        return Eligibility(False, UNKNOWN_CONSENT, canonical)
    for d in opted_in:
        basis = _evidence_basis(d)
        if basis:
            return Eligibility(True, ELIGIBLE, canonical, basis)
    return Eligibility(False, EVIDENCE_MISSING, canonical)


def check_many(phones) -> list:
    """One Eligibility per input, same order. Duplicates are fine (each gets its own result)."""
    phones = list(phones)
    key_sets = [C.phone_lookup_keys(p) for p in phones]
    all_keys = sorted({k for ks in key_sets for k in ks})
    contacts = {}
    try:
        for i in range(0, len(all_keys), _BATCH):
            chunk = all_keys[i:i + _BATCH]
            for d in C.db[C.CONTACTS].find({"_id": {"$in": chunk}}):
                contacts[d["_id"]] = d
    except Exception as e:
        logger.error("[WA-Campaign] consent lookup failed (%s) - nobody is eligible", exc_summary(e))
        return [Eligibility(False, INVALID_NUMBER) if not ks else Eligibility(False, LOOKUP_FAILED, ks[0])
                for ks in key_sets]
    return [_decide(p, contacts) for p in phones]


def check(phone) -> Eligibility:
    return check_many([phone])[0]
