"""Admin Accounts tab - "Created" column + sort by creation date.
Run:  python -m pytest test_dashboard_accounts_created.py -q -W ignore"""
import sys, re
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
import server
from routers import admin as ADM

fake = sys.modules["database"].db
c = TestClient(server.app)
A = {"Authorization": "Bearer at"}


@pytest.fixture(autouse=True)
def _admin():
    role = fake.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    fake.dashboard_users.insert_one({"token": "at", "status": "active", "full_name": "T", "mobile": "9000000001", "role_id": role})
    yield


def _acct(name, created="__missing__", **kw):
    d = {"name": name, "phone": "9" + str(abs(hash(name)))[:9].ljust(9, "0"), "roles": ["user"], "status": "active"}
    if created != "__missing__":
        d["created_at"] = created
    d.update(kw)
    return fake.accounts.insert_one(d).inserted_id


def _names(**q):
    r = c.get("/admin/accounts", params={"page": 1, "page_size": 50, **q}, headers=A)
    assert r.status_code == 200, r.text
    return [i["full_name"] for i in r.json()["items"]], r.json()["items"]


def test_created_iso_normalises_strings_datetimes_and_bad_values():
    assert ADM._created_iso("2026-03-05T10:20:30.123456") == "2026-03-05T10:20:30Z"
    assert ADM._created_iso(datetime(2026, 3, 5, 10, 20, 30)) == "2026-03-05T10:20:30Z"
    assert ADM._created_iso("2026-03-05T15:50:30+05:30") == "2026-03-05T10:20:30Z"
    assert ADM._created_iso(datetime(2026, 3, 5, 15, 50, tzinfo=timezone.utc)) == "2026-03-05T15:50:00Z"
    assert ADM._created_iso("2026-03-05") == "2026-03-05T00:00:00Z"
    for bad in (None, "", "   ", "not a date", 12345, {}):
        assert ADM._created_iso(bad) == ""


def test_row_exposes_created_at_iso_and_keeps_legacy_created_at():
    _acct("A", "2026-03-05T10:20:30")
    _acct("B")                                  # old record, no timestamp
    _acct("C", "garbage")
    _, items = _names()
    by = {i["full_name"]: i for i in items}
    assert by["A"]["created_at_iso"] == "2026-03-05T10:20:30Z" and by["A"]["created_at"] == "2026-03-05"
    assert by["B"]["created_at_iso"] == "" and by["B"]["created_at"] == ""
    assert by["C"]["created_at_iso"] == ""


def test_created_is_not_last_login_or_updated():
    _acct("A", "2026-01-01T00:00:00", updated_at="2026-09-09T09:09:09", last_login="2026-10-10T10:10:10")
    _, items = _names()
    assert items[0]["created_at_iso"] == "2026-01-01T00:00:00Z"


def test_sort_asc_desc_across_mixed_types_with_missing_last():
    _acct("old_str", "2025-01-01T00:00:00")
    _acct("mid_dt", datetime(2025, 6, 1, 12, 0, 0))
    _acct("new_str", "2026-02-01T08:00:00")
    _acct("nodate")
    asc, _ = _names(sort="created_asc")
    desc, _ = _names(sort="created_desc")
    assert asc == ["old_str", "mid_dt", "new_str", "nodate"]
    assert desc == ["new_str", "mid_dt", "old_str", "nodate"]


def test_sort_pages_correctly_and_respects_filters():
    for i in range(7):
        _acct(f"u{i}", f"2026-01-0{i+1}T00:00:00")
    p1 = c.get("/admin/accounts", params={"page": 1, "page_size": 3, "sort": "created_asc"}, headers=A).json()
    p3 = c.get("/admin/accounts", params={"page": 3, "page_size": 3, "sort": "created_asc"}, headers=A).json()
    assert [i["full_name"] for i in p1["items"]] == ["u0", "u1", "u2"]
    assert [i["full_name"] for i in p3["items"]] == ["u6"] and p1["total"] == 7 and p1["pages"] == 3
    f = c.get("/admin/accounts", params={"page": 1, "sort": "created_desc", "search": "u3"}, headers=A).json()
    assert [i["full_name"] for i in f["items"]] == ["u3"]


def test_default_and_unknown_sort_behave_as_before():
    _acct("a", "2026-01-01T00:00:00"); _acct("b", "2026-02-01T00:00:00")
    d, _ = _names()
    u, _ = _names(sort="bogus")
    assert d == u == ["b", "a"]


def test_unpaged_call_still_returns_a_plain_list():
    _acct("a", "2026-01-01T00:00:00")
    r = c.get("/admin/accounts", headers=A)
    assert isinstance(r.json(), list) and r.json()[0]["created_at_iso"] == "2026-01-01T00:00:00Z"


def test_requires_admin():
    assert c.get("/admin/accounts", params={"page": 1}).status_code in (401, 403)


def test_dashboard_template_wires_the_column_consistently():
    html = open("templates/admin_dashboard.html", encoding="utf-8").read()
    head = re.search(r'<div id="section-accounts".*?</thead>', html, re.S).group(0)
    assert len(re.findall(r"<th[ >]", head)) == 12 and "Created" in head
    assert "fmtAccountCreated(a.created_at_iso)" in html and "qs.set('sort', _acctSort)" in html
    assert 'colspan="11"' not in html[html.index("async function loadAccounts"):html.index("function goAcctPage")]
