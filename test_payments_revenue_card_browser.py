"""Payments > Total Revenue card, in the REAL admin dashboard (headless Chromium).
Per-currency totals, zero totals hidden, a non-zero amount with no recorded currency shown as "Unrecorded Currency" (never INR),
nothing converted.  Run:  python -m pytest test_payments_revenue_card_browser.py -q -W ignore"""
import json
import sys

import pytest

from test_deals_admin_browser import _open, browser, world  # noqa: F401  (fixtures + helper)


def _card(page, invoices, subs=()):
    """Feed the page's own loader the given /admin/invoices/full and /admin/subscriptions answers, return the card text."""
    page.evaluate("""([inv, subs]) => { window.fetch = (url) => Promise.resolve({ok: true,
        json: () => Promise.resolve(String(url).includes('/admin/invoices/full') ? JSON.parse(inv) : JSON.parse(subs))}); }""",
                  [json.dumps(list(invoices)), json.dumps(list(subs))])
    page.evaluate("loadMerchantTxnsFull()")
    page.wait_for_function("document.getElementById('txn_total_revenue').textContent !== '--'")
    return page.inner_text("#txn_total_revenue").strip()


def inv(no, total, cur=""):
    return {"invoice_no": no, "total": total, "currency_code": cur, "merchant_name": "m", "type": "store", "created_at": "2026-01-01 10:00:00"}


def test_single_recorded_currency(browser, world):
    page, log = _open(browser)
    assert _card(page, [inv("A1", 500, "INR"), inv("A2", 58.24, "INR")]) == "INR 558.24"
    assert log["errors"] == []


def test_zero_unknown_currency_is_not_shown_the_reported_case(browser, world):
    """The reported card: 'INR 558.24 · 0.00 (no currency recorded)'. A free (0.00) row with no currency must not appear."""
    page, _ = _open(browser)
    text = _card(page, [inv("A1", 558.24, "INR"), inv("FREE", 0, "")])
    assert text == "INR 558.24"
    assert "no currency" not in text.lower() and "unrecorded" not in text.lower() and "0.00" not in text


def test_multiple_currencies_separate_sorted_never_added_or_converted(browser, world):
    page, _ = _open(browser)
    text = _card(page, [inv("A1", 100, "SAR"), inv("A2", 200, "INR"), inv("A3", 50, "SAR"), inv("A4", 30, "NPR")])
    assert text == "INR 200.00 · NPR 30.00 · SAR 150.00"


def test_zero_value_recorded_currency_is_hidden(browser, world):
    page, _ = _open(browser)
    assert _card(page, [inv("A1", 80, "INR"), inv("FREE", 0, "SAR")]) == "INR 80.00"


def test_non_zero_unknown_currency_is_shown_separately_and_never_as_inr(browser, world):
    page, _ = _open(browser)
    text = _card(page, [inv("A1", 558.24, "INR"), inv("A2", 99, "")])
    assert text == "INR 558.24 · Unrecorded Currency 99.00"
    assert "INR 657.24" not in text and "no currency recorded" not in text


def test_only_unknown_currency(browser, world):
    page, _ = _open(browser)
    assert _card(page, [inv("A1", 40, ""), inv("A2", 2.5, "")]) == "Unrecorded Currency 42.50"


def test_nothing_to_show_is_a_plain_zero(browser, world):
    page, _ = _open(browser)
    assert _card(page, []) == "0.00"
    assert _card(page, [inv("FREE", 0, "")]) == "0.00"


def test_pending_rows_do_not_count_as_revenue(browser, world):
    page, _ = _open(browser)
    text = _card(page, [inv("A1", 100, "INR")], [{"status": "pending", "total": 900, "currency_code": "INR", "plan": "x"}])
    assert text == "INR 100.00"


def test_revenue_function_is_pure_and_keeps_the_numbers(browser, world):
    page, _ = _open(browser)
    f = lambda rows: page.evaluate("r => txnRevenueByCurrency(r)", rows)
    assert f([{"total": "12.5", "currency_code": "INR"}, {"total": None, "currency_code": "INR"}]) == "INR 12.50"
    assert f([{"total": 0.001, "currency_code": ""}]) == "0.00"                      # rounds to 0.00: not shown
    assert f([{"total": 1, "currency_code": "inr"}]) == "inr 1.00"                    # the recorded code is shown as recorded
