"""Targeted tests for utils/phone.py (Phase C). Run:  python -m pytest test_phone_helper.py -q -W ignore
The helper touches no database and no route; these tests also pin compatibility with the legacy Indian formats and with
services/wa_consent.py, and prove that numbers of different countries can never collide."""
import phonenumbers
import pytest
from phonenumbers import PhoneNumberType as T

from utils import phone as P

REGIONS = ["IN", "JO", "NP", "PH", "SA"]


def _example(region, kind=T.MOBILE):
    return phonenumbers.example_number_for_type(region, kind)


def _e164(region):
    return phonenumbers.format_number(_example(region), phonenumbers.PhoneNumberFormat.E164)


def _national(region):
    return str(_example(region).national_number)


def test_exactly_the_five_countries_alphabetical():
    assert [c.iso for c in P.SUPPORTED_COUNTRIES] == ["IN", "JO", "NP", "PH", "SA"]
    names = [c.name for c in P.SUPPORTED_COUNTRIES]
    assert names == sorted(names)
    assert [c.dial for c in P.SUPPORTED_COUNTRIES] == ["91", "962", "977", "63", "966"]
    assert P.supported_country("sa").dial == "966" and P.supported_country("+977").iso == "NP" and P.supported_country("US") is None


@pytest.mark.parametrize("region", REGIONS)
def test_valid_mobile_accepted_in_every_input_form(region):
    e164, nat = _e164(region), _national(region)
    dial = e164[1:-len(nat)]
    forms = [
        (e164, None),                      # +<cc><national>
        ("00" + e164[1:], None),           # 00<cc><national>
        (nat, region),                     # bare national with the selected country
        ("0" + nat, region),               # national with a trunk 0 (people type 05xxxxxxx)
        (dial + nat, region),              # <cc><national> typed without plus
        (" " + e164[:4] + " " + e164[4:] + " ", None),   # spaces
    ]
    for raw, country in forms:
        r = P.parse(raw, country)
        assert r.ok and r.e164 == e164 and r.country == region and r.national == nat, (region, raw, r)


@pytest.mark.parametrize("region", REGIONS)
def test_landline_rejected_as_not_mobile(region):
    fixed = _example(region, T.FIXED_LINE)
    if fixed is None or phonenumbers.number_type(fixed) == T.FIXED_LINE_OR_MOBILE:
        pytest.skip("this country has no distinct landline example")
    r = P.parse(phonenumbers.format_number(fixed, phonenumbers.PhoneNumberFormat.E164))
    assert not r.ok and r.error == P.NOT_MOBILE


def test_rejections_and_error_codes():
    assert P.parse("").error == P.EMPTY and P.parse(None).error == P.EMPTY and P.parse("+").error == P.EMPTY
    assert P.parse("98765abc10", "IN").error == P.INVALID_FORMAT
    assert P.parse("123", "IN").error in (P.INVALID_FORMAT, P.INVALID_NUMBER)
    assert P.parse("9876543210").error == P.COUNTRY_REQUIRED            # a bare number is never guessed
    assert P.parse("9876543210", "US").error == P.UNSUPPORTED_COUNTRY
    assert P.parse("+16502530000").error == P.UNSUPPORTED_COUNTRY       # a real number from an unsupported country
    assert P.parse(_e164("IN"), "NP").error == P.COUNTRY_MISMATCH       # Nepal selected, +91 number typed
    assert P.parse("1234567890", "IN").error in (P.INVALID_NUMBER, P.NOT_MOBILE)   # right length, not an Indian mobile
    assert not P.parse("5012345678", "SA").ok                           # a 10-digit Saudi number is wrong: SA mobiles have 9


def test_no_ten_digit_assumption():
    assert len(_national("SA")) == 9 and len(_national("JO")) == 9      # 9-digit countries
    assert len(_national("NP")) == 10 and len(_national("PH")) == 10 and len(_national("IN")) == 10
    for region in ("SA", "JO"):
        assert P.parse(_national(region), region).ok                    # accepted at 9 digits
        assert not P.parse(_national(region) + "7", region).ok          # and not accepted with a 10th digit appended


def test_arabic_indic_digits_are_accepted_and_normalised():
    nat = _national("SA")
    arabic = nat.translate({ord(str(i)): 0x0660 + i for i in range(10)})
    r = P.parse(arabic, "SA")
    assert r.ok and r.e164 == _e164("SA")
    ext = nat.translate({ord(str(i)): 0x06F0 + i for i in range(10)})
    assert P.parse(ext, "SA").e164 == _e164("SA")


def test_old_app_versions_legacy_india_default():
    n = _national("IN")
    assert P.parse(n, legacy_india=True).e164 == "+91" + n              # bare 10 digits from an old app
    assert P.parse("+91" + n, legacy_india=True).e164 == "+91" + n      # what old apps normally send
    assert P.parse("91" + n, legacy_india=True).e164 == "+91" + n
    assert P.parse("0" + n, legacy_india=True).e164 == "+91" + n
    assert P.parse(n).error == P.COUNTRY_REQUIRED                       # without the legacy flag it is refused


def test_numbers_of_different_countries_never_collide():
    # A Nepal or Philippines mobile has the same 10-digit SHAPE as an Indian mobile. Same digits, different countries:
    for region in ("NP", "PH"):
        nat = _national(region)
        indian = P.parse(nat, "IN")
        foreign = P.parse(nat, region)
        if indian.ok and foreign.ok:                                    # when the digits are valid in both countries
            assert indian.e164 != foreign.e164
            assert not set(P.lookup_keys(indian.e164)) & set(P.lookup_keys(foreign.e164))
    # and the lookup keys of a non-Indian number contain no bare national form that an Indian record could match
    for region in ("JO", "NP", "PH", "SA"):
        keys = P.lookup_keys(_e164(region))
        assert keys == [_e164(region), _e164(region)[1:]]
        assert _national(region) not in keys and ("0" + _national(region)) not in keys


def test_lookup_keys_for_india_equal_the_legacy_variants_exactly():
    from routers.users import _phone_variants                           # the existing function, unchanged
    n = _national("IN")
    legacy = set(_phone_variants("+91" + n))
    assert set(P.lookup_keys(P.to_e164("+91" + n))) == legacy
    for raw in (n, "91" + n, "+91" + n, "0" + n):
        assert set(P.lookup_keys_for(raw, legacy_india=True)) == legacy
        assert set(P.lookup_keys_for(raw, "IN")) == legacy


def test_old_indian_accounts_with_odd_numbers_can_still_be_found():
    odd = "5551234567"                                                  # Indian shape, not a valid Indian mobile today
    from routers.users import _phone_variants
    assert not P.parse(odd, "IN").ok
    assert set(P.lookup_keys_for(odd, legacy_india=True)) == set(_phone_variants(odd))
    assert set(P.lookup_keys_for("+91" + odd, legacy_india=True)) == set(_phone_variants(odd))
    # but a number of another shape or country is NOT turned into an Indian guess
    assert P.legacy_india_keys("+966" + _national("SA")) == []
    assert P.legacy_india_keys("12345") == []
    assert P.lookup_keys_for(odd, "SA") == []                           # an invalid Saudi number gives no lookup at all
    assert P.lookup_keys_for(odd) == []                                 # nor does a bare number with no country


def test_old_logic_collision_is_gone():
    # The old helper cut any longer number to its last 10 digits and called it Indian:
    from routers.users import _phone_variants
    nepal = _e164("NP")
    assert "+91" + _national("NP") in _phone_variants(nepal)            # documents the OLD hazard
    assert "+91" + _national("NP") not in P.lookup_keys(nepal)          # the new helper does not do it


def test_wa_digits_and_consistency_with_whatsapp_consent_key():
    from services.wa_consent import normalize_phone
    for region in REGIONS:
        e = _e164(region)
        assert P.wa_digits(e) == e[1:]
        assert normalize_phone(e) == P.wa_digits(e), region             # same canonical WhatsApp key for explicit +E.164 input
    n = _national("IN")
    for raw in (n, "91" + n, "+91" + n, "0" + n):
        assert normalize_phone(raw) == P.wa_digits(P.to_e164(raw, legacy_india=True))


def test_wa_consent_still_guesses_for_bare_numbers_documented_hazard():
    # services/wa_consent.py treats a bare 10-digit number starting 6-9 as Indian. For Nepal and Philippines that is wrong unless
    # the country is known; Phase D must pass the country to it. This test records the current behaviour so it is not forgotten.
    from services.wa_consent import normalize_phone
    nat = _national("NP")
    assert normalize_phone(nat) == "91" + nat
    assert P.to_e164(nat, "NP") == "+977" + nat


def test_invalid_input_gives_no_keys():
    assert P.lookup_keys("not a number") == [] and P.lookup_keys("") == []
    assert P.to_e164("garbage", "IN") == ""
