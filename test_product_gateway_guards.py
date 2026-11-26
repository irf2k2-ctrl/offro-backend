"""NOTE: sorts AFTER test_banner_media.py on purpose (that module swaps the shared fake `database` at import).
Product orders: the same exact amount/currency verification, configured minimum, zero-amount and minimum-payable rules as store subscriptions
and banners. India's flow is untouched."""
import sys

import pytest

import routers.merchant_products as MP
from services import country_context as CC

fake = sys.modules["database"].db
SA = {"currency_code": "SAR", "currency_symbol": "SAR", "tax_label": "VAT", "tax_percent": 15, "iso": "SA", "name": "Saudi Arabia"}


@pytest.fixture
def rzp(monkeypatch):
    monkeypatch.setenv("RAZORPAY_INTERNATIONAL_CURRENCIES", "SAR")
    monkeypatch.delenv("RAZORPAY_MIN_ORDER_MINOR", raising=False)
    monkeypatch.setattr(MP, "RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setattr(MP, "RAZORPAY_KEY_SECRET", "s")
    calls, reply = [], {}

    def fake_request(method, path, auth, json_data, timeout=8):
        calls.append(json_data)

        class R:
            def json(self):
                return reply.get("override") or {"id": "order_P1", "amount": json_data["amount"], "currency": json_data["currency"]}
        return R()
    monkeypatch.setattr(MP, "_razorpay_request", fake_request)
    return calls, reply


def _order(paise, ctx=SA):
    with CC.activated(ctx):
        return MP._razorpay_order(paise, "prd_x")


def test_exact_order_is_used_as_calculated(rzp):
    calls, _ = rzp
    assert _order(14) == ("order_P1", "razorpay", None)
    assert calls == [{"amount": 14, "currency": "SAR", "receipt": "prd_x"}]       # never rounded up to SAR 1


def test_amount_mismatch_is_not_passed_on(rzp):
    _, reply = rzp
    reply["override"] = {"id": "order_BAD", "amount": 100, "currency": "SAR"}
    assert _order(14) == (None, "manual", "gateway_amount_mismatch")


def test_currency_mismatch_is_not_passed_on(rzp):
    _, reply = rzp
    reply["override"] = {"id": "order_BAD", "amount": 14, "currency": "INR"}
    assert _order(14) == (None, "manual", "gateway_amount_mismatch")
    reply["override"] = {"id": "order_BAD"}                                          # no amount/currency echoed at all
    assert _order(14) == (None, "manual", "gateway_amount_mismatch")


def test_below_configured_minimum_never_reaches_razorpay_and_is_not_raised(rzp, monkeypatch):
    calls, _ = rzp
    monkeypatch.setenv("RAZORPAY_MIN_ORDER_MINOR", "SAR:100")
    assert _order(14) == (None, "manual", "below_gateway_minimum")
    assert _order(99) == (None, "manual", "below_gateway_minimum")
    assert calls == []
    assert _order(100)[1] == "razorpay" and calls[-1]["amount"] == 100


@pytest.mark.parametrize("paise", [0, -5])
def test_zero_payable_never_reaches_razorpay(rzp, paise):
    calls, _ = rzp
    assert _order(paise) == (None, "manual", None)
    assert _order(paise, None) == (None, "manual", None)
    assert calls == []


def test_manual_reason_is_reported_and_recorded_reason_wins(rzp):
    with CC.activated(SA):
        assert MP._manual_reason(14, "manual", "gateway_amount_mismatch") == "gateway_amount_mismatch"
        assert MP._manual_reason(14, "razorpay") is None
        assert MP._manual_reason(0, "manual") is None
    assert MP._manual_reason(14, "manual", "x") is None                                  # India: never a reason


def test_india_flow_is_unchanged(rzp, monkeypatch):
    calls, reply = rzp
    monkeypatch.setenv("RAZORPAY_MIN_ORDER_MINOR", "INR:100000")                       # ignored for India
    reply["override"] = {"id": "order_IN"}                                             # India: the order id is used as before
    assert _order(49900, None) == ("order_IN", "razorpay", None)
    assert calls[-1]["currency"] == "INR" and calls[-1]["amount"] == 49900


def test_minimum_payable_after_discount_applies_to_products_like_the_other_flows(monkeypatch):
    fake.discounts.delete_many({})
    fake.discounts.insert_one({"code": "HALF", "type": "PERCENTAGE", "value": 50, "active": True, "applies_to": "ALL", "max_uses": 0, "used_count": 0})
    fake.discounts.insert_one({"code": "FREE", "type": "PERCENTAGE", "value": 100, "active": True, "applies_to": "ALL", "max_uses": 0, "used_count": 0})
    dec = {"unit_price": 0.12, "gst_percent": 15}
    with CC.activated(SA):
        with pytest.raises(Exception) as e:
            MP._price_block(dec, "HALF")                                              # 0.06 + VAT = 0.07 -> refused
        assert e.value.status_code == 400 and "SAR 1.00" in e.value.detail["message"]
        free = MP._price_block(dec, "FREE")
        assert free["total"] == 0 and free["paise"] == 0                             # free path: no gateway
        assert MP._price_block({"unit_price": 100, "gst_percent": 15}, "HALF")["total"] == 57.5
        assert MP._price_block(dec, None)["total"] == 0.14                             # no code: not a discount decision
    ind = MP._price_block({"unit_price": 0.12, "gst_percent": 18}, "HALF")            # India: no minimum-payable rule
    assert ind["total"] > 0
