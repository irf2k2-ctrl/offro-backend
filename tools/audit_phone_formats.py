"""READ-ONLY audit of how phone numbers are stored (Phase C). It never writes, updates, deletes or creates anything.

    python tools/audit_phone_formats.py            # reads MONGO_URL like the app does, prints a text report
    python tools/audit_phone_formats.py --json     # same report as JSON

Run it deliberately, against the database you mean (staging first). It prints COUNTS and MASKED samples only (country code and
the last four digits), never a full number, name or id.

What it reports, per collection and field:
  * how numbers are stored (bare 10 digits, 91 + 10, +91 + 10, 0 + 10, +E.164 of another supported country, digits-only E.164, other)
  * Indian-shaped numbers that fail modern validation (these still need the legacy lookup so those accounts can log in)
  * duplicates: the same number (after canonicalising) on more than one record in the SAME collection
  * overlap between collections (expected for accounts/users/merchants, which mirror each other; reported as counts)
  * look-alike pairs: two different countries holding the same national digits (the collision the old last-10-digits logic caused)
Nothing is merged or changed. Decisions about duplicates are for an administrator.
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import phone as P  # noqa: E402

# (collection, field). Fields that do not exist on a collection are simply counted as empty.
TARGETS = [
    ("accounts", "phone"),
    ("users", "phone"),
    ("merchants", "phone"),
    ("influencers", "phone"),
    ("stores", "merchant_phone"),
    ("wa_contacts", "phone"),
    ("customers_whatsapp", "phone_number"),
]


class _ReadOnly:
    """Hands out collections that can only find() and count_documents(). Any write attempt raises."""

    class _Coll:
        def __init__(self, coll):
            self._c = coll

        def find(self, *a, **k):
            return self._c.find(*a, **k)

        def count_documents(self, *a, **k):
            return self._c.count_documents(*a, **k)

        def __getattr__(self, name):
            raise PermissionError("audit is read-only: %s is not allowed" % name)

    def __init__(self, db):
        self._db = db

    def __getitem__(self, name):
        return _ReadOnly._Coll(self._db[name])


def mask(e164_or_raw) -> str:
    s = str(e164_or_raw or "")
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) <= 4:
        return "••••"
    prefix = "+" if s.strip().startswith("+") else ""
    return prefix + "•" * (len(digits) - 4) + digits[-4:]


def classify(stored) -> dict:
    """How one stored value looks. Returns {'class', 'canonical', 'country', 'national'}.

    canonical is the E.164 value this record would have under the new rules. Old Indian shapes (bare 10, 91+10, 0+10) are assumed to
    be Indian because that is how every existing record was written; digits-only numbers that start with another supported calling code are
    read as that country (WhatsApp style)."""
    raw = str(stored if stored is not None else "").strip()
    if not raw:
        return {"class": "empty", "canonical": "", "country": "", "national": ""}
    s = raw.replace(" ", "").replace("-", "")
    if s.startswith("+"):
        r = P.parse(s)
        if r.ok:
            return {"class": "plus_e164_" + r.country, "canonical": r.e164, "country": r.country, "national": r.national}
        return {"class": "plus_invalid", "canonical": "", "country": "", "national": ""}
    if not s.isascii() or not s.isdigit():
        return {"class": "non_numeric", "canonical": "", "country": "", "national": ""}
    # Indian legacy shapes first: this is how Indian accounts were written.
    n = None
    cls = None
    if len(s) == 10:
        n, cls = s, "bare_10"
    elif len(s) == 12 and s.startswith("91"):
        n, cls = s[2:], "cc91_plus_10"
    elif len(s) == 11 and s.startswith("0"):
        n, cls = s[1:], "zero_plus_10"
    if n is not None:
        r = P.parse("+91" + n)
        if r.ok:
            return {"class": cls, "canonical": r.e164, "country": "IN", "national": n}
        return {"class": cls + "_not_valid_indian_mobile", "canonical": "+91" + n, "country": "IN", "national": n}
    r = P.parse("+" + s)                                   # digits-only E.164 of another supported country
    if r.ok:
        return {"class": "digits_e164_" + r.country, "canonical": r.e164, "country": r.country, "national": r.national}
    return {"class": "other", "canonical": "", "country": "", "national": ""}


def audit(db, targets=TARGETS, samples=3) -> dict:
    ro = db if isinstance(db, _ReadOnly) else _ReadOnly(db)
    report = {"read_only": True, "collections": {}, "overlap": {}, "lookalikes": {}}
    owners = {}                                              # (collection) -> {canonical: count}
    nat_countries = defaultdict(lambda: defaultdict(set))    # national -> country -> {collection}
    for coll, field in targets:
        formats, sample_by_class = Counter(), defaultdict(list)
        canon = Counter()
        total = 0
        for doc in ro[coll].find({}, {field: 1}):
            total += 1
            c = classify(doc.get(field))
            formats[c["class"]] += 1
            if len(sample_by_class[c["class"]]) < samples and c["class"] != "empty":
                sample_by_class[c["class"]].append(mask(doc.get(field)))
            if c["canonical"]:
                canon[c["canonical"]] += 1
                nat_countries[c["national"]][c["country"]].add(coll)
        owners[coll] = canon
        dup_numbers = {k: v for k, v in canon.items() if v > 1}
        indian_invalid = sum(v for k, v in formats.items() if k.endswith("_not_valid_indian_mobile"))
        report["collections"][coll] = {
            "field": field,
            "records": total,
            "formats": dict(sorted(formats.items())),
            "masked_samples": {k: v for k, v in sorted(sample_by_class.items())},
            "indian_shaped_but_not_valid_mobile": indian_invalid,
            "duplicate_numbers_within_collection": len(dup_numbers),
            "records_in_duplicate_groups": sum(dup_numbers.values()),
            "duplicate_masked_samples": [mask(k) for k in list(dup_numbers)[:samples]],
        }
    names = [c for c, _ in targets]
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            both = set(owners[a]) & set(owners[b])
            if both:
                report["overlap"]["%s & %s" % (a, b)] = len(both)
    look = {k: v for k, v in nat_countries.items() if len(v) > 1}
    report["lookalikes"] = {
        "pairs_of_different_countries_with_same_national_digits": len(look),
        "masked_samples": [mask(k) for k in list(look)[:samples]],
    }
    return report


def render_text(rep) -> str:
    out = ["PHONE FORMAT AUDIT (read-only: nothing was changed)", ""]
    for coll, d in rep["collections"].items():
        out.append("%s.%s  records: %d" % (coll, d["field"], d["records"]))
        for k, v in d["formats"].items():
            ex = ", ".join(d["masked_samples"].get(k, []))
            out.append("    %-40s %6d   %s" % (k, v, ("e.g. " + ex) if ex else ""))
        out.append("    Indian-shaped but not valid mobile: %d" % d["indian_shaped_but_not_valid_mobile"])
        out.append("    duplicate numbers in this collection: %d (records involved: %d) %s" % (
            d["duplicate_numbers_within_collection"], d["records_in_duplicate_groups"],
            ("e.g. " + ", ".join(d["duplicate_masked_samples"])) if d["duplicate_masked_samples"] else ""))
        out.append("")
    out.append("Overlap between collections (numbers present in both; expected for mirrored collections):")
    out += ["    %s: %d" % (k, v) for k, v in rep["overlap"].items()] or ["    none"]
    lk = rep["lookalikes"]
    out.append("")
    out.append("Look-alike pairs (same national digits in two different countries): %d %s" % (
        lk["pairs_of_different_countries_with_same_national_digits"],
        ("e.g. " + ", ".join(lk["masked_samples"])) if lk["masked_samples"] else ""))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only audit of stored phone number formats.")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--samples", type=int, default=3, help="masked samples per class (default 3)")
    a = ap.parse_args(argv)
    from database import db                                  # reads MONGO_URL like the app
    rep = audit(_ReadOnly(db), samples=a.samples)
    print(json.dumps(rep, indent=2, ensure_ascii=False) if a.json else render_text(rep))


if __name__ == "__main__":
    main()
