"""Targeted tests for tools/audit_phone_formats.py (Phase C): classification, duplicates, look-alikes, masking, and read-only guarantee."""
import json

import pytest

from tools import audit_phone_formats as A


def _db():
    import sys
    return sys.modules["database"].db


def test_classify_the_legacy_and_international_shapes():
    c = A.classify
    assert c("9876543210")["class"] == "bare_10" and c("9876543210")["canonical"] == "+919876543210"
    assert c("919876543210")["class"] == "cc91_plus_10"
    assert c("09876543210")["class"] == "zero_plus_10"
    assert c("+919876543210") == {"class": "plus_e164_IN", "canonical": "+919876543210", "country": "IN", "national": "9876543210"}
    assert c("+966512345678")["class"] == "plus_e164_SA"
    assert c("966512345678")["class"] == "digits_e164_SA" and c("966512345678")["canonical"] == "+966512345678"
    assert c("5551234567")["class"] == "bare_10_not_valid_indian_mobile"          # still Indian-shaped: needs the legacy lookup
    assert c("")["class"] == "empty" and c(None)["class"] == "empty"
    assert c("abc123")["class"] == "non_numeric" and c("+12")["class"] == "plus_invalid"


def test_mask_never_shows_more_than_last_four_digits():
    m = A.mask("+919876543210")
    assert m.endswith("3210") and "98765" not in m and m.startswith("+")
    assert A.mask("12") == "••••"


def test_report_counts_duplicates_overlap_and_lookalikes():
    db = _db()
    db.accounts.insert_many([
        {"phone": "+919876543210"}, {"phone": "9876543210"},                          # same Indian number in two formats: a duplicate
        {"phone": "+9779812345678"}, {"phone": "+919812345678"},                      # Nepal and India holding the same national digits
        {"phone": "5551234567"}, {"phone": ""},
    ])
    db.users.insert_many([{"phone": "+919876543210"}])
    db.merchants.insert_many([{"phone": "9111111111"}])
    before = {n: db[n].count_documents({}) for n in ("accounts", "users", "merchants")}
    rep = A.audit(db, targets=[("accounts", "phone"), ("users", "phone"), ("merchants", "phone")])
    a = rep["collections"]["accounts"]
    assert a["records"] == 6
    assert a["duplicate_numbers_within_collection"] == 1 and a["records_in_duplicate_groups"] == 2
    assert a["indian_shaped_but_not_valid_mobile"] == 1
    assert a["formats"]["empty"] == 1 and a["formats"]["plus_e164_NP"] == 1
    assert rep["overlap"]["accounts & users"] == 1
    assert rep["lookalikes"]["pairs_of_different_countries_with_same_national_digits"] == 1
    assert rep["read_only"] is True
    assert {n: db[n].count_documents({}) for n in before} == before                  # nothing changed


def test_report_text_and_json_hold_no_full_number():
    db = _db()
    db.accounts.insert_many([{"phone": "+919876543210"}, {"phone": "+966512345678"}])
    rep = A.audit(db, targets=[("accounts", "phone")])
    text, js = A.render_text(rep), json.dumps(rep, ensure_ascii=False)
    for blob in (text, js):
        assert "9876543210" not in blob and "512345678" not in blob
        assert "3210" in blob


def test_audit_cannot_write_even_by_mistake():
    ro = A._ReadOnly(_db())
    for op in ("insert_one", "update_one", "delete_one", "update_many", "delete_many", "drop", "create_index", "replace_one"):
        with pytest.raises(PermissionError):
            getattr(ro["accounts"], op)
    with pytest.raises(PermissionError):
        ro["accounts"].find_one_and_update


def test_missing_collections_and_fields_are_handled():
    rep = A.audit(_db(), targets=[("no_such_collection", "phone")])
    assert rep["collections"]["no_such_collection"]["records"] == 0
