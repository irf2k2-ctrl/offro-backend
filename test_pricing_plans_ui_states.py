"""Static checks for the Pricing & Plans dashboard client (static/js/pricing_plans.js): every tab talks to the API, nothing is mocked,
and loading / retry / friendly-error / busy states exist. Behaviour in a real browser is checked separately (not part of this file)."""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


JS = _read("static/js/pricing_plans.js")
CSS = _read("static/css/pricing_plans.css")


def test_every_tab_is_backed_by_an_api_call():
    for frag in ["'/countries'", "'/countries/' + encodeURIComponent(st.country) + '/prices'", "'/history'", "'/prices/' + section",
                 "'/prices/reward'", "'/status'"]:
        assert frag in JS, frag
    assert "var API = '/admin/pricing-config'" in JS


@pytest.mark.parametrize("bad", ["mockData", "MOCK", "dummy", "sampleData", "localStorage", "sessionStorage", "Math.random", "razorpay", "/checkout", "/wallet", "/withdraw"])
def test_no_mock_data_or_out_of_scope_calls(bad):
    assert bad.lower() not in JS.lower()


def test_loading_state_for_all_tabs_and_retry():
    assert "function loaderHtml" in JS and "st.loading" in JS
    assert "if (st.loading) return setHtml(el, loaderHtml())" in JS      # before any tab view is chosen, so all seven tabs get it
    assert "data-act=\"retry\"" in JS and "act === 'retry'" in JS
    assert ".pp-loading" in CSS and ".pp-spin" in CSS and "prefers-reduced-motion" in CSS


def test_friendly_errors_do_not_expose_raw_failures():
    for frag in ["Could not reach the server", "Your session has expired", "Something went wrong on our side", "do not have permission"]:
        assert frag in JS, frag
    # 5xx wording never echoes the server body
    m = re.search(r"if \(status >= 500\) return '([^']+)'", JS)
    assert m and "+" not in m.group(0)


def test_latest_request_wins():
    assert "++st.rseq" in JS and "seq !== st.rseq" in JS


def test_actions_are_guarded_against_double_submit():
    assert "function guarded" in JS and "'Saving...'" in JS and ".disabled = on" in JS
    for act in ["save", "toggle", "save-tax", "save-country", "save-reward"]:
        assert re.search(r"act === '%s'\) guarded\(" % re.escape(act), JS), act
    assert "country-status" in JS and "reward-toggle" in JS
    assert JS.count("guarded(t,") >= 7
    assert ".pp-btn:disabled" in CSS


def test_still_no_removed_features():
    html = _read("templates/partials/pricing_plans.html")
    for banned in ["Bulk", "Import", "Export", "Countries Priced"]:
        assert banned not in JS and banned not in html


def test_inner_html_only_through_set_html():
    assert len(re.findall(r"\.innerHTML\s*=", JS)) == 1
