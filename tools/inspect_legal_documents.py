"""READ-ONLY report of the legal / information documents this environment's database holds (Stage 2).

Connects only to the database the server itself would use (MONGO_URL of the environment you run it in), performs only reads, and
prints for every document type: where its English text lives, whether it has text, how long it is, and how many translations exist.
Nothing is written, created, migrated or deleted. Run it separately in staging and in production to compare; it never talks to both.

Usage:  python tools/inspect_legal_documents.py            # human readable
        python tools/inspect_legal_documents.py --json     # machine readable
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def inspect() -> dict:
    from database import db
    from services import legal_documents as LD
    from services import translations as T
    defs = LD.definitions()
    rows = []
    for slug, d in defs.items():
        text = LD.english_text(slug, defs)
        key = LD.key_for(slug)
        rows.append({"slug": slug, "title": d[1], "audience": d[2], "stored_in": "legal_documents" if d[3] is None else d[3],
                     "selector": d[4], "has_english": bool(text.strip()), "english_length": len(text),
                     "translations": {"working": db[T.VALUES].count_documents({"key": key}),
                                      "published_languages": sorted({v["lang"] for v in db[T.VERSIONS].find({"track": T.DOCUMENT_TRACK}) if key in v.get("strings", {})})}})
    other = {}
    for coll, field in (("policies", "type"), ("terms", "type")):
        other[coll] = sorted({str(x.get(field)) for x in db[coll].find({}, {field: 1}) if x.get(field)})
    other["settings_keys_that_look_like_documents"] = sorted(
        {str(x.get("key")) for x in db.settings.find({"key": {"$regex": "about|terms|policy|faq|contact|disclaimer|how", "$options": "i"}}, {"key": 1}) if x.get("key")})
    return {"environment": T.environment(), "documents": rows, "other_stored_types": other}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rep = inspect()
    if args.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False, default=str))
        return 0
    print(f"Environment: {rep['environment']}   (read-only report)")
    print(f"{'slug':22} {'stored in':16} {'English':8} {'chars':>7}  translations (working / published languages)")
    for r in rep["documents"]:
        print(f"{r['slug']:22} {r['stored_in']:16} {'yes' if r['has_english'] else 'no':8} {r['english_length']:>7}  "
              f"{r['translations']['working']} / {','.join(r['translations']['published_languages']) or '-'}")
    print("Other stored types:", json.dumps(rep["other_stored_types"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
