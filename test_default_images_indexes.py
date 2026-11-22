"""Stage 3 follow-up: the unique indexes of the two new Default Images collections are created BY THE RUNNING APPLICATION
(startup lifecycle), safely, repeatedly, with failures logged - not merely when the function is called directly."""
import inspect
import logging
import sys

import pytest
from fastapi.testclient import TestClient

import server
from services import default_images as DI

DB = sys.modules["database"].db
MAP_IDX, VAR_IDX = "default_image_countries_cat_url", "default_no_service_variants_id"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(DI, "_indexes_ok", False)
    for c in (DI.MAPPINGS, DI.VARIANTS):
        DB.drop_collection(c)                      # test isolation only (mongomock); the application never drops anything
    yield


def unique_indexes(coll):
    return {n: i for n, i in DB[coll].index_information().items() if i.get("unique")}


def test_startup_creates_both_unique_indexes_in_the_running_application():
    assert MAP_IDX not in unique_indexes(DI.MAPPINGS) and VAR_IDX not in unique_indexes(DI.VARIANTS)
    with TestClient(server.app):                   # entering the client runs the real FastAPI startup event
        pass
    assert MAP_IDX in unique_indexes(DI.MAPPINGS)
    assert VAR_IDX in unique_indexes(DI.VARIANTS)


def test_startup_function_invokes_the_initialisation(monkeypatch):
    calls = []
    real = DI.ensure_indexes_safe
    monkeypatch.setattr(DI, "ensure_indexes_safe", lambda: calls.append(1) or real())
    server.startup()
    assert calls == [1]


def test_startup_source_wires_it_after_db_setup():
    src = inspect.getsource(server.startup)
    assert "default_images" in src and "ensure_indexes_safe" in src


def test_repeated_startups_are_safe_and_keep_all_data():
    with TestClient(server.app):
        DB[DI.MAPPINGS].insert_one({"category": "store", "url_key": "k", "countries": ["SA"]})
        DB[DI.VARIANTS].insert_one({"variant_id": "v", "url": "https://x/y.jpg", "countries": ["SA"]})
    for _ in range(3):
        with TestClient(server.app):
            pass
    assert DB[DI.MAPPINGS].count_documents({}) == 1 and DB[DI.VARIANTS].count_documents({}) == 1
    assert len(unique_indexes(DI.MAPPINGS)) == 1 and len(unique_indexes(DI.VARIANTS)) == 1


def test_unique_index_really_blocks_duplicates_after_startup():
    with TestClient(server.app):
        pass
    DB[DI.MAPPINGS].insert_one({"category": "store", "url_key": "k", "countries": ["SA"]})
    with pytest.raises(Exception):
        DB[DI.MAPPINGS].insert_one({"category": "store", "url_key": "k", "countries": ["JO"]})


def test_index_failure_is_logged_not_silent_and_does_not_stop_startup(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("index build refused")
    monkeypatch.setattr(DI, "ensure_indexes", boom)
    with caplog.at_level(logging.ERROR, logger=DI.logger.name):
        with TestClient(server.app):               # the app still starts
            pass
    rec = [r for r in caplog.records if r.name == DI.logger.name and r.levelno >= logging.ERROR]
    assert rec and "index build refused" in rec[0].getMessage() and "default image country index" in rec[0].getMessage()
    assert DI._indexes_ok is False


def test_failure_at_startup_is_retried_before_the_first_write(monkeypatch):
    real = DI.ensure_indexes
    state = {"fail": True}

    def flaky():
        if state["fail"]:
            raise RuntimeError("db unreachable")
        real()
    monkeypatch.setattr(DI, "ensure_indexes", flaky)
    assert DI.ensure_indexes_safe() is False
    assert MAP_IDX not in unique_indexes(DI.MAPPINGS)
    state["fail"] = False
    DI.set_mapping("store", "https://x/a.jpg", ["SA"])          # first use of the new collection
    assert MAP_IDX in unique_indexes(DI.MAPPINGS)
    DI.create_variant({"url": "https://x/ns.jpg", "countries": ["SA"]})
    assert VAR_IDX in unique_indexes(DI.VARIANTS)


def test_guard_is_cheap_after_success(monkeypatch):
    n = []
    real = DI.ensure_indexes
    monkeypatch.setattr(DI, "ensure_indexes", lambda: n.append(1) or real())
    assert DI.ensure_indexes_once() and DI.ensure_indexes_once() and DI.ensure_indexes_once()
    assert n == [1]


def test_initialisation_never_drops_or_deletes():
    src = inspect.getsource(DI.ensure_indexes) + inspect.getsource(DI.ensure_indexes_safe) + inspect.getsource(DI.ensure_indexes_once)
    for bad in (".drop", "drop_collection", "delete_", "remove(", "update_", "insert_"):
        assert bad not in src
